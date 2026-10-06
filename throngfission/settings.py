import json
from hashlib import sha256
from math import isfinite

from skelet import EnvSource, Storage


class Settings(Storage):
    """Connection and scheduling settings; capacity waiting has no deadline."""

    namespace: str = 'fission-function'
    name: str = ''
    python_name: str = 'throngfission-python'
    debian_name: str = 'throngfission-debian'
    ubuntu_name: str = 'throngfission-ubuntu'
    kubeconfig: str = ''
    context: str = ''
    router_namespace: str = 'fission'
    router_service: str = 'router:80'
    router_url: str = ''
    image: str = 'python'
    cpu_request: str = '250m'
    memory_request: str = '256Mi'
    anti_affinity: str = 'soft'
    pool_size: int = 2
    heartbeat_interval: float = 5.0
    session_timeout: float = 60.0
    request_timeout: float = 30.0
    upload_timeout: float = 45.0
    api_recovery_timeout: float = 60.0
    transfer_chunk_size: int = 1024 * 1024
    websocket_max_message_size: int = 10 * 1024 * 1024
    poll_interval: float = 0.1
    cleanup_timeout: float = 120.0
    function_idle_timeout: int = 120
    stream_idle_timeout: int = 60
    stream_max_duration: int = 0
    socket_tracker_interval: int = 1
    termination_grace_period: int = 1
    janitor_schedule: str = '* * * * *'
    janitor_request_timeout: float = 10.0
    janitor_unreachable_timeout: float = 120.0
    janitor_success_history: int = 0
    janitor_failure_history: int = 1
    janitor_job_ttl: int = 60
    janitor_backoff_limit: int = 0
    janitor_cpu_request: str = '10m'
    janitor_memory_request: str = '32Mi'

    @classmethod
    def from_env(cls) -> 'Settings':
        return cls(_sources=[EnvSource[object](prefix='THRONGFISSION_')])

    @property
    def resolved_image(self) -> str:
        """Resolve a runtime preset while preserving explicit image references."""
        return {
            'python': 'ghcr.io/fission/python-env@sha256:f6c8c470c084ce7ecadcaaca76717a7ecaf24b64f5d85a94b1bafa45529b814c',
            'debian': 'docker.io/library/throngfission-debian:py313-bookworm-45815b42-v1',
            'ubuntu': 'docker.io/library/throngfission-ubuntu:py312-noble-45815b42-v1',
        }.get(self.image, self.image)

    @property
    def function_name(self) -> str:
        """Select an independent shared function without requiring a launch-time name."""
        if self.name:
            return self.name
        for preset in ('python', 'debian', 'ubuntu'):
            if self.resolved_image == Settings(image=preset).resolved_image:
                return str(getattr(self, preset + '_name'))
        return 'throngfission-' + sha256(self.resolved_image.encode()).hexdigest()[:12]

    def validate(self) -> None:
        if self.anti_affinity not in ('none', 'soft', 'hard'):
            raise ValueError('anti_affinity must be none, soft or hard.')
        if self.pool_size < 1:
            raise ValueError('pool_size must be positive for the Fission poolmgr executor.')
        if self.transfer_chunk_size < 1:
            raise ValueError('transfer_chunk_size must be positive.')
        if 4 * ((self.transfer_chunk_size + 2) // 3) + len(json.dumps({'op': 'upload', 'state': ''})) > self.websocket_max_message_size:
            raise ValueError('websocket_max_message_size must fit a base64 encoded transfer_chunk_size and its JSON envelope.')
        intervals = (self.heartbeat_interval, self.session_timeout, self.request_timeout, self.upload_timeout, self.api_recovery_timeout, self.poll_interval, self.cleanup_timeout, self.function_idle_timeout, self.stream_idle_timeout, self.socket_tracker_interval, self.termination_grace_period, self.janitor_request_timeout, self.janitor_unreachable_timeout)
        if any(not isfinite(value) or value <= 0 for value in intervals):
            raise ValueError('All timeouts and polling intervals must be finite and positive.')
        thresholds = (self.stream_max_duration, self.janitor_success_history, self.janitor_failure_history, self.janitor_job_ttl, self.janitor_backoff_limit)
        if any(value < 0 for value in thresholds):
            raise ValueError('Duration limits, grace periods and job retention settings must be non-negative.')
        if self.session_timeout <= self.heartbeat_interval + self.request_timeout:
            raise ValueError('session_timeout must exceed heartbeat_interval + request_timeout.')
        # Fission 1.27 caps router taps at 30 seconds and flushes them every 5.
        if self.function_idle_timeout <= min(self.stream_idle_timeout / 2, 30) + 5:
            raise ValueError('function_idle_timeout must exceed the Fission router tap interval plus its 5-second batching interval.')


class WorkerSettings(Storage):
    namespace: str = 'fission-function'
    name: str = 'throngfission'
    secret: str
    session_timeout: float = 60.0
    transfer_chunk_size: int = 1024 * 1024
    websocket_max_message_size: int = 10 * 1024 * 1024
    api_recovery_timeout: float = 60.0
    poll_interval: float = 0.1
    janitor_request_timeout: float = 10.0
    janitor_unreachable_timeout: float = 120.0
    pod_name: str = ''
    pod_uid: str = ''

    @classmethod
    def from_env(cls) -> 'WorkerSettings':
        return cls(_sources=[EnvSource[object](prefix='THRONGFISSION_')])


class SetupSettings(Storage):
    """Installation deadlines and local build storage, independent of isolate waiting."""

    setup_timeout: float = 600.0
    setup_poll_interval: float = 1.0
    diagnostic_timeout: float = 300.0
    image_timeout: int = 600
    image_cleanup_ttl: int = 60
    certificate_days: int = 3650
    work_dir: str = '.'
    apparmor_profile: str = 'auto'

    @classmethod
    def from_env(cls) -> 'SetupSettings':
        return cls(_sources=[EnvSource[object](prefix='THRONGFISSION_')])

    def validate(self) -> None:
        intervals = (self.setup_timeout, self.setup_poll_interval, self.diagnostic_timeout, self.image_timeout, self.image_cleanup_ttl, self.certificate_days)
        if any(not isfinite(value) or value <= 0 for value in intervals):
            raise ValueError('Setup deadlines, polling intervals and retention must be finite and positive.')
# mypy: disable-error-code=misc
# skelet's dynamic Storage descriptors trigger an unlocated disallow-any-expr
# diagnostic on subclasses in mypy 1.14; field types remain checked normally.
