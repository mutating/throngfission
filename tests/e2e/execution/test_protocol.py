import pytest

from throngfission.execution.errors import FissionError, IsolateClosedError


def test_worker_protocol_error_invalidates_the_session(isolate):
    """An unsupported wire operation returns a worker error and forbids later RPCs."""
    with pytest.raises(FissionError, match='Unknown operation'):
        isolate.connection.request({'op': 'unsupported'})
    with pytest.raises(IsolateClosedError):
        isolate.run('true')
