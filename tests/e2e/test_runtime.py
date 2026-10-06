from shlex import quote

import pytest
from cantok import TimeoutToken


@pytest.mark.parametrize('signal_number', [15, 9])
def test_commands_can_signal_their_own_child_process_groups(isolate, signal_number):
    """Workers can stop separate child sessions even under stacked AppArmor profiles."""
    child = 'import time; print("ready", flush=True); time.sleep(60)'
    parent = f'import os, subprocess, sys; p = subprocess.Popen([sys.executable, "-c", {child!r}], stdout=subprocess.PIPE, start_new_session=True); assert p.stdout.readline() == b"ready\\n"; os.killpg(p.pid, {signal_number}); print(p.wait(timeout=5))'
    result = isolate.run('python -c ' + quote(parent), TimeoutToken(15))
    assert result.success, result.stderr
    assert result.stdout == str(-signal_number) + '\n'


def test_cancellation_kills_descendants_and_preserves_the_session(isolate):
    """Cancelling a parent prevents its child from writing later and permits reuse."""
    child = 'import time; from pathlib import Path; time.sleep(2); Path("escaped").touch()'
    parent = f'import subprocess, sys, time; subprocess.Popen([sys.executable, "-c", {child!r}]); print("started", flush=True); time.sleep(100)'
    result = isolate.run('python -c ' + quote(parent), TimeoutToken(0.5))
    assert result.killed_by_token
    assert result.returncode == -9
    assert result.stdout == 'started\n'
    result = isolate.run('python -c "import time; from pathlib import Path; time.sleep(3); print(Path(\'escaped\').exists())"')
    assert result.success
    assert result.stdout == 'False\n'
