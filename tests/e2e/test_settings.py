import shlex

import pytest
from cantok import TimeoutToken
from locklib import LockTraceWrapper, SmartLock

from throngfission import Settings


@pytest.mark.parametrize(('image', 'distribution', 'python_version'), [('python', 'alpine', '3.13'), ('debian', 'debian', '3.13'), ('ubuntu', 'ubuntu', '3.12')])
def test_image_presets_run_python_shell_and_pip(manager, monkeypatch, image, distribution, python_version):
    """Each skelet preset boots its intended OS and runs shell commands and installed Python packages."""
    monkeypatch.setenv('THRONGFISSION_IMAGE', image)
    manager.settings.image = Settings.from_env().image
    isolate = manager.get(manager.read(), TimeoutToken(180))
    isolate.lock = LockTraceWrapper(SmartLock())
    isolate.cleanup_lock = LockTraceWrapper(SmartLock())
    isolate.connection.lock = LockTraceWrapper(SmartLock())
    try:
        result = isolate.run('sh -c ' + shlex.quote('. /etc/os-release; printf "%s\\n" "$ID"; python -c "import sys; print(str(sys.version_info.major) + \'.\' + str(sys.version_info.minor))"'), TimeoutToken(30))
        assert result.success, result.stderr
        assert result.stdout.splitlines() == [distribution, python_version]
        isolate.install('skelet==0.0.21')
        assert isolate.run('python -c "from skelet import Storage; print(Storage.__name__)"', TimeoutToken(30)).stdout == 'Storage\n'
        pod = manager.cluster.owned_pod(isolate.pod, isolate.uid)
        runtime = next(container for container in pod['spec']['containers'] if container['name'] == manager.settings.function_name)
        assert runtime['image'] == manager.settings.resolved_image
        assert pod['spec']['hostUsers'] is False
    finally:
        isolate.kill()


def test_skelet_overrides_reach_scheduled_workers(manager, monkeypatch):
    """Environment overrides become typed settings and actual worker resource requests."""
    for name in Settings.__annotations__:
        monkeypatch.setenv('THRONGFISSION_' + name.upper(), str(getattr(manager.settings, name)))
    monkeypatch.setenv('THRONGFISSION_CPU_REQUEST', '175m')
    monkeypatch.setenv('THRONGFISSION_MEMORY_REQUEST', '192Mi')
    monkeypatch.setenv('THRONGFISSION_SESSION_TIMEOUT', '45.0')
    monkeypatch.setenv('THRONGFISSION_TRANSFER_CHUNK_SIZE', '2048')
    monkeypatch.setenv('THRONGFISSION_WEBSOCKET_MAX_MESSAGE_SIZE', '4096')
    manager.settings = Settings.from_env()
    isolate = manager.get(manager.read(), TimeoutToken(180))
    try:
        pod = manager.cluster.owned_pod(isolate.pod, isolate.uid)
        runtime = next(container for container in pod['spec']['containers'] if container['name'] == manager.settings.function_name)
        assert runtime['resources']['requests'] == {'cpu': '175m', 'memory': '192Mi'}
        variables = {item['name']: item.get('value') for item in runtime['env']}
        assert variables['THRONGFISSION_SESSION_TIMEOUT'] == '45.0'
        assert variables['THRONGFISSION_TRANSFER_CHUNK_SIZE'] == '2048'
        assert variables['THRONGFISSION_WEBSOCKET_MAX_MESSAGE_SIZE'] == '4096'
        assert isolate.read()
        assert isolate.run('true').success
    finally:
        isolate.kill()
