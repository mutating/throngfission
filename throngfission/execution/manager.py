import base64
from pathlib import Path
from threading import Lock
from typing import List, Optional, Union, cast

from cantok import AbstractToken, DefaultToken
from locklib import ContextLockProtocol
from throng import AbstractManager

from throngfission.execution.archive import read_directory
from throngfission.execution.cluster import Cluster
from throngfission.execution.errors import IsolateClosedError
from throngfission.execution.isolate import FissionIsolate
from throngfission.execution.settings import Settings


class FissionManager(AbstractManager):
    def __init__(self, path: Union[str, Path] = '.', exclude: Optional[List[str]] = None, prepare: Optional[List[str]] = None, packages: Optional[List[str]] = None, *, settings: Optional[Settings] = None) -> None:
        super().__init__(path, exclude, prepare, packages)
        self.settings = settings if settings is not None else Settings.from_env()  # type: ignore[misc]
        self.settings.validate()
        self.lock: ContextLockProtocol = Lock()
        self._cluster: Optional[Cluster] = None

    @property
    def cluster(self) -> Cluster:
        with self.lock:
            if self._cluster is None:
                self._cluster = Cluster(self.settings)
            return self._cluster

    def read(self) -> bytes:
        return read_directory(self.path, self.exclude)

    def get(self, state: bytes, token: AbstractToken = DefaultToken()) -> FissionIsolate:  # noqa: B008
        return cast(FissionIsolate, super().get(state, token=token))

    def _get(self, state: bytes, token: AbstractToken = DefaultToken()) -> FissionIsolate:  # noqa: B008
        token.check()
        cluster = self.cluster
        connection, pod, uid = cluster.connect(token)
        try:
            connection.socket.settimeout(self.settings.upload_timeout)
            try:
                connection.request({'op': 'init', 'size': len(state), 'exclude': self.exclude})
                for offset in range(0, len(state), self.settings.transfer_chunk_size):
                    token.check()
                    chunk = state[offset:offset + self.settings.transfer_chunk_size]
                    connection.request({'op': 'upload', 'state': base64.b64encode(chunk).decode('ascii')})
                token.check()
                connection.request({'op': 'init_done'})
            except IsolateClosedError as error:
                raise IsolateClosedError(
                    f'Connection lost while uploading the workspace during init '
                    f'({len(state)} TAR bytes, {4 * ((len(state) + 2) // 3)} encoded bytes). '
                    f'Chunk size: {self.settings.transfer_chunk_size} bytes. Check worker logs and upload_timeout.',
                ) from error
            connection.socket.settimeout(self.settings.request_timeout)
            token.check()
            return FissionIsolate(cluster, connection, pod, uid)
        except BaseException:
            connection.close()
            cluster.delete_pod(pod, uid)
            raise

    def uninstall(self) -> None:
        """Remove this shared installation, terminating all its isolates."""
        self.cluster.uninstall()
