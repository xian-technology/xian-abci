"""The node's public native path must not load the retired Python engine."""

import subprocess
import sys

NATIVE_NODE_PROBE = """
import builtins
import importlib.abc
import sys

legacy_modules = {
    'contracting.execution.executor',
    'contracting.execution.module',
    'contracting.execution.python_tracer',
    'contracting.execution.tracer',
    'contracting.execution.tracer_common',
    'contracting.execution.sandbox',
    # The Python per-transaction host, deleted in increment 9.
    'contracting.execution.runtime',
    'contracting.stdlib.bridge.random',
    'xian.rewards',
}

class RejectPythonEngine(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == name or fullname.startswith(name + '.') for name in legacy_modules):
            raise AssertionError('native node imported retired Python machinery: ' + fullname)

sys.meta_path.insert(0, RejectPythonEngine())
original_import = builtins.__import__
original_trace = sys.gettrace()
original_profile = sys.getprofile()

import xian.xian_abci
from contracting.local import ContractingClient
from xian.execution_engine import build_vm_runtime

assert build_vm_runtime().mode == 'xian_vm_v2'
source = '''counter = Variable()

@construct
def seed():
    counter.set(0)

@export
def bump():
    counter.set(counter.get() + 1)
    return counter.get()

@export
def fail():
    counter.set(999)
    assert False, 'rollback'
'''
with ContractingClient(storage_home=sys.argv[1], metering=False) as client:
    assert client.raw_driver.vm_profile == 'xian_vm_v2'
    client.submit(source, name='con_native_independence', signer='alice')
    contract = client.get_contract_proxy('con_native_independence')
    assert contract.bump(signer='alice') == 1
    client.raw_driver.commit()
    failed = client.executor.execute(
        sender='alice', contract_name='con_native_independence',
        function_name='fail', kwargs={}, metering=False,
    )
    assert failed['status_code'] == 1, failed
    assert failed['writes'] == {}, failed
    assert failed['events'] == [], failed
    assert client.raw_driver.get('con_native_independence.counter') == 1
    assert contract.bump(signer='alice') == 2

assert not legacy_modules.intersection(sys.modules)
assert builtins.__import__ is original_import
assert sys.gettrace() is original_trace
assert sys.getprofile() is original_profile
"""


def test_native_node_deployment_and_rollback_do_not_load_python_executor(tmp_path):
    result = subprocess.run(
        [sys.executable, "-c", NATIVE_NODE_PROBE, str(tmp_path / "storage")],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
