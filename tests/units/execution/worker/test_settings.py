from throngfission.execution.worker.settings import WorkerSettings


def test_worker_settings_use_skelet(monkeypatch):
    """Worker and janitor settings receive the same typed environment values."""
    monkeypatch.setenv('THRONGFISSION_SECRET', 'credential')
    monkeypatch.setenv('THRONGFISSION_SESSION_TIMEOUT', '42.5')
    monkeypatch.setenv('THRONGFISSION_JANITOR_REQUEST_TIMEOUT', '7.5')
    monkeypatch.setenv('THRONGFISSION_JANITOR_UNREACHABLE_TIMEOUT', '145.5')
    monkeypatch.setenv('THRONGFISSION_API_RECOVERY_TIMEOUT', '75.5')
    monkeypatch.setenv('THRONGFISSION_POLL_INTERVAL', '0.25')
    monkeypatch.setenv('THRONGFISSION_TRANSFER_CHUNK_SIZE', '524288')
    monkeypatch.setenv('THRONGFISSION_WEBSOCKET_MAX_MESSAGE_SIZE', '2097152')
    settings = WorkerSettings.from_env()
    assert settings.secret == 'credential'
    assert settings.session_timeout == 42.5
    assert settings.janitor_request_timeout == 7.5
    assert settings.janitor_unreachable_timeout == 145.5
    assert settings.api_recovery_timeout == 75.5
    assert settings.poll_interval == 0.25
    assert settings.transfer_chunk_size == 524288
    assert settings.websocket_max_message_size == 2097152
