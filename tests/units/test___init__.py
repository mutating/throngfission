import subprocess
import sys


def test_public_imports_do_not_load_cli():
    """The top-level API preserves class identities without loading administration code."""
    program = '''
import sys
from throngfission import FissionIsolate, FissionManager, Settings
from throngfission.execution.isolate import FissionIsolate as Isolate
from throngfission.execution.manager import FissionManager as Manager
from throngfission.execution.settings import Settings as ExecutionSettings
assert (FissionIsolate, FissionManager, Settings) == (Isolate, Manager, ExecutionSettings)
assert not any(name == 'throngfission.cli' or name.startswith('throngfission.cli.') for name in sys.modules)
'''
    result = subprocess.run([sys.executable, '-I', '-c', program], check=False, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
