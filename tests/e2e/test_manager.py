from cantok import TimeoutToken


def test_manager_runs_and_cleans_up(manager):
    """A complete manager run executes on Fission and releases its worker pod."""
    result = manager.run('python -c "print(42)"', TimeoutToken(180))
    assert result.success
    assert result.stdout == '42\n'
    assert not manager.cluster.api.request('GET', manager.cluster.core + 'pods', query=[('labelSelector', 'throngfission.io/installation=' + manager.settings.function_name + ',managed=false')])['items']


def test_chain_shares_state_then_releases_its_worker(manager):
    """Manager chains preserve command state and release the pod after a failure."""
    results = manager.chain('sh -c "printf retained > marker"', 'cat marker', 'sh -c "exit 7"', token=TimeoutToken(180))
    assert [result.returncode for result in results] == [0, 0, 7]
    assert results[1].stdout == 'retained'
    assert not manager.cluster.api.request('GET', manager.cluster.core + 'pods', query=[('labelSelector', 'throngfission.io/installation=' + manager.settings.function_name + ',managed=false')])['items']
