import base64
import json
import os
from contextlib import nullcontext
from io import BytesIO
from unittest.mock import Mock

import pytest

from throngfission.execution.archive import read_directory
from throngfission.execution.worker import runtime
from throngfission.execution.worker.runtime import Session, main
from throngfission.execution.worker.settings import WorkerSettings


@pytest.mark.parametrize('chunk_size', [2048, 4097])
def test_initialize_and_snapshot(tmp_path, monkeypatch, chunk_size):
    """Disk-backed transfers preserve TAR bytes across exact and partial chunk boundaries."""
    (tmp_path / 'source.py').write_text('print(1)')
    state = read_directory(tmp_path)
    builder = Mock()
    monkeypatch.setattr('throngfission.execution.worker.runtime.venv.EnvBuilder', Mock(return_value=builder))
    session = Session(chunk_size)
    try:
        assert session.dispatch({'op': 'ping'}) == {}
        assert session.dispatch({'op': 'init', 'size': len(state), 'exclude': ['ignored']}) == {}
        upload = session.snapshot
        for offset in range(0, len(state), chunk_size):
            assert session.dispatch({'op': 'upload', 'state': base64.b64encode(state[offset:offset + chunk_size]).decode()}) == {}
        builder.create.assert_not_called()
        assert not (session.path / 'source.py').exists()
        assert session.dispatch({'op': 'init_done'}) == {}
        assert upload.closed
        builder.create.assert_called_once_with(session.environment)
        assert (session.path / 'source.py').read_text() == 'print(1)'
        (session.path / 'ignored').write_text('excluded')
        assert session.dispatch({'op': 'read'}) == {}
        download = session.snapshot
        chunks = []
        while chunk := base64.b64decode(session.dispatch({'op': 'download'})['state']):
            assert len(chunk) <= chunk_size
            chunks.append(chunk)
        assert b''.join(chunks) == state
        assert download.closed
        assert session.snapshot is None
        with pytest.raises(ValueError, match='already been initialized'):
            session.dispatch({'op': 'init'})
    finally:
        session.close()
    assert not session.root.exists()


@pytest.mark.parametrize(('initialized', 'message', 'error'), [
    (False, {'op': 'init', 'size': -1}, 'non-negative integer'),
    (False, {'op': 'init', 'size': '1'}, 'non-negative integer'),
    (False, {'op': 'upload'}, 'Begin a workspace upload'),
    (False, {'op': 'init_done'}, 'Begin a workspace upload'),
    (True, {'op': 'upload'}, 'Begin a workspace upload'),
    (True, {'op': 'download'}, 'Begin a snapshot download'),
])
def test_invalid_transfer_state(initialized, message, error):
    """Transfers require valid sizes and the matching initialization phase."""
    session = Session(3)
    session.initialized = initialized
    try:
        with pytest.raises(ValueError, match=error):
            session.dispatch(message)
        assert session.snapshot is None
    finally:
        session.close()


@pytest.mark.parametrize(('size', 'message', 'error'), [
    (4, {'op': 'init', 'size': 4}, 'already in progress'),
    (4, {'op': 'upload', 'state': '!!!!'}, 'base64'),
    (4, {'op': 'upload', 'state': 'YWJjZA=='}, 'chunk size'),
    (2, {'op': 'upload', 'state': 'YWJj'}, 'snapshot size'),
    (4, {'op': 'init_done'}, 'incomplete'),
    (0, {'op': 'init_done'}, r'empty file|file could not be opened'),
])
def test_failed_upload_closes_its_spool(size, message, error):
    """Malformed or incomplete uploads cannot initialize and their spool is released."""
    session = Session(3)
    session.dispatch({'op': 'init', 'size': size})
    stream = session.snapshot
    try:
        with pytest.raises((ValueError, runtime.tarfile.TarError), match=error):
            session.dispatch(message)
        assert not session.initialized
    finally:
        session.close()
    assert stream.closed
    assert not session.root.exists()


def test_interrupted_download_closes_its_spool():
    """An unfinished download rejects replacement and closes its temporary file on exit."""
    session = Session(3)
    session.initialized = True
    try:
        session.dispatch({'op': 'read'})
        stream = session.snapshot
        with pytest.raises(ValueError, match='already in progress'):
            session.dispatch({'op': 'read'})
        assert session.dispatch({'op': 'ping'}) == {}
        assert len(base64.b64decode(session.dispatch({'op': 'download'})['state'])) == 3
    finally:
        session.close()
    assert stream.closed
    assert not session.root.exists()


def test_upload_renews_the_lease_between_chunks(monkeypatch):
    """Each acknowledged chunk keeps an upload alive before heartbeats are started."""
    monkeypatch.setattr(runtime, 'monotonic', Mock(side_effect=[0, 10, 20, 30]))
    session = Session(3)
    try:
        session.dispatch({'op': 'init', 'size': 6})
        assert session.last_seen == 10
        session.dispatch({'op': 'upload', 'state': 'YWJj'})
        assert session.last_seen == 20
        session.dispatch({'op': 'upload', 'state': 'ZGVm'})
        assert session.last_seen == 30
    finally:
        session.close()


@pytest.mark.parametrize(('initialized', 'operation', 'message'), [(False, 'read', 'Initialize'), (True, 'unknown', 'Unknown operation')])
def test_invalid_operation_state(initialized, operation, message):
    """Operations cannot bypass initialization or invoke unknown handlers."""
    session = Session(4096)
    session.initialized = initialized
    try:
        with pytest.raises(ValueError, match=message):
            session.dispatch({'op': operation})
    finally:
        session.close()


