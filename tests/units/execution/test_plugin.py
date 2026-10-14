import pytest
from throng import throng

from throngfission import FissionManager


def test_entrypoint_discovers_fission(tmp_path):
    """pristan discovers the installed plugin without contacting Kubernetes."""
    manager = throng(tmp_path, exclude=['*.pyc'])['fission']
    assert isinstance(manager, FissionManager)
    assert manager.path == tmp_path
    assert manager.exclude == ['*.pyc']
    assert manager._cluster is None


@pytest.mark.parametrize('image', ['registry.example.com/team/fission-python:debian', f"registry.example.com:5000/team/runtime@sha256:{'a' * 64}"])
def test_discovered_plugin_reads_the_runtime_image_from_skelet(monkeypatch, image):
    """The public throng entry point preserves a configured image tag or digest."""
    monkeypatch.setenv('THRONGFISSION_IMAGE', image)
    manager = throng()['fission']
    assert manager.settings.image == image
    assert manager._cluster is None


@pytest.mark.parametrize('positional', [False, True])
@pytest.mark.parametrize(('prepare', 'packages'), [(None, None), ([], []), (['setup'], ['pytest', 'package[extra]>=1'])])
def test_entrypoint_passes_preparation_and_packages_offline(tmp_path, positional, prepare, packages):
    """All slot arguments reach Fission without breaking discovery of other backends."""
    managers = throng(tmp_path, ['ignored'], prepare, packages) if positional else throng(tmp_path, exclude=['ignored'], prepare=prepare, packages=packages)
    manager = managers['fission']
    assert isinstance(manager, FissionManager)
    assert manager.path == tmp_path
    assert manager.exclude == ['ignored']
    assert manager.prepare == prepare
    assert manager.packages == packages
    assert manager._cluster is None
    assert 'local' in managers
    assert 'temporary_directory' in managers
