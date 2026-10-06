"""Standalone CronJob program; only its service account can delete worker pods."""

import json
import ssl
from pathlib import Path
from time import monotonic, sleep, time
from typing import Dict, List, Optional, Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from throngfission.settings import WorkerSettings


class Response(Protocol):
    def read(self) -> bytes: ...
    def __enter__(self) -> 'Response': ...
    def __exit__(self, *args: object) -> None: ...


def request(url: str, headers: Dict[str, str], method: str = 'GET', body: object = None, context: Optional[ssl.SSLContext] = None, *, timeout: float) -> Dict[str, object]:  # noqa: PLR0913
    data = json.dumps(body).encode() if body is not None else None
    headers = {**headers, 'Content-Type': 'application/merge-patch+json' if method == 'PATCH' else 'application/json'}
    with cast(Response, urlopen(Request(url, data=data, headers=headers, method=method), context=context, timeout=timeout)) as response:
        return cast(Dict[str, object], json.loads(response.read()))


def reap(settings: WorkerSettings) -> None:
    account = Path('/var/run/secrets/kubernetes.io/serviceaccount')
    context = ssl.create_default_context(cafile=str(account / 'ca.crt'))
    headers = {'Authorization': 'Bearer ' + (account / 'token').read_text().strip(), 'Content-Type': 'application/merge-patch+json'}
    base = 'https://kubernetes.default.svc/api/v1/namespaces/' + settings.namespace + '/pods'
    pods = request(base + '?labelSelector=throngfission.io/installation%3D' + settings.name + ',managed%3Dfalse', headers, context=context, timeout=settings.janitor_request_timeout)
    for pod in cast(List[Dict[str, object]], pods['items']):
        try:
            inspect_pod(pod, base, headers, context, settings)
        except HTTPError as error:
            if error.code not in (404, 409):
                raise


def inspect_pod(pod: Dict[str, object], base: str, headers: Dict[str, str], context: ssl.SSLContext, settings: WorkerSettings) -> None:
    metadata = cast(Dict[str, object], pod['metadata'])
    if metadata.get('deletionTimestamp'):
        return
    url = base + '/' + str(metadata['name'])
    status = cast(Dict[str, object], pod.get('status', {}))
    annotations = cast(Dict[str, str], metadata.get('annotations', {}))
    key = 'throngfission.io/unreachable-since'
    now = time()
    expired = False
    try:
        health = request('http://' + str(status['podIP']) + ':8888/', {'X-Throngfission-Token': settings.secret}, timeout=settings.janitor_request_timeout)
        age = health.get('age')
        expired = bool(health['closed']) or (isinstance(age, (int, float)) and age > settings.session_timeout)
        healthy = isinstance(age, (int, float))
    except (URLError, TimeoutError, OSError, ValueError, KeyError):
        healthy = False
    if not healthy and not expired:
        since = float(annotations.get(key, str(now)))
        expired = now - since > settings.janitor_unreachable_timeout
        if key not in annotations:
            request(url, headers, 'PATCH', {'metadata': {'annotations': {key: str(now)}}}, context, timeout=settings.janitor_request_timeout)
    elif healthy and key in annotations:
        request(url, headers, 'PATCH', {'metadata': {'annotations': {key: None}}}, context, timeout=settings.janitor_request_timeout)
    if expired:
        try:
            request(url, headers, 'DELETE', {'apiVersion': 'v1', 'kind': 'DeleteOptions', 'preconditions': {'uid': metadata['uid']}}, context, timeout=settings.janitor_request_timeout)
        except HTTPError as error:
            if error.code not in (404, 409):
                raise


def main() -> None:
    settings = WorkerSettings.from_env()  # type: ignore[misc]
    deadline: Optional[float] = None
    while True:
        failure: OSError
        try:
            reap(settings)
            return
        except HTTPError as error:
            if error.code not in (429, 500, 502, 503, 504):
                raise
            failure = error
        except (URLError, TimeoutError, ConnectionError) as error:
            failure = error
        if deadline is None:
            deadline = monotonic() + settings.api_recovery_timeout
        if monotonic() >= deadline:
            raise failure
        sleep(settings.poll_interval)
