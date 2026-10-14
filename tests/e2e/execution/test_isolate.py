from io import BytesIO
from tarfile import open as open_tar
from time import sleep

import pytest
from cantok import TimeoutToken

from throngfission.execution.errors import IsolateClosedError


def test_workspace_and_installed_packages_persist(isolate):
    """Files and pip installations survive commands within the same isolate."""
    isolate.install('pytest==8.3.5')
    assert isolate.run('sh -c "printf \'def test_example(): assert 2 + 2 == 4\\n\' > test_example.py"').success
    result = isolate.run('python -m pytest -q')
    assert result.success, result.stderr + result.stdout
    assert '1 passed' in result.stdout
    result = isolate.run('sh -c "printf error >&2; exit 7"')
    assert result.returncode == 7
    assert result.stderr == 'error'
    with open_tar(fileobj=BytesIO(isolate.read())) as archive:
        assert archive.extractfile('test_example.py').read() == b'def test_example(): assert 2 + 2 == 4\n'
        assert not any(name.startswith('venv/') for name in archive.getnames())


def test_commands_outlive_transport_timeouts(isolate):
    """A command survives RPC timeouts, session leases and Fission's idle reaper."""
    result = isolate.run('python -c "import time; time.sleep(240); print(42)"')
    assert result.success
    assert result.stdout == '42\n'


def test_idle_isolate_survives_its_session_lease(isolate, manager):
    """Heartbeats preserve a retained workspace even when no commands are running."""
    assert isolate.run('sh -c "printf retained > marker"').success
    sleep(manager.settings.session_timeout + 2 * manager.settings.heartbeat_interval)
    assert isolate.run('cat marker').stdout == 'retained'


def test_live_isolates_have_independent_pods_and_files(isolate, manager):
    """Simultaneously retained isolates never share workspace state or pod identity."""
    second = manager.get(manager.read(), TimeoutToken(180))
    try:
        assert second.uid != isolate.uid
        assert isolate.run('sh -c "printf private > marker"').success
        assert second.run('python -c "from pathlib import Path; print(Path(\'marker\').exists())"').stdout == 'False\n'
        assert isolate.run('cat marker').stdout == 'private'
    finally:
        second.kill()


def test_deleted_pod_invalidates_the_isolate(isolate, manager):
    """A vanished worker causes a session error without allocating a replacement."""
    manager.cluster.delete_pod(isolate.pod, isolate.uid)
    with pytest.raises(IsolateClosedError):
        isolate.run('true')
    assert not manager.cluster.api.request('GET', f'{manager.cluster.core}pods', query=[('labelSelector', f'throngfission.io/installation={manager.settings.function_name},managed=false')])['items']
