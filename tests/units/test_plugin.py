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


@pytest.mark.parametrize('image', ['registry.example.com/team/fission-python:debian', 'registry.example.com:5000/team/runtime@sha256:' + 'a' * 64])
def test_discovered_plugin_reads_the_runtime_image_from_skelet(monkeypatch, image):
    """The public throng entry point preserves a configured image tag or digest."""
    monkeypatch.setenv('THRONGFISSION_IMAGE', image)
    manager = throng()['fission']
    assert manager.settings.image == image
    assert manager._cluster is None
