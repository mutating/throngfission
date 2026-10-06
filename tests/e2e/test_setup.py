from pathlib import Path
from time import sleep
from unittest.mock import Mock

import pytest
from cantok import TimeoutToken

from throngfission import Settings
from throngfission.api import Kubernetes
from throngfission.errors import APIError
from throngfission.settings import SetupSettings
from throngfission.setup import Setup


def test_disposable_cluster_has_three_ready_nodes(kubernetes_cluster):
    """Setup scenarios use three independent K3s node containers on one private network."""
    if kubernetes_cluster is None:
        pytest.skip('This check describes the disposable test infrastructure.')
    kubeconfig, containers = kubernetes_cluster
    api = Kubernetes(Settings(kubeconfig=str(kubeconfig)))
    try:
        nodes = api.request('GET', '/api/v1/nodes')['items']
        assert len({container.get_wrapped_container().id for container in containers}) == 3
        assert len({node['metadata']['name'] for node in nodes}) == 3
        assert all(any(condition['type'] == 'Ready' and condition['status'] == 'True' for condition in node['status']['conditions']) for node in nodes)
    finally:
        api.close()


def test_first_setup_creates_three_diagnosed_functions(setup_cluster):
    """An empty cluster receives Fission, node images and three independently diagnosed functions."""
    if setup_cluster is None:
        pytest.skip('Fresh installation is confined to disposable clusters.')
    settings, options, _ = setup_cluster
    api = Kubernetes(settings)
    try:
        for preset in ['python', 'debian', 'ubuntu']:
            name = Settings(image=preset).function_name
            function = api.request('GET', '/apis/fission.io/v1/namespaces/fission-function/functions/' + name)
            assert function['spec']['onceOnly'] is True
            environment = api.request('GET', '/apis/fission.io/v1/namespaces/fission-function/environments/' + name)
            assert environment['spec']['runtime']['podspec']['affinity']['podAntiAffinity']['preferredDuringSchedulingIgnoredDuringExecution'] == [
                {'weight': 100, 'podAffinityTerm': {'topologyKey': 'kubernetes.io/hostname', 'labelSelector': {'matchLabels': {'environmentName': name}}}}]
            owner = api.request('GET', '/api/v1/namespaces/fission-function/configmaps/' + name)
            assert len(owner['metadata']['annotations']['throngfission.io/diagnostic']) == 64
        assert not api.request('GET', '/api/v1/namespaces/fission-function/pods', query=[('labelSelector', 'managed=false')])['items']
        jobs = api.request('GET', '/apis/batch/v1/namespaces/fission-function/jobs')['items']
        assert all(not job['metadata']['name'].startswith(('throngfission-import-', 'throngfission-apparmor-')) for job in jobs)
        assert list(Path(options.work_dir).glob('throngfission-*')) == []
    finally:
        api.close()


def test_repeated_setup_makes_no_persistent_writes(setup_cluster, monkeypatch):
    """An already prepared cluster needs only reads and admission dry-runs, never Jobs or diagnostics."""
    if setup_cluster is None:
        pytest.skip('Installation checks use the disposable cluster.')
    settings, options, _ = setup_cluster
    requests = []
    original = Kubernetes.request
    monkeypatch.setattr(Kubernetes, 'request', lambda client, method, path, *args, **kwargs: (requests.append((method, path, kwargs)), original(client, method, path, *args, **kwargs))[1])
    Setup(settings, options).run()
    assert requests
    assert all(method == 'GET' or (method == 'POST' and kwargs.get('query') == [('dryRun', 'All')]) for method, _, kwargs in requests)


