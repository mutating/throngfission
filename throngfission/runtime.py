"""Fission worker; packaged independently of the client and its Kubernetes SDK."""

import base64
import hmac
import json
import os
import signal
import subprocess
import tarfile
import venv
from contextlib import AbstractContextManager
from importlib import import_module
from pathlib import Path
from tempfile import TemporaryDirectory, TemporaryFile
from time import monotonic
from typing import BinaryIO, Dict, List, Optional, Protocol, cast

from throngfission.archive import restore_directory, write_directory
from throngfission.protocol import decode, string
from throngfission.settings import WorkerSettings


class Socket(Protocol):
    def receive(self) -> Optional[str]: ...
    def send(self, value: str) -> None: ...
    def close(self) -> None: ...
    def set_max_message_length(self, length: int) -> None: ...


class Gevent(Protocol):
    def Timeout(self, seconds: float, exception: bool) -> 'AbstractContextManager[None]': ...  # noqa: N802


class Request(Protocol):
    headers: Dict[str, str]


class Flask(Protocol):
    request: Request


class Session:
    """One serialized protocol session owns one workspace and one process group."""

    def __init__(self, chunk_size: int) -> None:
        self.directory = TemporaryDirectory(prefix='throngfission-')
        self.root = Path(self.directory.name)
        self.path = self.root / 'workspace'
        self.path.mkdir()
        self.environment = self.root / 'venv'
        self.process: Optional[subprocess.Popen[bytes]] = None
        self.process_stopped = False
        self.output: List[BinaryIO] = []
        self.exclude: Optional[List[str]] = None
        self.initialized = False
        self.closed = False
        self.last_seen = monotonic()
        self.chunk_size = chunk_size
        self.snapshot: Optional[BinaryIO] = None
        self.remaining = 0

    def dispatch(self, message: Dict[str, object]) -> Dict[str, object]:
        self.last_seen = monotonic()
        operation = string(message, 'op')
        if operation == 'ping':
            return {}
        if operation in ('init', 'upload', 'init_done'):
            self.upload(message)
            return {}
        if not self.initialized:
            raise ValueError('Initialize the workspace before executing operations.')
        if operation in ('read', 'download'):
            return self.download(operation)
        if operation == 'start':
            self.start(cast(List[str], message['argv']))
            return {}
        if operation == 'cancel':
            self.stop_process()
            return {}
        if operation == 'poll':
            return self.poll()
        raise ValueError(f'Unknown operation: {operation}')

    def upload(self, message: Dict[str, object]) -> None:
        operation = string(message, 'op')
        if operation == 'init':
            if self.initialized:
                raise ValueError('The session has already been initialized.')
            if self.snapshot is not None:
                raise ValueError('A snapshot transfer is already in progress.')
            size = message['size']
            if not isinstance(size, int) or size < 0:
                raise ValueError('Snapshot size must be a non-negative integer.')
            self.remaining = size
            self.snapshot = TemporaryFile()  # noqa: SIM115  # Owned until init_done() or close().
            self.exclude = cast(Optional[List[str]], message.get('exclude'))
            return
        if self.initialized or self.snapshot is None:
            raise ValueError('Begin a workspace upload before sending data.')
        if operation == 'upload':
            chunk = base64.b64decode(string(message, 'state'), validate=True)
            if len(chunk) > min(self.chunk_size, self.remaining):
                raise ValueError('Upload exceeds the chunk size or declared snapshot size.')
            self.snapshot.write(chunk)
            self.remaining -= len(chunk)
            return
        if self.remaining:
            raise ValueError('The workspace upload is incomplete.')
        self.snapshot.seek(0)
        restore_directory(self.snapshot, self.path)
        self.snapshot.close()
        self.snapshot = None
        venv.EnvBuilder(with_pip=True).create(self.environment)
        self.initialized = True
        return

    def download(self, operation: str) -> Dict[str, object]:
        if operation == 'read':
            if self.snapshot is not None:
                raise ValueError('A snapshot transfer is already in progress.')
            self.snapshot = TemporaryFile()  # noqa: SIM115  # Owned until download EOF or close().
            write_directory(self.path, self.snapshot, self.exclude)
            self.snapshot.seek(0)
            return {}
        if self.snapshot is None:
            raise ValueError('Begin a snapshot download before requesting data.')
        chunk = self.snapshot.read(self.chunk_size)
        if not chunk:
            self.snapshot.close()
            self.snapshot = None
        return {'state': base64.b64encode(chunk).decode('ascii')}

    def start(self, argv: List[str]) -> None:
        if self.process is not None:
            raise ValueError('A command is already running.')
        self.output = [TemporaryFile(), TemporaryFile()]  # noqa: SIM115  # Owned until poll() or close().
        environment = os.environ.copy()
        environment['PATH'] = str(self.environment / 'bin') + os.pathsep + environment.get('PATH', '')
        environment['VIRTUAL_ENV'] = str(self.environment)
        environment['PIP_CACHE_DIR'] = str(self.root / 'pip-cache')
        environment.pop('THRONGFISSION_SECRET', None)
        self.process = subprocess.Popen(argv, cwd=self.path, env=environment, stdin=subprocess.DEVNULL, stdout=self.output[0], stderr=self.output[1], start_new_session=True)
        self.process_stopped = False

    def stop_process(self) -> None:
        if self.process is not None and not self.process_stopped:
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.process.wait()
            self.process_stopped = True

    def poll(self) -> Dict[str, object]:
        if self.process is None:
            raise ValueError('There is no running command.')
        returncode = self.process.poll()
        if returncode is None:
            return {'done': False}
        self.stop_process()
        result: Dict[str, object] = {'done': True, 'returncode': returncode}
        for name, stream in zip(('stdout', 'stderr'), self.output):
            stream.seek(0)
            result[name] = stream.read().decode('utf-8', errors='replace')
            stream.close()
        self.output = []
        self.process = None
        return result

    def close(self) -> None:
        self.closed = True
        self.stop_process()
        for stream in self.output:
            stream.close()
        self.output = []
        if self.snapshot is not None:
            self.snapshot.close()
            self.snapshot = None
        self.directory.cleanup()


