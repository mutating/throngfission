from zipfile import ZipFile

import pytest
from cantok import TimeoutToken
from throng.errors import (
    CannotInstallDependencyError,
    InterruptedInstallationError,
    NotSuccessfulRunError,
    PreparationCommandFailedError,
)


def test_manager_runs_and_cleans_up(manager):
    """A complete manager run executes on Fission and releases its worker pod."""
    result = manager.run('python -c "print(42)"', TimeoutToken(180))
    assert result.success
    assert result.stdout == '42\n'
    assert not manager.cluster.api.request('GET', f'{manager.cluster.core}pods', query=[('labelSelector', f'throngfission.io/installation={manager.settings.function_name},managed=false')])['items']


def test_chain_shares_state_then_releases_its_worker(manager):
    """Manager chains preserve command state and release the pod after a failure."""
    results = manager.chain('sh -c "printf retained > marker"', 'cat marker', 'sh -c "exit 7"', token=TimeoutToken(180))
    assert [result.returncode for result in results] == [0, 0, 7]
    assert results[1].stdout == 'retained'
    assert not manager.cluster.api.request('GET', f'{manager.cluster.core}pods', query=[('labelSelector', f'throngfission.io/installation={manager.settings.function_name},managed=false')])['items']


@pytest.mark.parametrize('method', ['get', 'scope', 'run', 'chain'])
def test_packages_and_preparation_run_before_remote_commands(manager, method):
    """A local wheel is installed without an index before preparation imports it."""
    files = {
        'prepared_dependency.py': 'value = "installed"\n',
        'prepared_dependency-1.0.dist-info/METADATA': 'Metadata-Version: 2.1\nName: prepared-dependency\nVersion: 1.0\n',
        'prepared_dependency-1.0.dist-info/WHEEL': 'Wheel-Version: 1.0\nGenerator: throngfission-tests\nRoot-Is-Purelib: true\nTag: py3-none-any\n',
        'prepared_dependency-1.0.dist-info/RECORD': '',
    }
    files['prepared_dependency-1.0.dist-info/RECORD'] = ''.join(f'{name},,\n' for name in files)
    wheel = 'prepared_dependency-1.0-py3-none-any.whl'
    with ZipFile(manager.path / wheel, 'w') as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    manager.packages = ['--no-index', f'./{wheel}']
    manager.prepare = ['python -c "from prepared_dependency import value; from pathlib import Path; Path(\'marker\').write_text(value)"']
    token = TimeoutToken(180)
    if method == 'get':
        isolate = manager.get(manager.read(), token)
        try:
            result = isolate.run('cat marker', token=token, exception=True)
        finally:
            isolate.kill()
    elif method == 'scope':
        with manager.scope(token=token) as isolate:
            result = isolate.run('cat marker', token=token, exception=True)
    elif method == 'run':
        result = manager.run('cat marker', token=token, exception=True)
    else:
        results = manager.chain('cat marker', 'cat marker', token=token, exception=True)
        assert len(results) == 2
        result = results[-1]
    assert result.stdout == 'installed'
    assert not manager.cluster.api.request('GET', f'{manager.cluster.core}pods', query=[('labelSelector', f'throngfission.io/installation={manager.settings.function_name},managed=false')])['items']


@pytest.mark.parametrize('stage', ['installation', 'preparation'])
def test_setup_failure_releases_remote_worker(manager, stage):
    """Failed automatic pip/setup commands retain output and cannot leave a pod behind."""
    if stage == 'installation':
        manager.packages = ['--no-index', './missing.whl']
    manager.prepare = ['sh -c "printf setup-error >&2; exit 7"']
    wrapper = InterruptedInstallationError if stage == 'installation' else PreparationCommandFailedError
    cause_type = CannotInstallDependencyError if stage == 'installation' else NotSuccessfulRunError
    with pytest.raises(wrapper) as caught:
        manager.chain('true', token=TimeoutToken(180), exception=False)
    assert isinstance(caught.value.__cause__, cause_type)
    assert caught.value.__cause__.result.stderr
    assert not manager.cluster.api.request('GET', f'{manager.cluster.core}pods', query=[('labelSelector', f'throngfission.io/installation={manager.settings.function_name},managed=false')])['items']


@pytest.mark.parametrize('method', ['run', 'chain'])
def test_exception_mode_releases_remote_worker(manager, method):
    """Native throng exception handling exposes captured output after remote cleanup."""
    with pytest.raises(NotSuccessfulRunError) as caught:
        getattr(manager, method)('sh -c "printf failed >&2; exit 7"', token=TimeoutToken(180), exception=True)
    assert caught.value.result.returncode == 7
    assert caught.value.result.stderr == 'failed'
    assert not manager.cluster.api.request('GET', f'{manager.cluster.core}pods', query=[('labelSelector', f'throngfission.io/installation={manager.settings.function_name},managed=false')])['items']