def test_command_process_and_output(monkeypatch):
    """Commands use a private venv, process group and separate stdout/stderr files."""
    process = Mock(pid=123, poll=Mock(side_effect=[None, 7]))
    popen = Mock(return_value=process)
    monkeypatch.setattr('throngfission.execution.worker.runtime.subprocess.Popen', popen)
    kill = Mock()
    monkeypatch.setattr('throngfission.execution.worker.runtime.os.killpg', kill, raising=False)
    monkeypatch.setattr('throngfission.execution.worker.runtime.signal.SIGKILL', 9, raising=False)
    monkeypatch.setenv('THRONGFISSION_SECRET', 'hidden')
    monkeypatch.setenv('PATH', '/custom path/bin')
    session = Session(4096)
    session.initialized = True
    try:
        session.dispatch({'op': 'start', 'argv': ['python', 'program.py']})
        options = popen.call_args.kwargs
        assert options['start_new_session'] is True
        assert options['cwd'] == session.path
        assert options['env']['VIRTUAL_ENV'] == str(session.environment)
        assert options['env']['PATH'].split(os.pathsep) == [str(session.environment / 'bin'), '/custom path/bin']
        assert 'THRONGFISSION_SECRET' not in options['env']
        options['stdout'].write(b'output\xff')
        options['stderr'].write(b'error')
        assert session.dispatch({'op': 'poll'}) == {'done': False}
        assert session.dispatch({'op': 'poll'}) == {'done': True, 'returncode': 7, 'stdout': 'output\ufffd', 'stderr': 'error'}
        assert session.output == []
        assert session.process is None
        kill.assert_called_once_with(123, 9)
    finally:
        session.close()


def test_cancel_kills_process_group(monkeypatch):
    """Cancellation kills descendants and cleanup closes every output handle."""
    kill = Mock(side_effect=ProcessLookupError)
    monkeypatch.setattr('throngfission.execution.worker.runtime.os.killpg', kill, raising=False)
    monkeypatch.setattr('throngfission.execution.worker.runtime.signal.SIGKILL', 9, raising=False)
    session = Session(4096)
    session.initialized = True
    session.process = Mock(pid=123)
    stream = BytesIO()
    session.output = [stream]
    session.dispatch({'op': 'cancel'})
    session.close()
    assert session.closed
    assert stream.closed
    kill.assert_called_once_with(123, 9)
    session.process.wait.assert_called_once()


def test_process_state_errors():
    """Polling an absent command and starting a second command are rejected."""
    session = Session(4096)
    try:
        with pytest.raises(ValueError, match='no running command'):
            session.poll()
        session.process = Mock()
        with pytest.raises(ValueError, match='already running'):
            session.start(['python'])
        session.process = None
    finally:
        session.close()


@pytest.mark.parametrize(('active', 'closed'), [(False, False), (True, False), (True, True)])
def test_authenticated_health(monkeypatch, active, closed):
    """Janitor health distinguishes unused, live and closed worker sessions."""
    monkeypatch.setattr(runtime, 'session', Mock(closed=closed, last_seen=10) if active else None)
    monkeypatch.setattr(runtime, 'monotonic', lambda: 20)
    monkeypatch.setattr(runtime, 'import_module', Mock(return_value=Mock(request=Mock(headers={'X-Throngfission-Token': 'secret'}))))
    monkeypatch.setattr(WorkerSettings, 'from_env', Mock(return_value=WorkerSettings(secret='secret')))
    assert json.loads(main()) == {'closed': closed, 'age': 10 if active else None}


def test_wrong_credential_is_rejected(monkeypatch):
    """Unauthenticated callers cannot inspect or open a worker session."""
    monkeypatch.setattr(runtime, 'import_module', Mock(return_value=Mock(request=Mock(headers={}))))
    monkeypatch.setattr(WorkerSettings, 'from_env', Mock(return_value=WorkerSettings(secret='secret')))
    with pytest.raises(PermissionError, match='credential'):
        main()


def test_worker_accepts_only_one_session(monkeypatch):
    """A specialized pod never accepts another isolate after its first session."""
    monkeypatch.setattr(runtime, 'session', Mock())
    monkeypatch.setattr(runtime, 'import_module', Mock(return_value=Mock(request=Mock(headers={'X-Throngfission-Token': 'secret'}))))
    monkeypatch.setattr(WorkerSettings, 'from_env', Mock(return_value=WorkerSettings(secret='secret')))
    socket = Mock()
    assert main(socket) == ''
    socket.close.assert_called_once()


@pytest.mark.parametrize('ending', [None, '{"op":"close"}', '{"op":"bad"}'])
def test_session_loop_always_cleans_up(monkeypatch, ending):
    """EOF, explicit close and operation failure all close the workspace and socket."""
    monkeypatch.setattr(runtime, 'session', None)
    session = Mock(dispatch=Mock(side_effect=[{}, ValueError('invalid')]))
    monkeypatch.setattr(runtime, 'Session', Mock(return_value=session))
    monkeypatch.setattr(runtime, 'import_module', Mock(side_effect=[Mock(request=Mock(headers={'X-Throngfission-Token': 'secret'})), Mock(Timeout=Mock(return_value=nullcontext()))]))
    monkeypatch.setattr(WorkerSettings, 'from_env', Mock(return_value=WorkerSettings(secret='secret', pod_name='worker', pod_uid='uid')))
    socket = Mock(receive=Mock(side_effect=['{"op":"ping"}', ending]))
    assert main(socket) == ''
    assert json.loads(socket.send.call_args_list[0].args[0]) == {'protocol': 2, 'pod': 'worker', 'uid': 'uid'}
    socket.set_max_message_length.assert_called_once_with(10 * 1024 * 1024)
    if ending == '{"op":"bad"}':
        assert json.loads(socket.send.call_args.args[0]) == {'error': 'invalid'}
    session.close.assert_called_once()
    socket.close.assert_called_once()
