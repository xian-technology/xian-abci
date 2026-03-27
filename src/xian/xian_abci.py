import asyncio
import gc
import importlib
import os
import signal
import sys
from dataclasses import replace
from datetime import datetime, timedelta
from urllib.parse import urlsplit, urlunsplit

from contracting.client import ContractingClient
from loguru import logger

from abci.server import ABCIServer
from abci.utils import get_logger
from xian.constants import Constants
from xian.methods import (
    check_tx,
    commit,
    echo,
    finalize_block,
    info,
    init_chain,
    prepare_proposal,
    process_proposal,
    query,
)
from xian.metrics import MetricsService
from xian.nonce import NonceStorage
from xian.parallel_executor import ParallelBlockExecutor
from xian.perf import PerfTracker
from xian.processor import TxProcessor
from xian.rewards import RewardsHandler
from xian.services.bds.bds import BDS
from xian.services.bds.config import BdsConfig
from xian.services.state_sync import StateSnapshotManager
from xian.simulator import QuerySimulationService, snapshot_driver_state
from xian.utils.block import get_latest_block_height
from xian.utils.cometbft import (
    load_genesis_data,
    load_tendermint_config,
)
from xian.utils.state_patches import StatePatchManager
from xian.validators import ValidatorHandler

get_logger("requests").setLevel(30)
get_logger("urllib3").setLevel(30)
get_logger("asyncio").setLevel(30)

LOG_RETENTION_DAYS = 3


def resolve_local_rpc_url(cometbft_config: dict) -> str:
    laddr = str(
        cometbft_config.get("rpc", {}).get("laddr", "tcp://127.0.0.1:26657")
    )
    normalized = laddr
    if not normalized.startswith(("http://", "https://")):
        normalized = normalized.replace("tcp://", "http://").replace(
            "unix://", "http://"
        )
    normalized = normalized.rstrip("/")
    parts = urlsplit(normalized)
    host = parts.hostname or "127.0.0.1"
    if host in {"0.0.0.0", "::"}:
        netloc = f"127.0.0.1:{parts.port or 26657}"
        return urlunsplit((parts.scheme or "http", netloc, parts.path, "", ""))
    return normalized


def load_module(module_path, original_module_path):
    """
    Inplace replace of a module with a new one and taking its name.
    """
    try:
        # Replace all functions in the original module with the new modules functions
        module = importlib.import_module(module_path)
        modified_original_module = importlib.import_module(original_module_path)
        for name in dir(module):
            if name.startswith("__"):
                continue
            setattr(modified_original_module, name, getattr(module, name))
        sys.modules[original_module_path] = modified_original_module
        del sys.modules[module_path]
        gc.collect()
        logger.info(f"Loaded module {module_path}")
    except Exception as e:
        raise Exception(f"Failed to load module {module_path}: {e}")


