from concurrent.futures import ThreadPoolExecutor
from time import sleep

import pytest
from cantok import TimeoutToken
from locklib import LockTraceWrapper, SmartLock

from throngfission.execution.errors import IsolateClosedError


def test_repeated_allocations_leave_no_specialized_pods(manager):
    """Repeated get/run/kill cycles keep shared objects constant and remove workers."""
    snapshot = manager.read()
    pods = set()
    for _ in range(10):
        isolate = manager.get(snapshot, TimeoutToken(180))
        try:
            assert isolate.pod not in pods
            pods.add(isolate.pod)
            assert isolate.run('python -c "print(42)"').stdout == '42\n'
        finally:
            isolate.kill()
        assert not manager.cluster.api.request('GET', f'{manager.cluster.core}pods', query=[('labelSelector', f'throngfission.io/installation={manager.settings.function_name},managed=false')])['items']
    for resource in ['configmaps', 'secrets']:
        objects = manager.cluster.api.request('GET', manager.cluster.core + resource, query=[('labelSelector', f'throngfission.io/installation={manager.settings.function_name}')])['items']
        assert len(objects) == 1


def test_uninstall_waits_for_retained_isolates_to_disappear(isolate, manager):
    """Removing shared infrastructure confirms deletion of every retained worker."""
    manager.uninstall()
    with pytest.raises(IsolateClosedError):
        isolate.run('true')
    assert not manager.cluster.api.request('GET', f'{manager.cluster.core}pods', query=[('labelSelector', f'throngfission.io/installation={manager.settings.function_name}')])['items']
    for resource in ['environments', 'functions', 'packages', 'httptriggers']:
        assert not manager.cluster.api.request('GET', f'/apis/fission.io/v1/namespaces/{manager.settings.namespace}/{resource}', query=[('labelSelector', f'throngfission.io/installation={manager.settings.function_name}')])['items']


def test_allocation_waits_for_real_scheduler_capacity(manager, fission_cluster):
    """A full three-node cluster keeps get waiting until a previous isolate is killed."""
    if not fission_cluster:
        pytest.skip('Resource saturation is confined to the disposable CI cluster.')
    nodes = manager.cluster.api.request('GET', '/api/v1/nodes')['items']
    cpu = nodes[0]['status']['allocatable']['cpu']
    millicores = int(cpu[:-1]) if cpu.endswith('m') else int(cpu) * 1000
    manager.settings.cpu_request = f'{millicores // 2 + 1!s}m'
    manager.settings.pool_size = 1
    manager.lock = LockTraceWrapper(SmartLock())
    manager.cluster.lock = LockTraceWrapper(SmartLock())
    snapshot = manager.read()
    allocated = []
    token = TimeoutToken(180)
    try:
        for _ in nodes:
            allocated.append(manager.get(snapshot, TimeoutToken(180)))
        first = allocated[0]
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(manager.get, snapshot, token)
            try:
                while True:
                    token.check()
                    pods = manager.cluster.api.request('GET', f'{manager.cluster.core}pods', query=[('labelSelector', f'environmentName={manager.settings.function_name},managed=true')])['items']
                    if any(condition.get('reason') == 'Unschedulable' for pod in pods for condition in pod.get('status', {}).get('conditions', [])):
                        break
                    sleep(0.2)
                assert not future.done()
                first.kill()
                second = future.result(timeout=120)
                try:
                    assert second.pod != first.pod
                    assert second.run('true').success
                finally:
                    second.kill()
            finally:
                token.cancel()
    finally:
        for isolate in allocated:
            isolate.kill()
