from importlib.metadata import distribution
from unittest.mock import Mock

import pytest

from throngfission.cli.main import main


def test_installed_console_entrypoint_loads_cli(monkeypatch, capsys):
    """Installed command metadata resolves to working help after moving the CLI module."""
    entrypoint = next(point for point in distribution('throngfission').entry_points if point.group == 'console_scripts' and point.name == 'throngfission')
    monkeypatch.setattr('sys.argv', ['throngfission', '--help', '--kubeconfig', '/missing/cluster'])
    assert entrypoint.load()() == 0
    assert 'throngfission setup' in capsys.readouterr().out


@pytest.mark.parametrize('arguments', [[], ['--help'], ['setup', '--help']])
def test_help_never_connects_to_a_cluster(arguments, monkeypatch, capsys):
    """Help and an empty invocation describe setup without contacting Kubernetes."""
    monkeypatch.setattr('sys.argv', ['throngfission', *arguments])
    setup = Mock()
    monkeypatch.setattr('throngfission.cli.main.Setup', setup)
    assert main() == 0
    assert 'throngfission setup' in capsys.readouterr().out
    setup.assert_not_called()


@pytest.mark.parametrize('arguments', [['other'], ['setup', '--typo'], ['setup', 'unexpected'], ['setup', '--namespace'], ['--help', 'false'], ['setup', '--name', 'obsolete'], ['setup', '--image', 'debian']])
def test_invalid_commands_fail_before_installation(arguments, monkeypatch):
    """Unknown commands and malformed arguments cannot fall back to default setup settings."""
    monkeypatch.setattr('sys.argv', ['throngfission', *arguments])
    setup = Mock()
    monkeypatch.setattr('throngfission.cli.main.Setup', setup)
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
    setup.assert_not_called()


@pytest.mark.parametrize(('option', 'message'), [
    ('--namespace', 'throngfission: error: --namespace requires a value\n'),
    ('--setup-timeout', 'throngfission: error: --setup-timeout requires a value\n'),
    ('--anti-affinity', 'throngfission: error: --anti-affinity requires a value\n'),
])
def test_missing_option_value_reports_the_complete_flag(option, message, monkeypatch, capsys):
    """The CLI preserves the exact error line, including hyphenated option names."""
    monkeypatch.setattr('sys.argv', ['throngfission', 'setup', option])
    setup = Mock()
    monkeypatch.setattr('throngfission.cli.main.Setup', setup)
    with pytest.raises(SystemExit) as caught:
        main()
    assert caught.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ''
    assert captured.err.splitlines(keepends=True)[-1] == message
    setup.assert_not_called()


def test_cli_values_override_environment_through_skelet(monkeypatch):
    """Typed CLI overrides win over environment settings and reach the setup runner."""
    monkeypatch.setenv('THRONGFISSION_SETUP_TIMEOUT', '77.5')
    monkeypatch.setenv('THRONGFISSION_NAMESPACE', 'environment')
    monkeypatch.setenv('THRONGFISSION_POOL_SIZE', '4')
    monkeypatch.setenv('THRONGFISSION_ANTI_AFFINITY', 'none')
    monkeypatch.setattr('sys.argv', ['throngfission', 'setup', '--namespace', 'command', '--anti-affinity', 'hard', '--image-timeout', '90'])
    runner = Mock()
    setup = Mock(return_value=runner)
    monkeypatch.setattr('throngfission.cli.main.Setup', setup)
    assert main() == 0
    settings, options = setup.call_args.args
    assert (settings.namespace, settings.pool_size, options.setup_timeout, options.image_timeout) == ('command', 4, 77.5, 90)
    assert settings.anti_affinity == 'hard'
    runner.run.assert_called_once_with()
