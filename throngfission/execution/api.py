"""Typed boundary around the official Kubernetes client's untyped API."""

import json
import ssl
from importlib import import_module
from time import monotonic, sleep
from typing import Dict, List, Optional, Protocol, Tuple, Type, cast

from urllib3.exceptions import (
    ConnectTimeoutError,
    MaxRetryError,
    ProtocolError,
    ReadTimeoutError,
)

from throngfission.execution.errors import APIError
from throngfission.execution.settings import Settings


class Configuration(Protocol):
    host: str
    verify_ssl: bool
    ssl_ca_cert: Optional[str]
    cert_file: Optional[str]
    key_file: Optional[str]


class Client(Protocol):
    configuration: Configuration

    def call_api(self, path: str, method: str, **kwargs: object) -> object: ...
    def update_params_for_auth(self, headers: Dict[str, str], query: List[Tuple[str, str]], settings: List[str]) -> None: ...
    def close(self) -> None: ...


class ConfigModule(Protocol):
    def new_client_from_config(self, config_file: Optional[str], context: Optional[str]) -> Client: ...


class Failure(Protocol):
    status: int


class Response(Protocol):
    def read(self) -> bytes: ...
    def release_conn(self) -> None: ...


class Kubernetes:
    def __init__(self, settings: Settings) -> None:
        config = cast(ConfigModule, import_module('kubernetes.config'))
        self.client = config.new_client_from_config(settings.kubeconfig or None, settings.context or None)
        self.timeout = settings.request_timeout
        self.recovery_timeout = settings.api_recovery_timeout
        self.poll_interval = settings.poll_interval
        self.exception = cast(Type[Exception], import_module('kubernetes.client.rest').ApiException)

    def request(self, method: str, path: str, body: object = None, query: Optional[List[Tuple[str, str]]] = None) -> Dict[str, object]:
        deadline = monotonic() + self.recovery_timeout if method in ('GET', 'DELETE') else None
        while True:
            try:
                response = cast(Response, self.client.call_api(path, method, body=body, query_params=query or [], auth_settings=['BearerToken'], _preload_content=False, _return_http_data_only=True, _request_timeout=self.timeout, header_params={'Content-Type': 'application/merge-patch+json' if method == 'PATCH' else 'application/json'}))
                try:
                    return cast(Dict[str, object], json.loads(response.read()))
                finally:
                    response.release_conn()
            except self.exception as error:
                status = cast(Failure, error).status
                if deadline is None or status not in (429, 500, 502, 503, 504) or monotonic() >= deadline:
                    raise APIError(status, str(error)) from error
            except (OSError, ConnectTimeoutError, MaxRetryError, ProtocolError, ReadTimeoutError):
                if deadline is None or monotonic() >= deadline:
                    raise
            sleep(self.poll_interval)

    def connection(self) -> Tuple[str, Dict[str, str], Dict[str, object]]:
        configuration = self.client.configuration
        headers: Dict[str, str] = {}
        self.client.update_params_for_auth(headers, [], ['BearerToken'])
        options: Dict[str, object] = {
            'cert_reqs': ssl.CERT_REQUIRED if configuration.verify_ssl else ssl.CERT_NONE,
            'ca_certs': configuration.ssl_ca_cert,
            'certfile': configuration.cert_file,
            'keyfile': configuration.key_file,
        }
        return configuration.host.replace('https://', 'wss://').replace('http://', 'ws://'), headers, options

    def close(self) -> None:
        self.client.close()
