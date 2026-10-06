from runpy import run_module
from unittest.mock import Mock

import pytest


def test_module_entrypoint_returns_the_cli_exit_status(monkeypatch):
    """Python module invocation delegates to the same CLI and preserves its exit status."""
    main = Mock(return_value=7)
    monkeypatch.setattr('throngfission.cli.main', main)
    with pytest.raises(SystemExit) as error:
        run_module('throngfission', run_name='__main__')
    assert error.value.code == 7
    main.assert_called_once_with()
