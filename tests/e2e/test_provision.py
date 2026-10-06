import json
from time import sleep
from unittest.mock import Mock

import pytest
from cantok import TimeoutToken

from throngfission import Settings
from throngfission.api import Kubernetes
from throngfission.provision import run
from throngfission.setup import Setup


@pytest.mark.parametrize('missing', [[(0, 'ubuntu')], [(0, 'python'), (1, 'debian'), (2, 'ubuntu')]])
def test_setup_restores_images_only_on_affected_nodes(setup_cluster, missing, monkeypatch):
    """Mixed per-node caches are repaired individually while complete image-node pairs are skipped."""
    if setup_cluster is None:
        pytest.skip('Image removal is confined to disposable nodes.')
    settings, options, containers = setup_cluster
    api = Kubernetes(settings)
    suspended = []
    try:
        for preset in ['python', 'debian', 'ubuntu']:
            path = '/apis/batch/v1/namespaces/' + settings.namespace + '/cronjobs/' + Settings(image=preset).function_name
            api.request('PATCH', path, {'spec': {'suspend': True}})
            suspended.append(path)
        deadline = TimeoutToken(120)
        while any((job.get('status') or {}).get('active') for job in api.request('GET', '/apis/batch/v1/namespaces/' + settings.namespace + '/jobs')['items']):
            deadline.check()
            sleep(0.5)
        deleted = []
        for index, preset in missing:
            container = containers[index].get_wrapped_container()
            node = container.exec_run(['cat', '/etc/hostname']).output.decode().strip() if index == 0 else 'throngfission-agent-' + str(index - 1)
            reference = Settings(image=preset).resolved_image
            result = container.exec_run(['crictl', 'rmi', reference])
            assert result.exit_code == 0, result.output.decode()
            assert container.exec_run(['crictl', 'inspecti', reference]).exit_code != 0
            deleted.append((node, preset, reference))
        deadline = TimeoutToken(120)
        while True:
            deadline.check()
            nodes = {node['metadata']['name']: node for node in api.request('GET', '/api/v1/nodes')['items']}
            if all(reference not in [name for image in nodes[node]['status'].get('images') or [] for name in image.get('names') or []] for node, _, reference in deleted):
                break
            sleep(0.5)
        importer = Mock(wraps=run)
        monkeypatch.setattr('throngfission.provision.run', importer)
        Setup(settings, options).run()
        observed = {(call.args[0][call.args[0].index('--nodes') + 1], call.args[0][call.args[0].index('--image') + 1]) for call in importer.call_args_list}
        assert observed == {(node, preset) for node, preset, _ in deleted}
        for index, preset in missing:
            result = containers[index].get_wrapped_container().exec_run(['crictl', 'inspecti', Settings(image=preset).resolved_image])
            assert result.exit_code == 0, result.output.decode()
            assert json.loads(result.output)['status']['pinned'] is True
    finally:
        try:
            for path in suspended:
                api.request('PATCH', path, {'spec': {'suspend': False}})
        finally:
            api.close()
