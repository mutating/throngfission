import base64
import shlex
from dataclasses import dataclass
from io import BytesIO
from threading import Event, Lock, Thread
from typing import List, Optional, Type, Union, cast

from cantok import AbstractToken, DefaultToken
from locklib import ContextLockProtocol
from throng import AbstractIsolate
from throng.abstracts.results import SimpleRunResult
from throng.errors import CannotInstallDependencyError, InterruptedInstallationError

from throngfission.cluster import Cluster
from throngfission.connection import Connection
from throngfission.errors import FissionError, IsolateClosedError
from throngfission.protocol import string


@dataclass
class RunResult(SimpleRunResult):
    killed_by_token: bool = False


def heartbeat(connection: Connection, stopped: Event, interval: float) -> None:
    """Keep idle isolates alive without retaining the isolate Python object."""
    while not stopped.wait(interval):
        try:
            connection.request({'op': 'ping'})
        except FissionError:
            stopped.set()
            connection.close()


class FissionIsolate(AbstractIsolate):
    connection: Optional[Connection] = None

    def __init__(self, cluster: Cluster, connection: Connection, pod: str, uid: str) -> None:
        self.cluster = cluster
        self.pod = pod
        self.uid = uid
        self.lock: ContextLockProtocol = Lock()
        self.cleanup_lock: ContextLockProtocol = Lock()
        self.stopped = Event()
        self.deleted = False
        self.thread = Thread(target=heartbeat, args=(connection, self.stopped, cluster.settings.heartbeat_interval), daemon=True, name='throngfission-heartbeat')
        self.connection = connection
        self.thread.start()

    def __del__(self) -> None:
        if self.connection is not None:
            self.stopped.set()
            self.connection.close()

    def active(self) -> Connection:
        if self.connection is None or self.stopped.is_set():
            raise IsolateClosedError('The isolate was destroyed or its session was lost.')
        return self.connection

    def run(self, command: str, token: AbstractToken = DefaultToken(), exception: Union[bool, BaseException, Type[BaseException]] = False) -> RunResult:  # noqa: B008
        return cast(RunResult, super().run(command, token=token, exception=exception))

    def _run(self, command: str, token: AbstractToken = DefaultToken()) -> RunResult:  # noqa: B008
        return self.execute(shlex.split(command), token)

    def execute(self, argv: List[str], token: AbstractToken) -> RunResult:
        with self.lock:
            connection = self.active()
            if not token:
                return RunResult(success=False, killed_by_token=True)
            connection.request({'op': 'start', 'argv': argv})
            cancelled = False
            while True:
                self.active()
                if not cancelled and not token:
                    connection.request({'op': 'cancel'})
                    cancelled = True
                result = connection.request({'op': 'poll'})
                if result.get('done'):
                    returncode = cast(int, result['returncode'])
                    return RunResult(success=returncode == 0, returncode=returncode, stdout=string(result, 'stdout'), stderr=string(result, 'stderr'), killed_by_token=cancelled)
                self.stopped.wait(self.cluster.settings.poll_interval)

    def read(self) -> bytes:
        with self.lock:
            self.active().request({'op': 'read'})
            buffer = BytesIO()
            while True:
                chunk = base64.b64decode(string(self.active().request({'op': 'download'}), 'state'), validate=True)
                if not chunk:
                    return buffer.getvalue()
                buffer.write(chunk)

    def install(self, *packages: str, token: AbstractToken = DefaultToken()) -> None:  # noqa: B008
        if not packages:
            return
        if not token:
            raise InterruptedInstallationError('The installation of the requested packages has been cancelled.')
        result = self.execute(['python', '-m', 'pip', 'install', *packages], token)
        if not result.success:
            raise CannotInstallDependencyError('pip could not install the requested packages.', result)

    def kill(self) -> None:
        if self.connection is None:
            return
        self.stopped.set()
        self.connection.close()
        self.thread.join()
        with self.cleanup_lock:
            if not self.deleted:
                self.cluster.delete_pod(self.pod, self.uid)
                self.deleted = True
