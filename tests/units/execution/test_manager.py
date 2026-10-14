import base64
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, call

import pytest
from cantok import CancellationError, SimpleToken
from locklib import LockTraceWrapper, SmartLock
from throng.errors import (
    CannotInstallDependencyError,
    InterruptedChainError,
    InterruptedInstallationError,
    NotSuccessfulRunError,
    PreparationCommandFailedError,
)

from throngfission import FissionManager, Settings
from throngfission.execution.errors import IsolateClosedError
from throngfission.execution.isolate import RunResult


def test_explicit_runtime_image_overrides_environment(monkeypatch):
    """An explicitly supplied image takes precedence over the environment configuration."""
    monkeypatch.setenv('THRONGFISSION_IMAGE', 'registry.example.com/runtime:environment')
    manager = FissionManager(settings=Settings(image='registry.example.com/runtime:explicit'))
    assert manager.settings.image == 'registry.example.com/runtime:explicit'
    assert manager._cluster is None


def test_lazy_cluster_is_created_once(monkeypatch):
    """Plugin discovery is offline and concurrent first use creates one client."""
    lock = LockTraceWrapper(SmartLock())
    factory = Mock(side_effect=lambda _: (lock.notify('create'), Mock())[1])
    monkeypatch.setattr('throngfission.execution.manager.Cluster', factory)
    manager = FissionManager(settings=Settings())
    manager.lock = lock
    factory.assert_not_called()
    with ThreadPoolExecutor(max_workers=3) as executor:
        clusters = list(executor.map(lambda _: manager.cluster, range(3)))
    assert all(cluster is clusters[0] for cluster in clusters)
    factory.assert_called_once()
    assert lock.was_event_locked('create')


@pytest.mark.parametrize('state', [b'', b'abc', b'abcdef', b'archive'])
def test_get_initializes_before_returning(monkeypatch, state):
    """Upload uses a longer timeout and restores command timing before handoff."""
    connection = Mock()
    cluster = Mock(connect=Mock(return_value=(connection, 'worker', 'uid')))
    monkeypatch.setattr('throngfission.execution.manager.Cluster', Mock(return_value=cluster))
    factory = Mock()
    monkeypatch.setattr('throngfission.execution.manager.FissionIsolate', factory)
    manager = FissionManager(exclude=['*.pyc'], settings=Settings(transfer_chunk_size=3, upload_timeout=33.0))
    assert manager.get(state) is factory.return_value
    messages = [call.args[0] for call in connection.request.call_args_list]
    assert messages[0] == {'op': 'init', 'size': len(state), 'exclude': ['*.pyc']}
    assert messages[-1] == {'op': 'init_done'}
    chunks = [base64.b64decode(message['state']) for message in messages[1:-1]]
    assert all(message['op'] == 'upload' for message in messages[1:-1])
    assert b''.join(chunks) == state
    assert [len(chunk) for chunk in chunks] == [min(3, len(state) - offset) for offset in range(0, len(state), 3)]
    assert [entry.args for entry in connection.socket.settimeout.call_args_list] == [(33.0,), (30.0,)]
    assert connection.mock_calls[0] == call.socket.settimeout(33.0)
    assert connection.mock_calls[-1] == call.socket.settimeout(30.0)
    factory.assert_called_once_with(cluster, connection, 'worker', 'uid')


@pytest.mark.parametrize('completed_requests', range(5))
def test_failed_initialization_is_cleaned(monkeypatch, completed_requests):
    """A rejected archive cannot leave its allocated pod running."""
    connection = Mock(request=Mock(side_effect=[*([{}] * completed_requests), ValueError('bad archive')]))
    cluster = Mock(connect=Mock(return_value=(connection, 'worker', 'uid')))
    monkeypatch.setattr('throngfission.execution.manager.Cluster', Mock(return_value=cluster))
    with pytest.raises(ValueError, match='bad archive'):
        FissionManager(settings=Settings(transfer_chunk_size=3)).get(b'archive')
    assert connection.request.call_count == completed_requests + 1
    connection.socket.settimeout.assert_called_once_with(45.0)
    connection.close.assert_called_once()
    cluster.delete_pod.assert_called_once_with('worker', 'uid')


