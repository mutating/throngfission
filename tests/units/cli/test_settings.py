import pytest

from throngfission.cli.settings import SetupSettings


@pytest.mark.parametrize('changes', [{'setup_timeout': 0.0}, {'setup_poll_interval': float('nan')}, {'diagnostic_timeout': float('inf')}, {'image_timeout': -1}, {'image_cleanup_ttl': 0}, {'certificate_days': 0}])
def test_setup_deadlines_must_be_positive_and_finite(changes):
    """Installation waits and cleanup cannot be disabled accidentally by invalid thresholds."""
    with pytest.raises(ValueError, match='finite and positive'):
        SetupSettings(**changes).validate()


def test_setup_settings_are_loaded_and_typed_by_skelet(monkeypatch):
    """Setup shares the plugin's environment prefix and converts its own numeric settings."""
    monkeypatch.setenv('THRONGFISSION_SETUP_TIMEOUT', '42.5')
    monkeypatch.setenv('THRONGFISSION_IMAGE_TIMEOUT', '12')
    monkeypatch.setenv('THRONGFISSION_WORK_DIR', '/external/builds')
    settings = SetupSettings.from_env()
    settings.validate()
    assert (settings.setup_timeout, settings.image_timeout, settings.work_dir) == (42.5, 12, '/external/builds')
