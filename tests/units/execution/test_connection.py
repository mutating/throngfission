from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import Mock, call

import pytest
from locklib import LockTraceWrapper, SmartLock
from websocket import WebSocketException

from throngfission.execution.connection import Connection
from throngfission.execution.errors import FissionError, IsolateClosedError


@pytest.mark.parametrize('value', ['', b'bytes', None])
def test_eof_is_a_lost_session(value):
    """Absent or binary replies never masquerade as successful operations."""
    connection = Connection(Mock(recv=Mock(return_value=value)))
    with pytest.raises(IsolateClosedError):
        connection.receive()


def test_remote_failure_is_visible():
    """Worker error messages retain their diagnostic text."""
    connection = Connection(Mock(recv=Mock(return_value='{"error":"invalid archive"}')))
    with pytest.raises(FissionError, match='invalid archive'):
        connection.receive()


@pytest.mark.parametrize('value', ['bad JSON', '[]'])
def test_malformed_reply_invalidates_the_protocol(value):
    """Invalid JSON replies become session failures, including during heartbeats."""
    connection = Connection(Mock(recv=Mock(return_value=value)))
    with pytest.raises(IsolateClosedError, match='invalid protocol'):
        connection.receive()


@pytest.mark.parametrize('error', [OSError('offline'), WebSocketException('broken frame')])
def test_transport_failure_is_not_replayed(error):
    """A failed exchange raises once without resending a command."""
    socket = Mock(send=Mock(side_effect=error))
    connection = Connection(socket)
    connection.lock = LockTraceWrapper(SmartLock())
    with pytest.raises(IsolateClosedError):
        connection.request({'op': 'start'})
    with pytest.raises(IsolateClosedError):
        connection.request({'op': 'start'})
    socket.send.assert_called_once()
    socket.recv.assert_not_called()


@pytest.mark.parametrize('error', [OSError('offline'), WebSocketException('closed')])
def test_receive_reports_transport_failure_consistently(error):
    """Handshake and ordinary replies use the same lost-session error type."""
    connection = Connection(Mock(recv=Mock(side_effect=error)))
    with pytest.raises(IsolateClosedError):
        connection.receive()


@pytest.mark.parametrize('reply', ['', 'bad JSON', '{"error":"failed"}'])
def test_failed_exchange_permanently_invalidates_the_connection(reply):
    """No subsequent command can use a session with an ambiguous previous reply."""
    socket = Mock(recv=Mock(return_value=reply))
    connection = Connection(socket)
    connection.lock = LockTraceWrapper(SmartLock())
    with pytest.raises(FissionError):
        connection.request({'op': 'start'})
    with pytest.raises(IsolateClosedError):
        connection.request({'op': 'start'})
    socket.send.assert_called_once()
    socket.recv.assert_called_once()


def test_concurrent_exchanges_hold_one_io_lock():
    """Concurrent heartbeats and commands cannot interleave socket frames."""
    lock = LockTraceWrapper(SmartLock())
    socket = Mock(send=Mock(side_effect=lambda _: lock.notify('send')), recv=Mock(side_effect=lambda: (lock.notify('receive'), '{}')[1]))
    connection = Connection(socket)
    connection.lock = lock
    with ThreadPoolExecutor(max_workers=4) as executor:
        assert list(executor.map(connection.request, [{'op': 'ping'}] * 20)) == [{}] * 20
    assert lock.was_event_locked('send')
    assert lock.was_event_locked('receive')
    assert socket.mock_calls == [call.send('{"op": "ping"}'), call.recv()] * 20


def test_close_interrupts_receive_before_locking():
    """Socket abort runs outside the I/O lock so a blocked receiver can exit."""
    lock = LockTraceWrapper(SmartLock())
    socket = Mock(abort=Mock(side_effect=lambda: lock.notify('abort')), close=Mock(side_effect=lambda **_: lock.notify('close')))
    connection = Connection(socket)
    connection.lock = lock
    connection.close()
    assert not lock.was_event_locked('abort')
    assert lock.was_event_locked('close')


@pytest.mark.parametrize('failure', [OSError('already closed'), AttributeError('socket removed')])
def test_close_tolerates_an_already_aborted_transport(failure):
    """Older websocket-client shutdown races cannot prevent final socket cleanup."""
    lock = LockTraceWrapper(SmartLock())
    socket = Mock(abort=Mock(side_effect=failure), close=Mock(side_effect=lambda **_: lock.notify('close')))
    connection = Connection(socket)
    connection.lock = lock
    connection.close()
    assert lock.was_event_locked('close')
    assert connection.closed


def test_close_unblocks_an_exchange_that_holds_the_io_lock():
    """Concurrent close can abort a receiver without waiting for its held lock."""
    receiving, interrupted = Event(), Event()
    lock = LockTraceWrapper(SmartLock())
    socket = Mock(recv=Mock(side_effect=lambda: (receiving.set(), interrupted.wait(10), '')[-1]), abort=Mock(side_effect=lambda: (lock.notify('abort'), interrupted.set())), close=Mock(side_effect=lambda **_: lock.notify('close')))
    connection = Connection(socket)
    connection.lock = lock
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(connection.request, {'op': 'ping'})
        assert receiving.wait(5)
        connection.close()
        with pytest.raises(IsolateClosedError):
            pending.result(timeout=5)
    assert not lock.was_event_locked('abort')
    assert lock.was_event_locked('close')
