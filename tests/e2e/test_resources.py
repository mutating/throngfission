import json
from time import sleep

import pytest
from cantok import CancellationError, TimeoutToken

from throngfission.errors import FissionError


@pytest.mark.parametrize('duration', [0, 300])
def test_executor_finalizer_does_not_change_function_generation(manager, duration):
    """Executor metadata writes preserve the function spec and cannot trigger worker replacement."""
    manager.settings.stream_max_duration = duration
    manager.cluster.ensure()
    deadline = TimeoutToken(120)
    path = '/apis/fission.io/v1/namespaces/' + manager.settings.namespace + '/functions/' + manager.settings.function_name
    while True:
        deadline.check()
        function = manager.cluster.api.request('GET', path)
        if 'fission.io/function-cleanup' in function['metadata'].get('finalizers', []):
            break
        sleep(0.1)
    assert function['metadata']['generation'] == 1
    assert function['spec']['streaming'].get('maxDurationSeconds', 0) == duration


@pytest.mark.parametrize(('labelled', 'host_users'), [(False, None), (False, True), (True, None), (True, True), (True, False)])
def test_admission_remaps_only_plugin_pods(manager, labelled, host_users):
    """Admission overrides host identities for plugin pods and leaves unrelated pods intact."""
    body = {'apiVersion': 'v1', 'kind': 'Pod', 'metadata': {'generateName': 'throngfission-admission-check-'}, 'spec': {'containers': [{'name': 'check', 'image': manager.settings.resolved_image}]}}
    if labelled:
        body['metadata']['labels'] = {'throngfission.io/installation': manager.settings.function_name}
    if host_users is not None:
        body['spec']['hostUsers'] = host_users
    pod = manager.cluster.api.request('POST', manager.cluster.core + 'pods', body, query=[('dryRun', 'All')])
    assert pod['spec'].get('hostUsers', True) is (not labelled)


@pytest.mark.parametrize('grace', [1, 2])
def test_pod_grace_respects_the_plugin_setting(manager, grace):
    """The configured grace reaches both the pod and its fetcher drain hook."""
    manager.settings.termination_grace_period = grace
    isolate = manager.get(manager.read(), TimeoutToken(180))
    try:
        pod = manager.cluster.owned_pod(isolate.pod, isolate.uid)
        assert pod['spec']['terminationGracePeriodSeconds'] == grace
        fetcher = next(container for container in pod['spec']['containers'] if container['name'] == 'fetcher')
        assert fetcher['lifecycle']['preStop']['sleep']['seconds'] == grace
        assert isolate.run('true').success
    finally:
        isolate.kill()


def test_worker_can_write_as_root_without_host_root(isolate, manager):
    """Fission preserves root inside the worker while mapping it to non-root host IDs."""
    pod = manager.cluster.owned_pod(isolate.pod, isolate.uid)
    assert pod['spec']['hostUsers'] is False
    container = next(container for container in pod['spec']['containers'] if container['name'] == manager.settings.function_name)
    assert container['securityContext']['privileged'] is False
    assert container['securityContext']['allowPrivilegeEscalation'] is False
    result = isolate.run('python -c "import json, os; from pathlib import Path; p = Path(\'/usr/local/throngfission-root-proof\'); p.write_text(\'installed\'); print(json.dumps([os.geteuid(), os.getegid(), Path(\'/proc/self/uid_map\').read_text(), Path(\'/proc/self/gid_map\').read_text(), p.read_text()]))"')
    assert result.success, result.stderr
    uid, gid, uid_map, gid_map, content = json.loads(result.stdout)
    assert (uid, gid, content) == (0, 0, 'installed')
    for mapping in (uid_map, gid_map):
        ranges = [list(map(int, line.split())) for line in mapping.splitlines()]
        assert all(host > 0 for _, host, _ in ranges)
        assert any(inside == 0 and count > 0 for inside, _, count in ranges)


@pytest.mark.parametrize('mode', ['none', 'soft', 'hard'])
def test_scheduler_policy_reaches_real_workers(manager, mode):
    """Fission carries each selected mode into pods; hard mode places three workers on three nodes."""
    manager.settings.anti_affinity = mode
    if mode == 'hard':
        manager.settings.pool_size = 3
    isolates = []
    try:
        for _ in range(3 if mode == 'hard' else 1):
            isolates.append(manager.get(manager.read(), TimeoutToken(180)))
        nodes = set()
        for isolate in isolates:
            pod = manager.cluster.owned_pod(isolate.pod, isolate.uid)
            assert pod['metadata']['labels']['environmentName'] == manager.settings.function_name
            affinity = pod['spec'].get('affinity', {}).get('podAntiAffinity', {})
            if mode == 'none':
                assert not affinity
            else:
                term = {'topologyKey': 'kubernetes.io/hostname', 'labelSelector': {'matchLabels': {'environmentName': manager.settings.function_name}}}
                if mode == 'soft':
                    assert affinity == {'preferredDuringSchedulingIgnoredDuringExecution': [{'weight': 100, 'podAffinityTerm': term}]}
                else:
                    assert affinity == {'requiredDuringSchedulingIgnoredDuringExecution': [term]}
            nodes.add(pod['spec']['nodeName'])
            assert isolate.run('true').success
        if mode == 'hard':
            assert len(nodes) == 3
            with pytest.raises(CancellationError):
                manager.get(manager.read(), TimeoutToken(3))
            released_node = manager.cluster.owned_pod(isolates[0].pod, isolates[0].uid)['spec']['nodeName']
            isolates.pop(0).kill()
            replacement = manager.get(manager.read(), TimeoutToken(180))
            isolates.append(replacement)
            assert manager.cluster.owned_pod(replacement.pod, replacement.uid)['spec']['nodeName'] == released_node
            assert replacement.run('true').success
    finally:
        for isolate in isolates:
            isolate.kill()


def test_setup_switches_only_after_active_isolates_finish(manager):
    """An active session blocks a mode change; setup can then replace idle resources."""
    manager.settings.anti_affinity = 'none'
    isolate = manager.get(manager.read(), TimeoutToken(180))
    owner_path = manager.cluster.core + 'configmaps/' + manager.settings.function_name
    before = manager.cluster.api.request('GET', owner_path)['metadata']['uid']
    manager.settings.anti_affinity = 'soft'
    try:
        with pytest.raises(FissionError, match='active isolates'):
            manager.cluster.reconfigure()
        assert manager.cluster.api.request('GET', owner_path)['metadata']['uid'] == before
    finally:
        isolate.kill()
    manager.cluster.reconfigure()
    manager.cluster.ensure()
    assert manager.cluster.api.request('GET', owner_path)['metadata']['uid'] != before
    next_isolate = manager.get(manager.read(), TimeoutToken(180))
    try:
        affinity = manager.cluster.owned_pod(next_isolate.pod, next_isolate.uid)['spec']['affinity']['podAntiAffinity']
        assert 'preferredDuringSchedulingIgnoredDuringExecution' in affinity
    finally:
        next_isolate.kill()
