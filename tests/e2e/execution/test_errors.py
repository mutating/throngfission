import pytest
from throng.errors import CannotInstallDependencyError


def test_failed_pip_install_preserves_a_usable_isolate(isolate):
    """pip failure exposes throng's expected exception without losing the workspace."""
    assert isolate.run('sh -c "printf retained > marker"').success
    with pytest.raises(CannotInstallDependencyError):
        isolate.install('--no-index', 'throngfission-intentionally-nonexistent-package')
    assert isolate.run('cat marker').stdout == 'retained'