def test_setup_switches_an_idle_preset_and_remains_idempotent(manager, tmp_path):
    """Setup rebuilds one test-owned preset when placement changes and skips repeat diagnosis."""
    manager.cluster.ensure()
    settings = Settings(**{field: getattr(manager.settings, field) for field in Settings.__annotations__})
    settings.name = ''
    settings.python_name = manager.settings.name
    options = SetupSettings(work_dir=str(tmp_path))
    api = Kubernetes(settings)
    name = settings.python_name
    path = '/api/v1/namespaces/' + settings.namespace + '/configmaps/' + name
    before = api.request('GET', path)['metadata']['uid']
    changed = Settings(**{field: getattr(settings, field) for field in Settings.__annotations__})
    changed.anti_affinity = 'hard'
    selected = Setup(changed, options)
    try:
        selected.function('python')
        assert api.request('GET', path)['metadata']['uid'] != before
        environment = api.request('GET', '/apis/fission.io/v1/namespaces/' + settings.namespace + '/environments/' + name)
        assert environment['spec']['runtime']['podspec']['affinity']['podAntiAffinity']['requiredDuringSchedulingIgnoredDuringExecution'] == [
            {'topologyKey': 'kubernetes.io/hostname', 'labelSelector': {'matchLabels': {'environmentName': name}}}]
        replacement = api.request('GET', path)['metadata']['uid']
        selected.function('python')
        assert api.request('GET', path)['metadata']['uid'] == replacement
    finally:
        selected.api.close()
        restored = Setup(settings, options)
        try:
            restored.function('python')
        finally:
            restored.api.close()
            api.close()


@pytest.mark.parametrize(('collection', 'name'), [
    ('/apis/apps/v1/namespaces/fission/deployments', 'router'),
    ('/apis/fission.io/v1/namespaces/fission-function/functions', 'throngfission-debian'),
    ('/apis/fission.io/v1/namespaces/fission-function/httptriggers', 'throngfission-ubuntu'),
    ('/apis/batch/v1/namespaces/fission-function/cronjobs', 'throngfission-python'),
    ('/api/v1/namespaces/fission-function/configmaps', 'throngfission-python'),
])
def test_setup_repairs_partial_installations(setup_cluster, collection, name):
    """Missing control-plane or function resources are restored without replacing other functions."""
    if setup_cluster is None:
        pytest.skip('Destructive recovery checks use only disposable clusters.')
    settings, options, _ = setup_cluster
    api = Kubernetes(settings)
    try:
        preserved_path = '/apis/fission.io/v1/namespaces/fission-function/functions/throngfission-ubuntu'
        preserved = api.request('GET', preserved_path)['metadata']['uid']
        path = collection + '/' + name
        old = api.request('GET', path)['metadata']['uid']
        api.request('DELETE', path, {'propagationPolicy': 'Foreground', 'preconditions': {'uid': old}})
        deadline = TimeoutToken(120)
        while True:
            deadline.check()
            try:
                api.request('GET', path)
            except APIError as error:
                if error.status != 404:
                    raise
                break
            sleep(0.2)
        Setup(settings, options).run()
        assert api.request('GET', path)['metadata']['uid'] != old
        assert api.request('GET', preserved_path)['metadata']['uid'] == preserved
    finally:
        api.close()


def test_setup_retries_a_diagnostic_interrupted_after_function_creation(setup_cluster, monkeypatch):
    """An existing function without a diagnostic checkpoint is tested on the next invocation."""
    if setup_cluster is None:
        pytest.skip('Interruption checks use only disposable clusters.')
    settings, options, _ = setup_cluster
    api = Kubernetes(settings)
    path = '/api/v1/namespaces/fission-function/configmaps/throngfission-python'
    try:
        api.request('PATCH', path, {'metadata': {'annotations': {'throngfission.io/diagnostic': None}}})
        with monkeypatch.context() as interruption:
            interruption.setattr('throngfission.setup.FissionManager.get', Mock(side_effect=InterruptedError('interrupted')))
            with pytest.raises(InterruptedError, match='interrupted'):
                Setup(settings, options).run()
        assert 'throngfission.io/diagnostic' not in api.request('GET', path)['metadata'].get('annotations', {})
        Setup(settings, options).run()
        assert len(api.request('GET', path)['metadata']['annotations']['throngfission.io/diagnostic']) == 64
    finally:
        api.close()