def test_lost_connection_during_workspace_upload_is_diagnosed(monkeypatch):
    """Report the init phase and payload size while preserving cleanup and cause."""
    connection = Mock(request=Mock(side_effect=IsolateClosedError('socket closed')))
    cluster = Mock(connect=Mock(return_value=(connection, 'worker', 'uid')))
    monkeypatch.setattr('throngfission.execution.manager.Cluster', Mock(return_value=cluster))
    with pytest.raises(IsolateClosedError) as caught:
        FissionManager().get(b'archive')
    assert 'uploading the workspace during init' in str(caught.value)
    assert '7 TAR bytes, 12 encoded bytes' in str(caught.value)
    assert isinstance(caught.value.__cause__, IsolateClosedError)
    connection.close.assert_called_once()
    cluster.delete_pod.assert_called_once_with('worker', 'uid')


def test_cancelled_get_is_offline(monkeypatch):
    """Cancellation before allocation avoids even constructing a cluster client."""
    factory = Mock()
    monkeypatch.setattr('throngfission.execution.manager.Cluster', factory)
    with pytest.raises(CancellationError):
        FissionManager().get(b'', SimpleToken(cancelled=True))
    factory.assert_not_called()


@pytest.mark.parametrize('method', ['run', 'chain'])
@pytest.mark.parametrize('failed', [False, True])
def test_convenience_methods_propagate_token_and_cleanup(monkeypatch, method, failed):
    """Convenience methods apply cancellation to allocation and always kill the isolate."""
    manager = FissionManager()
    isolate = Mock()
    allocate = Mock(return_value=isolate)
    monkeypatch.setattr(manager, 'read', Mock(return_value=b'archive'))
    monkeypatch.setattr(manager, 'get', allocate)
    token = SimpleToken()
    expected = RunResult(success=True) if method == 'run' else [RunResult(success=True)]
    getattr(isolate, method).return_value = expected
    if failed:
        getattr(isolate, method).side_effect = ValueError('command failed')
        with pytest.raises(ValueError, match='command failed'):
            getattr(manager, method)('command', token=token)
    else:
        assert getattr(manager, method)('command', token=token) is expected
    allocate.assert_called_once_with(b'archive', token=token)
    isolate.kill.assert_called_once()
    if method == 'run':
        isolate.run.assert_called_once_with('command', token=token, exception=False)
    else:
        isolate.chain.assert_called_once_with('command', token=token, exception=False)


@pytest.mark.parametrize('cancel_at', ['init', 'upload', 'init_done'])
def test_cancelled_initialization_releases_the_allocated_pod(monkeypatch, cancel_at):
    """Cancellation between transfer steps stops further requests and releases the pod."""
    token = SimpleToken()
    connection = Mock(request=Mock(side_effect=lambda message: token.cancel() if message['op'] == cancel_at else {}))
    cluster = Mock(connect=Mock(return_value=(connection, 'worker', 'uid')))
    monkeypatch.setattr('throngfission.execution.manager.Cluster', Mock(return_value=cluster))
    factory = Mock()
    monkeypatch.setattr('throngfission.execution.manager.FissionIsolate', factory)
    with pytest.raises(CancellationError):
        FissionManager(settings=Settings(transfer_chunk_size=3)).get(b'archive', token)
    assert connection.request.call_args.args[0]['op'] == cancel_at
    factory.assert_not_called()
    connection.close.assert_called_once()
    cluster.delete_pod.assert_called_once_with('worker', 'uid')


def test_snapshot_and_explicit_uninstall(tmp_path, monkeypatch):
    """Manager snapshots use TAR and uninstall delegates to the shared installation."""
    (tmp_path / 'file').write_text('content')
    cluster = Mock()
    monkeypatch.setattr('throngfission.execution.manager.Cluster', Mock(return_value=cluster))
    manager = FissionManager(tmp_path)
    assert manager.read()
    manager.uninstall()
    cluster.uninstall.assert_called_once()


def run_prepared(manager, method, token, exception=False):
    """Use each public acquisition path and release persistent isolates explicitly."""
    if method == 'get':
        isolate = manager.get(manager.read(), token)
        try:
            return isolate.run('work', token=token, exception=exception)
        finally:
            isolate.kill()
    if method == 'scope':
        with manager.scope(token=token) as isolate:
            return isolate.run('work', token=token, exception=exception)
    if method == 'run':
        return manager.run('work', token=token, exception=exception)
    return manager.chain('work', token=token, exception=exception)[0]


