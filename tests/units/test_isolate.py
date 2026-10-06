import base64
import gc
import weakref
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import Mock

import pytest
from cantok import SimpleToken
from locklib import LockTraceWrapper, SmartLock
from throng.errors import CannotInstallDependencyError

from throngfission.connection import Connection
from throngfission.errors import IsolateClosedError
from throngfission.isolate import FissionIsolate, RunResult, heartbeat
from throngfission.settings import Settings


@pytest.mark.parametrize('cancelled', [False, True])
def test_command_cancellation_preserves_output(monkeypatch, cancelled):
    """A cancelled command stops once and retains the worker's captured output."""
    monkeypatch.setattr('throngfission.isolate.Thread', Mock())
    token = SimpleToken()
    lock = LockTraceWrapper(SmartLock())
    connection = Mock(request=Mock(side_effect=lambda message: (lock.notify(message['op']), token.cancel() if cancelled else None, {'done': True, 'returncode': -9 if cancelled else 0, 'stdout': 'out', 'stderr': 'err'})[-1]))
    isolate = FissionIsolate(Mock(settings=Settings()), connection, 'worker', 'uid')
    isolate.lock = lock
    result = isolate.run('python -c "print(1)"', token)
    assert result == RunResult(success=not cancelled, returncode=-9 if cancelled else 0, stdout='out', stderr='err', killed_by_token=cancelled)
    assert connection.request.call_args_list[0].args == ({'op': 'start', 'argv': ['python', '-c', 'print(1)']},)
    assert lock.was_event_locked('start')
    assert lock.was_event_locked('poll')
    if cancelled:
        assert lock.was_event_locked('cancel')
    isolate.kill()


def test_cancel_before_run_does_not_send_command(monkeypatch):
    """An already cancelled token cannot start a process remotely."""
    monkeypatch.setattr('throngfission.isolate.Thread', Mock())
    connection = Mock()
    isolate = FissionIsolate(Mock(settings=Settings()), connection, 'worker', 'uid')
    isolate.lock = LockTraceWrapper(SmartLock())
    assert isolate.run('python', SimpleToken(cancelled=True)).killed_by_token
    connection.request.assert_not_called()
    isolate.kill()


def test_running_command_polls_without_restarting(monkeypatch):
    """A long command is polled repeatedly but submitted only once."""
    monkeypatch.setattr('throngfission.isolate.Thread', Mock())
    connection = Mock(request=Mock(side_effect=[{}, {'done': False}, {'done': True, 'returncode': 2, 'stdout': '', 'stderr': 'failed'}]))
    isolate = FissionIsolate(Mock(settings=Settings()), connection, 'worker', 'uid')
    isolate.lock = LockTraceWrapper(SmartLock())
    isolate.stopped = Mock(is_set=Mock(return_value=False))
    assert isolate.run('false').returncode == 2
    isolate.stopped.wait.assert_called_once_with(0.1)
    assert [call.args[0]['op'] for call in connection.request.call_args_list] == ['start', 'poll', 'poll']
    isolate.kill()


@pytest.mark.parametrize('chunks', [[], [b'archive'], [b'a', b'rch', b'ive']])
def test_read_serializes_with_commands(monkeypatch, chunks):
    """The entire download holds the operation lock and preserves chunk order."""
    monkeypatch.setattr('throngfission.isolate.Thread', Mock())
    lock = LockTraceWrapper(SmartLock())
    replies = iter([{}, *[{'state': base64.b64encode(chunk).decode()} for chunk in chunks], {'state': ''}])
    connection = Mock(request=Mock(side_effect=lambda message: (lock.notify(message['op']), next(replies))[1]))
    isolate = FissionIsolate(Mock(settings=Settings()), connection, 'worker', 'uid')
    isolate.lock = lock
    assert isolate.read() == b''.join(chunks)
    assert [call.args[0]['op'] for call in connection.request.call_args_list] == ['read', *(['download'] * (len(chunks) + 1))]
    assert lock.was_event_locked('read')
    assert lock.was_event_locked('download')
    isolate.kill()


def test_interrupted_download_never_returns_a_partial_snapshot(monkeypatch):
    """Losing a later chunk releases the operation lock and propagates the failure."""
    monkeypatch.setattr('throngfission.isolate.Thread', Mock())
    lock = LockTraceWrapper(SmartLock())
    connection = Mock(request=Mock(side_effect=[{}, {'state': 'YWJj'}, IsolateClosedError('lost')]))
    isolate = FissionIsolate(Mock(settings=Settings()), connection, 'worker', 'uid')
    isolate.lock = lock
    try:
        with pytest.raises(IsolateClosedError, match='lost'):
            isolate.read()
        lock.notify('failed')
        assert not lock.was_event_locked('failed')
        assert connection.request.call_count == 3
    finally:
        isolate.kill()


