from uuid import uuid4

from throngfission.manifests import Manifests
from throngfission.settings import SetupSettings


def test_create_only_reconciliation_preserves_existing_resource_version(manager):
    """The API defaults fields, but a second reconciliation leaves both data and resourceVersion intact."""
    manifests = Manifests(manager.cluster.api, SetupSettings())
    body = {'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': {'name': 'throngfission-reconcile-' + uuid4().hex[:8], 'namespace': manager.settings.namespace}, 'data': {'value': 'original'}}
    first, created = manifests.ensure(body)
    try:
        assert created
        body['data']['value'] = 'different'
        second, created = manifests.ensure(body)
        assert not created
        assert second['metadata']['resourceVersion'] == first['metadata']['resourceVersion']
        assert second['data'] == {'value': 'original'}
    finally:
        manager.cluster.api.request('DELETE', manifests.path(body) + '/' + body['metadata']['name'])
