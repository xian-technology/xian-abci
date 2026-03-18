UV ?= uv
COMETBFT_BIN ?= cometbft
COMETBFT_HOME ?= $(HOME)/.cometbft
RPC_LADDR ?= tcp://0.0.0.0:26657

.DEFAULT_GOAL := help

.PHONY: help sync validate wipe reset init node-id \
	run-abci run-cometbft run-dashboard configure-node export-state

help:
	@printf "Available targets:\n"
	@printf "  %-18s %s\n" "sync" "Install the development environment with uv"
	@printf "  %-18s %s\n" "validate" "Run the repo validation script"
	@printf "  %-18s %s\n" "init" "Initialize the local CometBFT home"
	@printf "  %-18s %s\n" "wipe" "Delete state and run cometbft unsafe-reset-all"
	@printf "  %-18s %s\n" "reset" "Run wipe and then init"
	@printf "  %-18s %s\n" "node-id" "Print the CometBFT node ID"
	@printf "  %-18s %s\n" "run-abci" "Run the xian-abci application process"
	@printf "  %-18s %s\n" "run-cometbft" "Run the CometBFT process"
	@printf "  %-18s %s\n" "run-dashboard" "Run the optional dashboard service"
	@printf "  %-18s %s\n" "configure-node" "Run xian-configure-node with ARGS='...'"
	@printf "  %-18s %s\n" "export-state" "Run xian-export-state with ARGS='...'"

sync:
	$(UV) sync --group dev

validate:
	./scripts/validate-repo.sh

wipe:
	rm -rf "$(COMETBFT_HOME)/xian"
	$(COMETBFT_BIN) unsafe-reset-all

reset: wipe init

init:
	$(COMETBFT_BIN) init

node-id:
	$(COMETBFT_BIN) show-node-id

run-abci:
	$(UV) run xian-abci

run-cometbft:
	$(COMETBFT_BIN) node --rpc.laddr "$(RPC_LADDR)"

run-dashboard:
	$(UV) run xian-dashboard

configure-node:
	$(UV) run xian-configure-node $(ARGS)

export-state:
	$(UV) run xian-export-state $(ARGS)