class Xian:
    def __init__(self, constants=Constants()):
        try:
            self.cometbft_config = load_tendermint_config(constants)
            self.genesis = load_genesis_data(constants)
        except Exception as e:
            logger.error(e)
            raise SystemExit()

        self.tracer_mode = self.cometbft_config.get("xian", {}).get(
            "tracer_mode", "python_line_v1"
        )
        self.client = ContractingClient(
            storage_home=constants.STORAGE_HOME,
            tracer_mode=self.tracer_mode,
        )
        self.chain_id = self.genesis.get("chain_id", None)
        if self.chain_id is None:
            raise ValueError("No value set for 'chain_id' in genesis block")
        self.profiler = PerfTracker.from_env(
            cometbft_home=constants.COMETBFT_HOME,
            node_name=self.cometbft_config.get("moniker", ""),
            chain_id=self.chain_id,
            tracer_mode=self.tracer_mode,
        )
        self.state_snapshot_manager = StateSnapshotManager(
            storage_home=constants.STORAGE_HOME,
            chain_id=self.chain_id,
        )
        self.validator_handler = ValidatorHandler(self)
        xian_config = self.cometbft_config.get("xian", {})
        self.nonce_storage = NonceStorage(
            self.client,
            reservation_ttl_seconds=xian_config.get(
                "pending_nonce_reservation_ttl_seconds",
                60.0,
            ),
        )
        self.transaction_trace_logging = xian_config.get(
            "transaction_trace_logging", False
        )
        self.tx_processor = TxProcessor(
            client=self.client,
            profiler=self.profiler,
            trace_logging=self.transaction_trace_logging,
        )
        self.simulator = QuerySimulationService(
            storage_home=constants.STORAGE_HOME,
            tracer_mode=self.tracer_mode,
            get_block_meta=lambda: self.current_block_meta,
            get_state_snapshot=lambda: snapshot_driver_state(
                self.client.raw_driver
            ),
            enabled=xian_config.get("simulation_enabled", True),
            max_concurrency=xian_config.get("simulation_max_concurrency", 2),
            timeout_ms=xian_config.get("simulation_timeout_ms", 3000),
            max_stamps=xian_config.get("simulation_max_stamps", 1_000_000),
        )
        self.rewards_handler = RewardsHandler(client=self.client)
        self.current_block_meta: dict = None
        self.fingerprint_hashes = []
        self.merkle_root_hash = None

        self.block_service_mode = xian_config.get("block_service_mode", False)
        self.bds_config = BdsConfig.from_runtime_settings(xian_config)
        if self.bds_config.spool_dir is None:
            self.bds_config = replace(
                self.bds_config,
                spool_dir=str(constants.STORAGE_HOME / "bds-spool"),
            )
        if self.bds_config.rpc_url is None:
            self.bds_config = replace(
                self.bds_config,
                rpc_url=resolve_local_rpc_url(self.cometbft_config),
            )

        self.pruning_enabled = xian_config.get("pruning_enabled", False)
        # If pruning is enabled, this is the number of blocks to keep history for
        self.blocks_to_keep = xian_config.get("blocks_to_keep", 100000)
        self.parallel_block_executor = ParallelBlockExecutor(
            storage_home=constants.STORAGE_HOME,
            enabled=xian_config.get("parallel_execution_enabled", False),
            workers=xian_config.get("parallel_execution_workers", 0),
            min_transactions=xian_config.get(
                "parallel_execution_min_transactions", 8
            ),
        )
        self.app_version = 1
        self.metrics_service = MetricsService.from_runtime_settings(
            self, xian_config
        )

        if self.genesis.get("abci_genesis", None) is None:
            raise ValueError(
                "No value set for 'abci_genesis' in Tendermint genesis.json"
            )

        self.enable_tx_fee = True
        self.static_rewards = False
        self.static_rewards_amount_foundation = 1
        self.static_rewards_amount_validators = 1
        self.current_block_rewards = {}

        # Initialize state patch manager
        self.state_patch_manager = StatePatchManager(self.client.raw_driver)

        # Set up the state patches file path
        start_path = os.path.dirname(os.path.realpath(__file__))
        patch_file_path = os.path.join(
            start_path, "tools", "state_patches", "state_patches.json"
        )

        if os.path.exists(patch_file_path):
            logger.info(f"Loading state patches from: {patch_file_path}")
            self.state_patch_manager.load_patches(patch_file_path)
        else:
            logger.warning(f"No state patches file found at {patch_file_path}")
            # Initialize with empty patches but still mark as loaded
            self.state_patch_manager.loaded = True

    @classmethod
    async def create(cls, constants=Constants()):
        self = cls(constants=constants)
        if self.block_service_mode:
            self.bds = BDS(self.bds_config)
            self._bds_storage_initialized = False
        return self

    async def start_runtime(self):
        if self.block_service_mode and hasattr(self, "bds"):
            if not getattr(self, "_bds_storage_initialized", False):
                await self.bds.initialize_storage(cometbft_genesis=self.genesis)
                self._bds_storage_initialized = True
            await self.bds.start()
        await self.metrics_service.start()

    async def echo(self, req):
        """
        Echo a string to test an ABCI client/server implementation
        """
        res = echo.echo(self, req)
        return res

    async def info(self, req):
        """
        Called every time the application starts
        Return information about the application state.
        """
        res = await info.info(self, req)
        return res

    async def init_chain(self, req):
        """Called once upon genesis."""
        resp = await init_chain.init_chain(self, req)
        return resp

    async def check_tx(self, raw_tx):
        """
        Technically optional - not involved in processing blocks.
        Guardian of the mempool: every node runs CheckTx before letting a transaction into its local mempool.
        The transaction may come from an external user or another node
        """
        with self.profiler.scope("check_tx"):
            res = await check_tx.check_tx(self, raw_tx)
        return res

    async def finalize_block(self, req):
        """
        Contains the fields of the newly decided block.
        This method is equivalent to the call sequence BeginBlock, [DeliverTx], and EndBlock in the previous version of ABCI.
        """
        self.profiler.start_block(req.height, len(req.txs))
        try:
            with self.profiler.scope("finalize_block", block_scoped=True):
                res = await finalize_block.finalize_block(self, req)
            self.profiler.end_block(app_hash=res.app_hash.hex())
            return res
        except Exception as exc:
            self.profiler.end_block(error=f"{type(exc).__name__}: {exc}")
            raise

    async def commit(self):
        """
        Signal the Application to persist the application state. Application is expected to persist its state at the end of this call, before calling ResponseCommit.
        """
        res = await commit.commit(self)
        return res

    async def process_proposal(self, req):
        """
        Contains all information on the proposed block needed to fully execute it.
        """
        with self.profiler.scope("process_proposal"):
            res = await process_proposal.process_proposal(self, req)
        return res

    async def prepare_proposal(self, req):
        """
        RequestPrepareProposal contains a preliminary set of transactions txs that CometBFT retrieved from the mempool, called raw proposal. The Application can modify this set and return a modified set of transactions via ResponsePrepareProposal.txs .
        """
        with self.profiler.scope("block_packing"):
            res = await prepare_proposal.prepare_proposal(self, req)
        return res

    async def list_snapshots(self, req):
        del req
        return self.state_snapshot_manager.list_snapshots_response()

    async def offer_snapshot(self, req):
        current_height = get_latest_block_height(
            self.client.raw_driver.storage_home
        )
        return self.state_snapshot_manager.offer_snapshot_response(
            req.snapshot,
            app_hash=req.app_hash,
            current_height=current_height,
        )

    async def load_snapshot_chunk(self, req):
        return self.state_snapshot_manager.load_snapshot_chunk_response(
            height=req.height,
            format_version=req.format,
            chunk_index=req.chunk,
        )

    async def apply_snapshot_chunk(self, req):
        response = self.state_snapshot_manager.apply_snapshot_chunk_response(
            index=req.index,
            chunk=req.chunk,
            sender=req.sender,
        )
        if response.result == response.ACCEPT:
            self.nonce_storage.flush_pending()
        return response

    async def query(self, req):
        """
        Query the application state
        Request Ex. http://localhost:26657/abci_query?path="path"
        """
        res = await query.query(self, req)
        return res

    async def close(self):
        self.parallel_block_executor.close()
        self.simulator.close()
        await self.metrics_service.close()
        if self.block_service_mode and hasattr(self, "bds"):
            await self.bds.close()


