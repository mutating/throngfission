import pytest
from throng.errors import ThrongError

from throngfission.execution.errors import (
    APIError,
    CleanupError,
    FissionError,
    IsolateClosedError,
)


@pytest.mark.parametrize('error_type', [FissionError, APIError, CleanupError, IsolateClosedError])
def test_public_failures_share_one_catchable_base(error_type):
    """Clients can catch plugin failures without treating them as cancellation."""
    assert issubclass(error_type, FissionError)
    assert issubclass(error_type, RuntimeError)
    assert issubclass(error_type, ThrongError)
