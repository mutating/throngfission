import pytest


@pytest.mark.parametrize(('labelled', 'host_users'), [(False, None), (False, True), (True, None), (True, True), (True, False)])
def test_admission_remaps_only_plugin_pods(manager, labelled, host_users):
    """Admission overrides host identities for plugin pods and leaves unrelated pods intact."""
    body = {'apiVersion': 'v1', 'kind': 'Pod', 'metadata': {'generateName': 'throngfission-admission-check-'}, 'spec': {'containers': [{'name': 'check', 'image': manager.settings.resolved_image}]}}
    if labelled:
        body['metadata']['labels'] = {'throngfission.io/installation': manager.settings.function_name}
    if host_users is not None:
        body['spec']['hostUsers'] = host_users
    pod = manager.cluster.api.request('POST', f'{manager.cluster.core}pods', body, query=[('dryRun', 'All')])
    assert pod['spec'].get('hostUsers', True) is (not labelled)