@pytest.mark.parametrize(('packages', 'success'), [((), True), (('pytest',), True), (('one', 'two'), False)])
def test_install_preserves_package_arguments(monkeypatch, packages, success):
    """pip receives package arguments directly and reports installation failures."""
    monkeypatch.setattr('throngfission.isolate.Thread', Mock())
    isolate = FissionIsolate(Mock(settings=Settings()), Mock(), 'worker', 'uid')
    execute = Mock(return_value=RunResult(success=success))
    monkeypatch.setattr(isolate, 'execute', execute)
    if success:
        isolate.install(*packages)
    else:
        with pytest.raises(CannotInstallDependencyError):
            isolate.install(*packages)
    if packages:
        assert execute.call_args.args[0] == ['python', '-m', 'pip', 'install', *packages]
    else:
        execute.assert_not_called()
    isolate.kill()


def test_concurrent_kill_deletes_once(monkeypatch):
    """Concurrent cleanup calls interrupt I/O and delete the pod exactly once."""
    monkeypatch.setattr('throngfission.isolate.Thread', Mock())
    lock = LockTraceWrapper(SmartLock())
    cluster = Mock(settings=Settings(), delete_pod=Mock(side_effect=lambda *_: lock.notify('delete')))
    isolate = FissionIsolate(cluster, Mock(), 'worker', 'uid')
    isolate.cleanup_lock = lock
    with ThreadPoolExecutor(max_workers=3) as executor:
        list(executor.map(lambda _: isolate.kill(), range(3)))
    cluster.delete_pod.assert_called_once_with('worker', 'uid')
    assert lock.was_event_locked('delete')
    with pytest.raises(IsolateClosedError):
        isolate.active()


def test_partial_initialization_can_be_disposed():
    """Finalization is safe even before a connection has been assigned."""
    isolate = FissionIsolate.__new__(FissionIsolate)
    isolate.kill()
    isolate.__del__()
    with pytest.raises(IsolateClosedError):
        isolate.active()


def test_heartbeat_stops_after_connection_failure():
    """A lost heartbeat invalidates the session and closes its socket."""
    stopped = Event()
    lock = LockTraceWrapper(SmartLock())
    socket = Mock(send=Mock(side_effect=lambda _: lock.notify('send')), recv=Mock(side_effect=['{}', '']), abort=Mock(side_effect=lambda: lock.notify('abort')), close=Mock(side_effect=lambda **_: lock.notify('close')))
    connection = Connection(socket)
    connection.lock = lock
    heartbeat(connection, stopped, 0.001)
    assert stopped.is_set()
    assert socket.send.call_count == 2
    assert lock.was_event_locked('send')
    assert not lock.was_event_locked('abort')
    assert lock.was_event_locked('close')


def test_heartbeat_does_not_keep_an_abandoned_isolate_alive():
    """Dropping the last isolate reference stops its real heartbeat thread."""
    lock = LockTraceWrapper(SmartLock())
    socket = Mock(recv=Mock(return_value='{}'), abort=Mock(side_effect=lambda: lock.notify('abort')), close=Mock(side_effect=lambda **_: lock.notify('close')))
    connection = Connection(socket)
    connection.lock = lock
    isolate = FissionIsolate(Mock(settings=Settings(heartbeat_interval=0.001)), connection, 'worker', 'uid')
    reference = weakref.ref(isolate)
    thread = isolate.thread
    del isolate
    gc.collect()
    thread.join(timeout=1)
    assert reference() is None
    assert not thread.is_alive()
    assert not lock.was_event_locked('abort')
    assert lock.was_event_locked('close')


def test_kill_interrupts_a_running_operation(monkeypatch):
    """Cleanup never needs the operation lock held by a blocked remote command."""
    monkeypatch.setattr('throngfission.isolate.Thread', Mock())
    receiving, interrupted = Event(), Event()
    io_lock = LockTraceWrapper(SmartLock())
    operation_lock = LockTraceWrapper(SmartLock())
    socket = Mock(recv=Mock(side_effect=lambda: (operation_lock.notify('receive'), receiving.set(), interrupted.wait(10), '')[-1]), abort=Mock(side_effect=lambda: (io_lock.notify('abort'), interrupted.set())))
    connection = Connection(socket)
    connection.lock = io_lock
    isolate = FissionIsolate(Mock(settings=Settings()), connection, 'worker', 'uid')
    isolate.lock = operation_lock
    isolate.cleanup_lock = LockTraceWrapper(SmartLock())
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(isolate.run, 'python')
        assert receiving.wait(5)
        isolate.kill()
        with pytest.raises(IsolateClosedError):
            pending.result(timeout=5)
    assert operation_lock.was_event_locked('receive')
    assert not io_lock.was_event_locked('abort')
    isolate.cluster.delete_pod.assert_called_once_with('worker', 'uid')
