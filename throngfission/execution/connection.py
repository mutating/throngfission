import json
from importlib import import_module
from threading import Lock
from typing import Dict, Protocol, Type, cast

from locklib import ContextLockProtocol

from throngfission.execution.errors import FissionError, IsolateClosedError
from throngfission.execution.protocol import decode


class Socket(Protocol):
    def send(self, value: str) -> object: ...
    def recv(self) -> object: ...
    def settimeout(self, timeout: float) -> None: ...
    def abort(self) -> None: ...
    def close(self, **kwargs: object) -> None: ...


class WebSockets(Protocol):
    WebSocketException: Type[Exception]

    def create_connection(self, url: str, **options: object) -> Socket: ...


class Connection:
    def __init__(self, socket: Socket) -> None:
        self.socket = socket
        self.lock: ContextLockProtocol = Lock()
        self.error_type = cast(WebSockets, import_module('websocket')).WebSocketException
        self.closed = False

    def receive(self) -> Dict[str, object]:
        try:
            value = self.socket.recv()
        except (OSError, self.error_type) as error:
            raise IsolateClosedError('The connection was lost; commands are never replayed.') from error
        if not isinstance(value, str) or not value:
            raise IsolateClosedError('The worker closed its session.')
        try:
            result = decode(value)
        except ValueError as error:
            raise IsolateClosedError('The worker sent an invalid protocol response.') from error
        if 'error' in result:
            raise FissionError(str(result['error']))
        return result

    def request(self, message: Dict[str, object]) -> Dict[str, object]:
        with self.lock:
            if self.closed:
                raise IsolateClosedError('The connection has already been invalidated.')
            try:
                self.socket.send(json.dumps(message))
                return self.receive()
            except (FissionError, OSError, self.error_type) as error:
                self.closed = True
                if isinstance(error, FissionError):
                    raise
                raise IsolateClosedError('The connection was lost; commands are never replayed.') from error

    def close(self) -> None:
        try:
            self.socket.abort()
        except (OSError, AttributeError):
            pass  # websocket-client 1.8 can race with an already closed socket.
        with self.lock:
            self.closed = True
            self.socket.close(timeout=0)
