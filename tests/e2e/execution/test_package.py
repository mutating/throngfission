from cantok import TimeoutToken

from throngfission import FissionManager


def test_independent_clients_reuse_one_worker_bundle(manager):
    """A second client reuses shared code and credentials while receiving a new pod."""
    first = manager.get(manager.read(), TimeoutToken(180))
    second_manager = FissionManager(manager.path, settings=manager.settings)
    try:
        owner = manager.cluster.api.request('GET', f'{manager.cluster.core}configmaps/{manager.settings.function_name}')
        second = second_manager.get(second_manager.read(), TimeoutToken(180))
        try:
            assert second.uid != first.uid
            assert second_manager.cluster.secret == manager.cluster.secret
            reused = second_manager.cluster.api.request('GET', f'{second_manager.cluster.core}configmaps/{manager.settings.function_name}')
            assert reused['metadata']['uid'] == owner['metadata']['uid']
            assert first.run('python -c "print(1)"').stdout == '1\n'
            assert second.run('python -c "print(2)"').stdout == '2\n'
        finally:
            second.kill()
    finally:
        first.kill()
        second_manager.cluster.api.close()
