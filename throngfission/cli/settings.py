from math import isfinite

from skelet import EnvSource, Storage


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
