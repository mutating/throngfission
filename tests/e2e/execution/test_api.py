from time import sleep

import pytest
from cantok import TimeoutToken

from throngfission.execution.errors import APIError


def test_kubeconfig_client_remains_usable_after_api_failure(isolate, manager):
    """API errors preserve queries, and cold-start leftovers disappear around a live worker."""
    with pytest.raises(APIError) as failure:
        manager.cluster.api.request('GET', f'{manager.cluster.core}pods/{manager.settings.function_name}-missing')
    assert failure.value.status == 404
    token = TimeoutToken(240)
    while True:
        token.check()
        pods = manager.cluster.api.request('GET', f'{manager.cluster.core}pods', query=[('labelSelector', f'throngfission.io/installation={manager.settings.function_name},managed=false')])['items']
        assert isolate.uid in {pod['metadata']['uid'] for pod in pods}
        if len(pods) == 1:
            break
        sleep(0.2)
    assert isolate.run('true').success
