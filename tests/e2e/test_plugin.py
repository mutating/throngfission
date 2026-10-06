from contextlib import nullcontext

import pytest
from throng import throng

from throngfission import FissionManager, Settings


@pytest.mark.parametrize('fails', [False, True])
def test_discovered_plugin_runs_and_releases_scope(manager, monkeypatch, fails):
    """pristan's public entry point runs remotely and releases normal or failed scopes."""
    for name in Settings.__annotations__:
        monkeypatch.setenv('THRONGFISSION_' + name.upper(), str(getattr(manager.settings, name)))
    (manager.path / 'included').write_text('uploaded')
    (manager.path / 'excluded').write_text('private')
    discovered = throng(manager.path, exclude=['excluded'])['fission']
    assert isinstance(discovered, FissionManager)
    try:
        with pytest.raises(ValueError, match='caller failed') if fails else nullcontext(), discovered.scope as isolate:
            assert isolate.run('cat included').stdout == 'uploaded'
            assert isolate.run('sh -c "test ! -e excluded"').success
            pod = discovered.cluster.owned_pod(isolate.pod, isolate.uid)
            assert pod['status']['phase'] == 'Running'
            if fails:
                raise ValueError('caller failed')
        assert not discovered.cluster.api.request('GET', discovered.cluster.core + 'pods', query=[('labelSelector', 'throngfission.io/installation=' + discovered.settings.name + ',managed=false')])['items']
    finally:
        discovered.uninstall()
        discovered.cluster.api.close()
