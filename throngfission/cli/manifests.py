"""Create-only reconciliation for pinned Kubernetes manifests."""

from time import sleep
from typing import Dict, List, Optional, Tuple, cast

from cantok import TimeoutToken

from throngfission.cli.settings import SetupSettings
from throngfission.execution.api import Kubernetes
from throngfission.execution.errors import APIError, FissionError


def contains(actual: object, desired: object) -> bool:
    """Compare declarative fields while allowing defaults supplied by Kubernetes."""
    if isinstance(desired, dict):
        return isinstance(actual, dict) and all(key in actual and contains(cast(Dict[str, object], actual)[key], value) for key, value in cast(Dict[str, object], desired).items())
    if isinstance(desired, list):
        return isinstance(actual, list) and len(actual) == len(desired) and all(contains(left, right) for left, right in zip(cast(List[object], actual), cast(List[object], desired)))
    return actual == desired


class Manifests:
    def __init__(self, api: Kubernetes, options: SetupSettings) -> None:
        self.api = api
        self.options = options
        self.discovery: Dict[str, List[Dict[str, object]]] = {}

    def path(self, body: Dict[str, object]) -> str:
        version = str(body['apiVersion'])
        prefix = '/api/v1' if version == 'v1' else f'/apis/{version}'
        if version not in self.discovery:
            self.discovery[version] = cast(List[Dict[str, object]], self.api.request('GET', prefix)['resources'])
        resource = next(item for item in self.discovery[version] if item['kind'] == body['kind'] and '/' not in str(item['name']))
        metadata = cast(Dict[str, str], body['metadata'])
        if resource['namespaced']:
            prefix += f"/namespaces/{metadata['namespace']}"
        return f"{prefix}/{resource['name']!s}"

    def get(self, path: str) -> Optional[Dict[str, object]]:
        try:
            return self.api.request('GET', path)
        except APIError as error:
            if error.status != 404:
                raise
        return None

    def ensure(self, body: Dict[str, object]) -> Tuple[Dict[str, object], bool]:
        """Read first; a successful competing creator is equivalent to our own."""
        path = self.path(body)
        name = str(cast(Dict[str, object], body['metadata'])['name'])
        existing = self.get(f'{path}/{name}')
        if existing is not None:
            return existing, False
        try:
            return self.api.request('POST', path, body), True
        except APIError as error:
            if error.status != 409:
                raise
        return self.api.request('GET', f'{path}/{name}'), False

    def wait(self, body: Dict[str, object], condition: str) -> None:
        name = cast(Dict[str, object], body['metadata'])['name']
        path = f'{self.path(body)}/{name!s}'
        deadline = TimeoutToken(self.options.setup_timeout)
        while True:
            deadline.check()
            current = self.api.request('GET', path)
            status = cast(Dict[str, object], current.get('status') or {})
            metadata = cast(Dict[str, object], current['metadata'])
            conditions = cast(List[Dict[str, str]], status.get('conditions') or [])
            observed = body['kind'] != 'Deployment' or cast(int, status.get('observedGeneration', 0)) >= cast(int, metadata.get('generation', 0))
            if observed and any(item['type'] == condition and item['status'] == 'True' for item in conditions):
                return
            if any(item.get('reason') == 'ProgressDeadlineExceeded' for item in conditions):
                raise FissionError(f'Fission deployment failed to become ready: {path}')
            sleep(self.options.setup_poll_interval)
