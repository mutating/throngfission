import sys
from subprocess import run


def test_module_help_works_without_cluster_access():
    """Module help exits successfully even with a deliberately unusable kubeconfig path."""
    result = run([sys.executable, '-m', 'throngfission', '--help', '--kubeconfig', '/missing/cluster'], check=False, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert 'throngfission setup' in result.stdout