session: Optional[Session] = None


def main(ws: Optional[Socket] = None, clients: object = None) -> str:  # noqa: ARG001
    """Serve a socket or the janitor's authenticated session-status request."""
    global session  # noqa: PLW0603
    flask = cast(Flask, import_module('flask'))
    settings = WorkerSettings.from_env()  # type: ignore[misc]
    supplied = flask.request.headers.get('X-Throngfission-Token', '')
    if not hmac.compare_digest(settings.secret, supplied):
        raise PermissionError('Invalid session credential.')
    if ws is None:
        health: Dict[str, object] = {'closed': session is not None and session.closed, 'age': monotonic() - session.last_seen if session is not None else None}
        return json.dumps(health)
    if session is not None:
        ws.close()
        return ''
    session = Session(settings.transfer_chunk_size)
    gevent = cast(Gevent, import_module('gevent'))
    try:
        ws.set_max_message_length(settings.websocket_max_message_size)
        greeting: Dict[str, object] = {'protocol': 2, 'pod': settings.pod_name, 'uid': settings.pod_uid}
        ws.send(json.dumps(greeting))
        while True:
            incoming = None
            with gevent.Timeout(settings.session_timeout, False):
                incoming = ws.receive()
            if incoming is None:
                break
            message = decode(incoming)
            if message.get('op') == 'close':
                break
            try:
                result = session.dispatch(message)
            except (OSError, ValueError, KeyError, tarfile.TarError) as error:
                failure: Dict[str, object] = {'error': str(error)}
                ws.send(json.dumps(failure))
                break
            ws.send(json.dumps(result))
    finally:
        session.close()
        ws.close()
    return ''