def cleanup_old_logs(logs_dir: str, days: int = 3):
    """Clean up log files older than specified days on startup"""
    try:
        threshold = datetime.now() - timedelta(days=days)
        for f in os.listdir(logs_dir):
            if not f.endswith(".log"):
                continue

            file_path = os.path.join(logs_dir, f)
            file_time = datetime.fromtimestamp(os.path.getctime(file_path))

            if file_time < threshold:
                try:
                    os.remove(file_path)
                    logger.debug(f"Removed old log file: {f}")
                except OSError as e:
                    logger.error(f"Error removing old log file {f}: {e}")
    except Exception as e:
        logger.error(f"Error during log cleanup: {e}")


def main():
    logger.remove()
    logger.add(sys.stderr, level="DEBUG")

    start_path = os.path.dirname(os.path.realpath(__file__))
    log_path = os.path.realpath(os.path.join(start_path, "..", ".."))
    logs_dir = os.path.join(log_path, "logs")

    os.makedirs(logs_dir, exist_ok=True)

    # Clean up old logs on startup
    cleanup_old_logs(logs_dir, days=LOG_RETENTION_DAYS)

    logger.add(
        os.path.join(log_path, "logs", "{time}.log"),
        retention=timedelta(days=LOG_RETENTION_DAYS),
        rotation=timedelta(hours=1),
        format="{time} {level} {name} {message}",
        level="DEBUG",
        enqueue=True,
        compression="zip",  # Compress old logs
    )

    # Register signal handlers
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    app = asyncio.run(Xian.create())
    ABCIServer(app=app).run()


def signal_handler(signum, frame):
    logger.info("Shutting down...")
    logger.remove()
    sys.exit(0)


if __name__ == "__main__":
    main()