@pytest.mark.parametrize('method', ['get', 'scope', 'run', 'chain'])
@pytest.mark.parametrize(('packages', 'prepare'), [(None, None), ([], []), (['one', 'two'], None), (None, ['prepare first', 'prepare second']), (['one', 'two'], ['prepare first', 'prepare second'])])
def test_every_new_isolate_installs_then_prepares_before_use(worker, method, packages, prepare):
    """Preparation belongs to each isolate, including repeated convenience calls."""
    manager = FissionManager(worker.manager.path, ['ignored'], prepare, packages, settings=worker.cluster.settings)
    token = SimpleToken()
    expected = ([['python', '-m', 'pip', 'install', *packages]] if packages else []) + ([command.split() for command in prepare] if prepare else []) + [['work']]
    for _ in range(2):
        assert run_prepared(manager, method, token).success
    assert worker.commands == expected * 2
    assert worker.cluster.connect.call_args_list == [call(token), call(token)]
    assert worker.cluster.delete_pod.call_args_list == [call('worker', 'uid'), call('worker', 'uid')]
    initializations = [entry.args[0] for entry in worker.connection.request.call_args_list if entry.args[0]['op'] == 'init']
    assert len(initializations) == 2
    assert all(message['exclude'] == ['ignored'] for message in initializations)
    assert manager.packages == packages
    assert manager.prepare == prepare


@pytest.mark.parametrize('method', ['get', 'scope', 'run', 'chain'])
def test_cancelled_acquisition_paths_never_contact_cluster(worker, method):
    """An already cancelled caller cannot allocate or prepare a remote pod."""
    worker.manager.packages = ['package']
    worker.manager.prepare = ['prepare']
    with pytest.raises(CancellationError):
        run_prepared(worker.manager, method, SimpleToken(cancelled=True))
    worker.factory.assert_not_called()
    worker.connection.request.assert_not_called()


@pytest.mark.parametrize('method', ['get', 'scope', 'run', 'chain'])
@pytest.mark.parametrize('stage', ['installation', 'preparation'])
@pytest.mark.parametrize('exception', [False, True, ValueError, ValueError('user policy')])
def test_setup_failure_keeps_cause_and_never_runs_user_command(worker, method, stage, exception):
    """Setup fails independently of the requested user-command exception policy."""
    worker.manager.packages = ['package']
    worker.manager.prepare = ['prepare first', 'prepare second']
    failure = {'done': True, 'returncode': 7, 'stdout': 'partial', 'stderr': 'setup failed'}
    worker.poll.side_effect = [failure] if stage == 'installation' else [worker.poll.return_value, failure]
    error_type = InterruptedInstallationError if stage == 'installation' else PreparationCommandFailedError
    cause_type = CannotInstallDependencyError if stage == 'installation' else NotSuccessfulRunError
    with pytest.raises(error_type) as caught:
        run_prepared(worker.manager, method, SimpleToken(), exception)
    assert isinstance(caught.value.__cause__, cause_type)
    assert caught.value.__cause__.result == RunResult(success=False, returncode=7, stdout='partial', stderr='setup failed')
    expected = [['python', '-m', 'pip', 'install', 'package']]
    assert worker.commands == expected + ([['prepare', 'first']] if stage == 'preparation' else [])
    worker.cluster.delete_pod.assert_called_once_with('worker', 'uid')


@pytest.mark.parametrize('method', ['get', 'scope', 'run', 'chain'])
@pytest.mark.parametrize('stage', ['installation', 'preparation'])
def test_cancellation_during_setup_stops_process_and_cleans_up(worker, method, stage):
    """The original caller token must reach pip and every preparation command."""
    token = SimpleToken()
    worker.manager.packages = ['package'] if stage == 'installation' else None
    worker.manager.prepare = ['prepare first', 'prepare second']

    def cancel():
        token.cancel()
        worker.poll.side_effect = None
        worker.poll.return_value = {'done': True, 'returncode': -9, 'stdout': 'partial', 'stderr': 'cancelled'}
        return {'done': False}

    worker.poll.side_effect = cancel
    error_type = InterruptedInstallationError if stage == 'installation' else PreparationCommandFailedError
    with pytest.raises(error_type) as caught:
        run_prepared(worker.manager, method, token)
    result = caught.value.__cause__.result
    assert isinstance(result, RunResult)
    assert result.killed_by_token
    assert result.returncode == -9
    assert result.stdout == 'partial'
    assert worker.commands == ([['python', '-m', 'pip', 'install', 'package']] if stage == 'installation' else [['prepare', 'first']])
    assert [entry.args[0]['op'] for entry in worker.connection.request.call_args_list].count('cancel') == 1
    worker.cluster.delete_pod.assert_called_once_with('worker', 'uid')


