import json
from contextlib import ExitStack
from time import sleep
from uuid import uuid4

import pytest
from cantok import TimeoutToken
from locklib import LockTraceWrapper, SmartLock

from throngfission import FissionManager, Settings
from throngfission.cli.settings import SetupSettings
from throngfission.cli.setup import Setup
from throngfission.execution.api import Kubernetes


def pytest_addoption(parser):
    """Allow deliberate use of an existing cluster instead of a disposable K3s."""
    parser.addoption('--fission-existing', action='store_true', help='Use THRONGFISSION_* / kubeconfig; do not install Kubernetes or Fission.')


@pytest.fixture(scope='session')
def kubernetes_cluster(request, tmp_path_factory):
    """Create three isolated K3s nodes and remove their containers, volumes and network."""
    if request.config.getoption('--fission-existing'):
        yield None
        return
    # Defer Docker-only dependencies for runs against an existing cluster.
    from testcontainers.community.k3s import K3SContainer  # noqa: PLC0415
    from testcontainers.core.container import DockerContainer  # noqa: PLC0415
    from testcontainers.core.network import Network  # noqa: PLC0415

    with ExitStack() as stack:
        network = stack.enter_context(Network())
        token = uuid4().hex
        server = K3SContainer('rancher/k3s:v1.36.3-k3s1', enable_cgroup_mount=False)
        server.with_network(network).with_network_aliases('server').with_env('K3S_TOKEN', token).with_env('K3S_URL', '')
        stack.callback(server.stop)
        nodes = [server.start()]
        for index in range(2):
            agent = DockerContainer('rancher/k3s:v1.36.3-k3s1').with_network(network)
            agent.with_env('K3S_URL', 'https://server:6443').with_env('K3S_TOKEN', token)
            agent.with_command(f'agent --node-name=throngfission-agent-{index!s}')
            agent.with_kwargs(privileged=True).with_tmpfs_mount('/run').with_tmpfs_mount('/var/run')
            stack.callback(agent.stop)
            nodes.append(agent.start())
        container = server.get_wrapped_container()
        ready = TimeoutToken(180)
        while True:
            ready.check()
            result = container.exec_run(['kubectl', 'get', 'nodes', '-o', 'json'])
            assert result.exit_code == 0, result.output.decode()
            listed = json.loads(result.output)['items']
            if len(listed) == 3 and all(any(condition['type'] == 'Ready' and condition['status'] == 'True' for condition in node['status']['conditions']) for node in listed):
                break
            sleep(0.5)
        kubeconfig = tmp_path_factory.mktemp('kubernetes') / 'config'
        kubeconfig.write_text(server.config_yaml())
        kubeconfig.chmod(0o600)
        try:
            yield kubeconfig, nodes
        finally:
            kubeconfig.unlink(missing_ok=True)


@pytest.fixture(scope='session')
def setup_cluster(kubernetes_cluster):
    """Exercise first installation on an empty three-node cluster through the public setup API."""
    if kubernetes_cluster is None:
        yield None
        return
    kubeconfig, nodes = kubernetes_cluster
    settings = Settings(kubeconfig=str(kubeconfig))
    options = SetupSettings(work_dir=str(kubeconfig.parent))
    api = Kubernetes(settings)
    try:
        assert not api.request('GET', '/apis/apps/v1/deployments', query=[('labelSelector', 'release=fission-v1-27-0')])['items']
    finally:
        api.close()
    Setup(settings, options).run()
    yield settings, options, nodes


@pytest.fixture(scope='session')
def fission_cluster(setup_cluster):
    """Share the installation exercised by setup; individual tests retain separate functions."""
    if setup_cluster is None:
        return {}
    settings, _, _ = setup_cluster
    return {'kubeconfig': settings.kubeconfig, 'namespace': settings.namespace}


@pytest.fixture
def manager(fission_cluster, tmp_path):
    """Give each test an installation and verify all its resources disappear."""
    settings = Settings.from_env()
    settings.name = f'throngfission-e2e-{uuid4().hex[:8]}'
    settings.heartbeat_interval = 1.0
    settings.session_timeout = 45.0
    for key, value in fission_cluster.items():
        setattr(settings, key, value)
    manager = FissionManager(tmp_path, settings=settings)
    try:
        yield manager
    finally:
        if manager._cluster is not None:
            try:
                manager.uninstall()
                token = TimeoutToken(120)
                paths = [f'{manager.cluster.core}pods', f'/apis/apps/v1/namespaces/{settings.namespace}/deployments', f'/apis/batch/v1/namespaces/{settings.namespace}/jobs']
                for path in paths:
                    while manager.cluster.api.request('GET', path, query=[('labelSelector', f'throngfission.io/installation={settings.name}')])['items']:
                        token.check()
                        sleep(0.2)
            finally:
                manager.cluster.api.close()


@pytest.fixture
def isolate(manager):
    """Allocate a worker with traced locks and always confirm its deletion."""
    isolate = manager.get(manager.read(), TimeoutToken(180))
    isolate.lock = LockTraceWrapper(SmartLock())
    isolate.cleanup_lock = LockTraceWrapper(SmartLock())
    isolate.connection.lock = LockTraceWrapper(SmartLock())
    try:
        yield isolate
    finally:
        isolate.kill()
