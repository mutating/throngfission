import pytest

from throngfission.execution.settings import Settings


@pytest.mark.parametrize(('image', 'reference'), [
    ('python', 'ghcr.io/fission/python-env@sha256:f6c8c470c084ce7ecadcaaca76717a7ecaf24b64f5d85a94b1bafa45529b814c'),
    ('debian', 'docker.io/library/throngfission-debian:py313-bookworm-45815b42-v1'),
    ('ubuntu', 'docker.io/library/throngfission-ubuntu:py312-noble-45815b42-v1'),
    ('python:3.13', 'python:3.13'),
    (f"registry.example:5000/team/ubuntu@sha256:{'a' * 64}", f"registry.example:5000/team/ubuntu@sha256:{'a' * 64}"),
])
def test_image_presets_and_explicit_references(image, reference, monkeypatch):
    """Only exact preset names expand, equally for explicit and skelet settings."""
    monkeypatch.setenv('THRONGFISSION_IMAGE', image)
    for settings in [Settings(image=image), Settings.from_env()]:
        assert settings.image == image
        assert settings.resolved_image == reference


def test_default_image_and_changed_selection():
    """The default stays the official runtime and changing an image updates resolution."""
    settings = Settings()
    assert settings.image == 'python'
    assert settings.anti_affinity == 'soft'
    assert settings.resolved_image.startswith('ghcr.io/fission/python-env@sha256:')
    settings.image = 'ubuntu'
    assert settings.resolved_image == 'docker.io/library/throngfission-ubuntu:py312-noble-45815b42-v1'


@pytest.mark.parametrize('mode', ['none', 'soft', 'hard'])
def test_anti_affinity_is_loaded_by_skelet(mode, monkeypatch):
    """Each scheduling mode is accepted from the plugin's environment settings."""
    monkeypatch.setenv('THRONGFISSION_ANTI_AFFINITY', mode)
    settings = Settings.from_env()
    settings.validate()
    assert settings.anti_affinity == mode


@pytest.mark.parametrize('mode', ['', 'preferred', 'HARD', 'off'])
def test_invalid_anti_affinity_is_rejected(mode):
    """An unknown scheduling mode fails before any cluster resources are created."""
    with pytest.raises(ValueError, match='anti_affinity must be'):
        Settings(anti_affinity=mode).validate()


@pytest.mark.parametrize('preset', ['python', 'debian', 'ubuntu'])
def test_default_function_names_follow_the_resolved_image(preset, monkeypatch):
    """A preset and its full image reference share one configurable function name."""
    reference = Settings(image=preset).resolved_image
    assert Settings(image=preset).function_name == Settings(image=reference).function_name == f'throngfission-{preset}'
    monkeypatch.setenv(f'THRONGFISSION_{preset.upper()}_NAME', 'configured')
    monkeypatch.setenv('THRONGFISSION_IMAGE', preset)
    assert Settings.from_env().function_name == 'configured'
    assert Settings(name='explicit', image=preset).function_name == 'explicit'


def test_custom_images_have_distinct_stable_function_names():
    """Explicit custom images require no manual names and cannot overwrite another runtime."""
    first = Settings(image='registry/team/runtime:one').function_name
    assert first == Settings(image='registry/team/runtime:one').function_name
    assert first != Settings(image='registry/team/runtime:two').function_name
    assert first.startswith('throngfission-')


@pytest.mark.parametrize('changes', [
    {'pool_size': 0}, {'heartbeat_interval': 0.0}, {'request_timeout': -1.0},
    {'upload_timeout': 0.0}, {'upload_timeout': float('inf')},
    {'poll_interval': 0.0}, {'cleanup_timeout': 0.0}, {'api_recovery_timeout': 0.0}, {'session_timeout': 20.0},
    {'function_idle_timeout': 35}, {'stream_idle_timeout': 0}, {'socket_tracker_interval': 0},
    {'janitor_request_timeout': 0.0}, {'janitor_unreachable_timeout': 0.0},
    {'heartbeat_interval': float('nan')}, {'request_timeout': float('inf')},
    {'stream_max_duration': -1}, {'termination_grace_period': -1}, {'termination_grace_period': 0},
    {'janitor_success_history': -1}, {'janitor_failure_history': -1},
    {'janitor_job_ttl': -1}, {'janitor_backoff_limit': -1},
    {'transfer_chunk_size': 0}, {'transfer_chunk_size': -1},
    {'websocket_max_message_size': 0}, {'transfer_chunk_size': 10 * 1024 * 1024},
])
def test_invalid_intervals(changes):
    """Reject settings that prevent allocation or expire a healthy heartbeat."""
    with pytest.raises(ValueError, match=r'positive|must exceed|non-negative|must fit'):
        Settings(**changes).validate()


@pytest.mark.parametrize(('idle', 'stream'), [(120, 60), (36, 120), (9, 6)])
def test_idle_threshold_accounts_for_fission_tap_batching(idle, stream):
    """Idle reaping allows a full router keepalive interval and its delayed flush."""
    Settings(function_idle_timeout=idle, stream_idle_timeout=stream).validate()


@pytest.mark.parametrize(('name', 'raw', 'expected'), [
    ('kubeconfig', '/tmp/cluster.yaml', '/tmp/cluster.yaml'), ('context', 'compute', 'compute'),
    ('function_idle_timeout', '160', 160), ('stream_idle_timeout', '80', 80),
    ('request_timeout', '25.0', 25.0), ('api_recovery_timeout', '75.5', 75.5),
    ('upload_timeout', '30.0', 30.0),
    ('stream_max_duration', '300', 300), ('socket_tracker_interval', '3', 3),
    ('termination_grace_period', '5', 5), ('janitor_schedule', '*/2 * * * *', '*/2 * * * *'),
    ('janitor_request_timeout', '7.5', 7.5), ('janitor_unreachable_timeout', '145.5', 145.5),
    ('janitor_success_history', '2', 2), ('janitor_failure_history', '3', 3),
    ('janitor_job_ttl', '90', 90), ('janitor_backoff_limit', '2', 2),
    ('janitor_cpu_request', '25m', '25m'), ('janitor_memory_request', '48Mi', '48Mi'),
    ('transfer_chunk_size', '524288', 524288), ('websocket_max_message_size', '2097152', 2097152),
])
def test_thresholds_are_typed_skelet_settings(monkeypatch, name, raw, expected):
    """Each resource and cleanup threshold can be overridden through skelet."""
    monkeypatch.setenv(f'THRONGFISSION_{name.upper()}', raw)
    settings = Settings.from_env()
    settings.validate()
    assert getattr(settings, name) == expected


def test_environment_is_read_for_each_manager(monkeypatch):
    """Fresh skelet sources observe environment changes between managers."""
    monkeypatch.setenv('THRONGFISSION_POOL_SIZE', '4')
    first = Settings.from_env()
    first.validate()
    monkeypatch.setenv('THRONGFISSION_POOL_SIZE', '6')
    assert first.pool_size == 4
    assert Settings.from_env().pool_size == 6


@pytest.mark.parametrize(('chunk_size', 'message_size', 'valid'), [(1, 33, True), (3, 33, True), (4, 33, False), (3, 32, False)])
def test_transfer_limit_includes_base64_padding_and_envelope(chunk_size, message_size, valid):
    """The worker limit must fit the complete JSON message, including base64 padding."""
    settings = Settings(transfer_chunk_size=chunk_size, websocket_max_message_size=message_size)
    if valid:
        settings.validate()
    else:
        with pytest.raises(ValueError, match='must fit'):
            settings.validate()
