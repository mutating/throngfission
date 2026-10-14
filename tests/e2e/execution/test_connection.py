import pytest

from throngfission.execution.errors import IsolateClosedError


def test_aborted_transport_never_reconnects_or_replays(isolate, manager):
    """An aborted socket invalidates further commands without replacing its pod."""
    assert isolate.run('sh -c "printf once > marker"').success
    isolate.connection.close()
    with pytest.raises(IsolateClosedError):
        isolate.run('sh -c "printf twice >> marker"')
    pods = manager.cluster.api.request('GET', f'{manager.cluster.core}pods', query=[('labelSelector', f'throngfission.io/installation={manager.settings.function_name},managed=false')])['items']
    assert {pod['metadata']['uid'] for pod in pods} <= {isolate.uid}
