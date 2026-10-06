import base64
import hashlib
import json
import secrets
from importlib import import_module
from threading import Lock
from time import monotonic, sleep
from typing import Dict, List, Optional, Tuple, cast

from cantok import AbstractToken
from locklib import ContextLockProtocol

from throngfission.api import Kubernetes
from throngfission.connection import Connection, WebSockets
from throngfission.errors import APIError, CleanupError, FissionError
from throngfission.package import worker_archive
from throngfission.protocol import string
from throngfission.resources import resources, root
from throngfission.settings import Settings


class Cluster:
    def __init__(self, settings: Settings) -> None:
        settings.validate()
        self.settings = settings
        self.api = Kubernetes(settings)
        self.lock: ContextLockProtocol = Lock()
        self.secret: Optional[str] = None
        self.core = '/api/v1/namespaces/' + settings.namespace + '/'

    def ensure(self) -> str:
        with self.lock:
            if self.secret is not None:
                return self.secret
            probe = self.api.request('POST', self.core + 'pods', {
                'apiVersion': 'v1', 'kind': 'Pod',
                'metadata': {'generateName': self.settings.function_name + '-', 'labels': {'throngfission.io/installation': self.settings.function_name}},
                'spec': {'hostUsers': True, 'automountServiceAccountToken': False, 'containers': [{'name': 'check', 'image': self.settings.resolved_image}]},
            }, query=[('dryRun', 'All')])
            if cast(Dict[str, object], probe.get('spec', {})).get('hostUsers') is not False:
                raise FissionError('Install the throngfission user-namespace admission policy before creating workers.')
            archive = worker_archive()
            fingerprint = self.fingerprint(self.settings, archive)
            owner = self.create(self.core + 'configmaps', root(self.settings, fingerprint, archive))
            if cast(Dict[str, str], owner['data'])['fingerprint'] != fingerprint:
                raise FissionError('This installation uses different code or settings; choose another name or uninstall it explicitly.')
            reference = {'apiVersion': 'v1', 'kind': 'ConfigMap', 'name': self.settings.function_name, 'uid': cast(Dict[str, str], owner['metadata'])['uid'], 'blockOwnerDeletion': True}
            token = secrets.token_urlsafe(32)
            for path, body in resources(self.settings, token):
                cast(Dict[str, object], body['metadata'])['ownerReferences'] = [reference]
                result = self.create(path, body)
                if body['kind'] == 'Secret':
                    token = base64.b64decode(cast(Dict[str, str], result['data'])['token']).decode()
            self.secret = token
            return token

    @staticmethod
    def fingerprint(settings: Settings, archive: str) -> str:
        """Tie a shared installation to its worker code and resource settings."""
        return hashlib.sha256((json.dumps(resources(settings, ''), sort_keys=True) + archive).encode()).hexdigest()

    def reconfigure(self) -> None:
        """Let explicit setup replace an idle installation when only scheduling changes."""
        try:
            owner = self.api.request('GET', self.core + 'configmaps/' + self.settings.function_name)
        except APIError as error:
            if error.status == 404:
                return
            raise
        archive = worker_archive()
        saved = cast(Dict[str, str], owner.get('data', {})).get('fingerprint')
        if saved == self.fingerprint(self.settings, archive):
            return
        for mode in ('none', 'soft', 'hard'):
            if mode == self.settings.anti_affinity:
                continue
            previous = Settings(**{field: getattr(self.settings, field) for field in Settings.__annotations__})  # type: ignore[misc]
            previous.anti_affinity = mode
            if saved == self.fingerprint(previous, archive):
                active = self.api.request('GET', self.core + 'pods', query=[('labelSelector', 'throngfission.io/installation=' + self.settings.function_name + ',managed=false')])
                if active['items']:
                    raise FissionError('Cannot switch anti_affinity while this function has active isolates; retry setup after they finish.')
                self.uninstall()
                return

    def create(self, path: str, body: Dict[str, object]) -> Dict[str, object]:
        try:
            result = self.api.request('GET', path + '/' + self.settings.function_name)
        except APIError as error:
            if error.status != 404:
                raise
            try:
                return self.api.request('POST', path, body)
            except APIError as conflict:
                if conflict.status != 409:
                    raise
            result = self.api.request('GET', path + '/' + self.settings.function_name)
        metadata = cast(Dict[str, object], result['metadata'])
        if cast(Dict[str, str], metadata.get('labels', {})).get('throngfission.io/installation') != self.settings.function_name:
            raise FissionError('Refusing to adopt an object not managed by throngfission: ' + path)
        return result

    def connect(self, token: AbstractToken) -> Tuple[Connection, str, str]:
        secret = self.ensure()
        websockets = cast(WebSockets, import_module('websocket'))
        while True:
            token.check()
            base, headers, options = self.api.connection()
            if self.settings.router_url:
                base = self.settings.router_url.rstrip('/').replace('https://', 'wss://').replace('http://', 'ws://')
                headers = {}
                options = {}
            else:
                base += '/api/v1/namespaces/' + self.settings.router_namespace + '/services/' + self.settings.router_service + '/proxy'
            headers['X-Throngfission-Token'] = secret
            try:
                socket = websockets.create_connection(base + '/throngfission/' + self.settings.namespace + '/' + self.settings.function_name, header=headers, sslopt=options, suppress_origin=True, timeout=self.settings.request_timeout)
            except (OSError, websockets.WebSocketException) as error:
                status = cast(Optional[int], getattr(error, 'status_code', None))
                if status is not None and status not in (404, 429, 500, 502, 503, 504):
                    raise FissionError('Fission rejected the session handshake.') from error
                self.wait_for_pool(token)
                continue
            connection = Connection(socket)
            try:
                hello = connection.receive()
                if hello.get('protocol') != 2:
                    raise FissionError('Unsupported worker protocol.')
                pod, uid = string(hello, 'pod'), string(hello, 'uid')
                worker = self.owned_pod(pod, uid)
                if cast(Dict[str, object], worker.get('spec', {})).get('hostUsers') is not False:
                    self.delete_pod(pod, uid)
                    raise FissionError('Fission did not preserve the required pod user namespace; refusing to run commands.')
                token.check()
                return connection, pod, uid
            except BaseException:
                connection.close()
                raise

    def wait_for_pool(self, token: AbstractToken) -> None:
        while True:
            token.check()
            result = self.api.request('GET', self.core + 'pods', query=[('labelSelector', 'environmentName=' + self.settings.function_name + ',managed=true')])
            for pod in cast(List[Dict[str, object]], result['items']):
                status = cast(Dict[str, object], pod.get('status', {}))
                for container in cast(List[Dict[str, object]], status.get('containerStatuses', [])):
                    waiting = cast(Dict[str, str], cast(Dict[str, object], container['state']).get('waiting', {}))
                    if waiting.get('reason') in ('InvalidImageName', 'CreateContainerConfigError'):
                        raise FissionError(waiting.get('message', 'The worker cannot start.'))
                if any(condition.get('type') == 'Ready' and condition.get('status') == 'True' for condition in cast(List[Dict[str, str]], status.get('conditions', []))):
                    sleep(self.settings.poll_interval)
                    return
            sleep(self.settings.poll_interval)

    def owned_pod(self, name: str, uid: str) -> Dict[str, object]:
        pod = self.api.request('GET', self.core + 'pods/' + name)
        metadata = cast(Dict[str, object], pod['metadata'])
        labels = cast(Dict[str, str], metadata.get('labels', {}))
        if metadata['uid'] != uid or labels.get('throngfission.io/installation') != self.settings.function_name or labels.get('managed') != 'false':
            raise FissionError('Refusing to use or delete a pod outside this installation.')
        return pod

    def delete_pod(self, name: str, uid: str) -> None:
        try:
            self.owned_pod(name, uid)
            self.api.request('DELETE', self.core + 'pods/' + name, {'apiVersion': 'v1', 'kind': 'DeleteOptions', 'preconditions': {'uid': uid}})
        except APIError as error:
            if error.status == 404:
                return
            raise
        self.wait_deleted(self.core + 'pods/' + name, uid)

    def wait_deleted(self, path: str, uid: str) -> None:
        deadline = monotonic() + self.settings.cleanup_timeout
        while monotonic() < deadline:
            try:
                result = self.api.request('GET', path)
            except APIError as error:
                if error.status == 404:
                    return
                raise
            if cast(Dict[str, str], result['metadata'])['uid'] != uid:
                return
            sleep(self.settings.poll_interval)
        raise CleanupError('Resource deletion is still pending: ' + path)

    def uninstall(self) -> None:
        with self.lock:
            self.secret = None
            path = self.core + 'configmaps/' + self.settings.function_name
            try:
                owner = self.api.request('GET', path)
                metadata = cast(Dict[str, object], owner['metadata'])
                if cast(Dict[str, str], metadata.get('labels', {})).get('throngfission.io/installation') != self.settings.function_name:
                    raise FissionError('Refusing to uninstall an object not managed by throngfission.')
                uid = str(metadata['uid'])
                self.api.request('DELETE', path, {'propagationPolicy': 'Foreground', 'preconditions': {'uid': uid}})
            except APIError as error:
                if error.status != 404:
                    raise
            else:
                self.wait_deleted(path, uid)
            deadline = monotonic() + self.settings.cleanup_timeout
            while True:
                try:
                    pods = self.api.request('GET', self.core + 'pods', query=[('labelSelector', 'throngfission.io/installation=' + self.settings.function_name)])
                except APIError as error:
                    if error.status == 404:
                        return
                    raise
                if not pods['items']:
                    return
                if monotonic() >= deadline:
                    raise CleanupError('Installation pods are still being deleted: ' + self.settings.function_name)
                sleep(self.settings.poll_interval)