@pytest.mark.parametrize('method', ['get', 'scope', 'run', 'chain'])
def test_cancellation_between_installation_and_preparation_prevents_commands(worker, method):
    """A successful pip result does not conceal cancellation before preparation."""
    token = SimpleToken()
    worker.manager.packages = ['package']
    worker.manager.prepare = ['prepare']
    worker.poll.side_effect = lambda: (token.cancel(), worker.poll.return_value)[1]
    with pytest.raises(PreparationCommandFailedError) as caught:
        run_prepared(worker.manager, method, token)
    assert isinstance(caught.value.__cause__, InterruptedChainError)
    assert worker.commands == [['python', '-m', 'pip', 'install', 'package']]
    worker.cluster.delete_pod.assert_called_once_with('worker', 'uid')


@pytest.mark.parametrize('stage', ['installation', 'preparation'])
@pytest.mark.parametrize('error_type', [IsolateClosedError, KeyboardInterrupt, SystemExit])
def test_setup_transport_errors_and_interrupts_release_the_pod(worker, stage, error_type):
    """Infrastructure failures preserve their cause; process interrupts are re-raised."""
    worker.manager.packages = ['package'] if stage == 'installation' else None
    worker.manager.prepare = ['prepare']
    error = error_type('interrupted')
    worker.poll.side_effect = error
    wrapper = InterruptedInstallationError if stage == 'installation' else PreparationCommandFailedError
    with pytest.raises(wrapper if error_type is IsolateClosedError else error_type) as caught:
        worker.manager.chain('work', token=SimpleToken())
    if error_type is IsolateClosedError:
        assert caught.value.__cause__ is error
    else:
        assert caught.value is error
    assert len(worker.commands) == 1
    worker.cluster.delete_pod.assert_called_once_with('worker', 'uid')


@pytest.mark.parametrize('method', ['run', 'chain'])
@pytest.mark.parametrize('exception', [False, True, ValueError, ValueError('custom')])
def test_user_failure_policy_and_cleanup(worker, method, exception):
    """The inherited methods preserve results or raise, and always delete their pod."""
    worker.poll.side_effect = [{'done': True, 'returncode': 7, 'stdout': 'out', 'stderr': 'failed'}, worker.poll.return_value]
    commands = ['fail'] if method == 'run' else ['fail', 'later']
    if exception is False:
        result = getattr(worker.manager, method)(*commands)
        failure = result if method == 'run' else result[0]
        assert failure.returncode == 7
        assert worker.commands == [[command] for command in commands]
    else:
        error_type = NotSuccessfulRunError if exception is True else ValueError
        with pytest.raises(error_type) as caught:
            getattr(worker.manager, method)(*commands, exception=exception)
        if exception is True:
            assert caught.value.result.stderr == 'failed'
        elif isinstance(exception, BaseException):
            assert caught.value is exception
        assert worker.commands == [['fail']]
    worker.cluster.delete_pod.assert_called_once_with('worker', 'uid')


@pytest.mark.parametrize('method', ['run', 'chain'])
def test_failed_preparation_can_be_retried_with_fresh_token(worker, method):
    """Cancellation of one isolate does not contaminate a later call on the manager."""
    cancelled = SimpleToken()
    worker.manager.prepare = ['prepare first', 'prepare second']
    worker.poll.side_effect = lambda: (cancelled.cancel(), worker.poll.return_value)[1]
    with pytest.raises(PreparationCommandFailedError):
        run_prepared(worker.manager, method, cancelled)
    worker.poll.side_effect = None
    fresh = SimpleToken()
    assert run_prepared(worker.manager, method, fresh).success
    assert worker.commands == [['prepare', 'first'], ['prepare', 'first'], ['prepare', 'second'], ['work']]
    assert worker.cluster.connect.call_args_list == [call(cancelled), call(fresh)]
    assert worker.cluster.delete_pod.call_count == 2
