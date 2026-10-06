import base64
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, call

import pytest
from cantok import CancellationError, SimpleToken
from locklib import LockTraceWrapper, SmartLock

from throngfission import FissionManager, Settings
from throngfission.errors import IsolateClosedError
from throngfission.isolate import RunResult


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
    monkeypatch.setattr('throngfission.manager.Cluster', factory)
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
    monkeypatch.setattr('throngfission.manager.Cluster', Mock(return_value=cluster))
    factory = Mock()
    monkeypatch.setattr('throngfission.manager.FissionIsolate', factory)
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
    monkeypatch.setattr('throngfission.manager.Cluster', Mock(return_value=cluster))
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
    monkeypatch.setattr('throngfission.manager.Cluster', Mock(return_value=cluster))
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
    monkeypatch.setattr('throngfission.manager.Cluster', factory)
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
    allocate.assert_called_once_with(b'archive', token)
    isolate.kill.assert_called_once()
    if method == 'run':
        isolate.run.assert_called_once_with('command', token)
    else:
        isolate.chain.assert_called_once_with('command', token=token)


@pytest.mark.parametrize('cancel_at', ['init', 'upload', 'init_done'])
def test_cancelled_initialization_releases_the_allocated_pod(monkeypatch, cancel_at):
    """Cancellation between transfer steps stops further requests and releases the pod."""
    token = SimpleToken()
    connection = Mock(request=Mock(side_effect=lambda message: token.cancel() if message['op'] == cancel_at else {}))
    cluster = Mock(connect=Mock(return_value=(connection, 'worker', 'uid')))
    monkeypatch.setattr('throngfission.manager.Cluster', Mock(return_value=cluster))
    factory = Mock()
    monkeypatch.setattr('throngfission.manager.FissionIsolate', factory)
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
    monkeypatch.setattr('throngfission.manager.Cluster', Mock(return_value=cluster))
    manager = FissionManager(tmp_path)
    assert manager.read()
    manager.uninstall()
    cluster.uninstall.assert_called_once()
