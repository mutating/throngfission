from throng.errors import ThrongError


class FissionError(ThrongError, RuntimeError):
    """A configuration, protocol or execution infrastructure failure."""


class IsolateClosedError(FissionError):
    """The isolate was destroyed or its session was lost."""


class CleanupError(FissionError):
    """Deletion could not be confirmed; cluster cleanup will retry."""


class APIError(FissionError):
    def __init__(self, status: int, message: str) -> None:
        self.status = status
        super().__init__(message)
