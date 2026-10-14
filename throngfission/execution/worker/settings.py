from skelet import EnvSource, Storage


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

# mypy: disable-error-code=misc
# skelet's dynamic Storage descriptors trigger an unlocated disallow-any-expr
# diagnostic on subclasses in mypy 1.14; field types remain checked normally.
