import pytest

from throngfission.execution.protocol import decode, string


@pytest.mark.parametrize('value', ['[]', 'null', 'false', '1', '"text"', 'invalid'])
def test_reject_non_objects(value):
    """Every protocol frame must contain a JSON object."""
    with pytest.raises(ValueError, match=r'JSON object|Expecting value'):
        decode(value)


def test_decode_object():
    """Object payloads preserve their typed JSON fields."""
    assert decode('{"value": "hello"}') == {'value': 'hello'}
    assert string({'value': 'hello'}, 'value') == 'hello'


@pytest.mark.parametrize(('message', 'exception'), [({}, KeyError), ({'value': 1}, ValueError)])
def test_require_string_fields(message, exception):
    """Missing and incorrectly typed fields fail before dispatch."""
    with pytest.raises(exception):
        string(message, 'value')
