import sys
from subprocess import run

import pytest


def test_setup_cli_reads_connection_and_paths_through_skelet(setup_cluster):
    """The installed module runs setup against the prepared cluster using actual CLI arguments."""
    if setup_cluster is None:
        pytest.skip('Setup CLI checks use the disposable cluster.')
    settings, options, _ = setup_cluster
    result = run([sys.executable, '-m', 'throngfission', 'setup', '--kubeconfig', settings.kubeconfig, '--namespace', settings.namespace, '--work-dir', options.work_dir], check=False, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'Setup complete: python, debian, ubuntu.' in result.stdout
    assert 'Preparing ' not in result.stdout
