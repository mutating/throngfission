from subprocess import CalledProcessError
from unittest.mock import Mock

import pytest

from throngfission.errors import FissionError
from throngfission.provision import Provision
from throngfission.settings import Settings, SetupSettings


def test_offline_nodes_are_reported_and_excluded(capsys):
    """Offline nodes remain untouched and are explicitly reported for a later setup run."""
    ready = {'metadata': {'name': 'ready'}, 'status': {'conditions': [{'type': 'Ready', 'status': 'True'}], 'nodeInfo': {'kubeletVersion': 'v1.36.3+k3s1', 'architecture': 'arm64', 'operatingSystem': 'linux'}}}
    api = Mock(request=Mock(return_value={'items': [{'metadata': {'name': 'offline'}, 'status': {'conditions': []}}, ready]}))
    assert Provision(api, Settings(), SetupSettings()).nodes() == [ready]
    assert 'Offline node skipped' in capsys.readouterr().out


@pytest.mark.parametrize('information', [{}, {'kubeletVersion': 'v1.36.3'}, {'architecture': 'armv7'}, {'operatingSystem': 'windows'}])
def test_unusable_clusters_fail_before_provisioning(information):
    """An empty cluster or unsupported Ready node cannot trigger image builds or imports."""
    nodes = [] if not information else [{'metadata': {'name': 'unsupported'}, 'status': {'conditions': [{'type': 'Ready', 'status': 'True'}], 'nodeInfo': {'kubeletVersion': 'v1.36.3+k3s1', 'architecture': 'arm64', 'operatingSystem': 'linux', **information}}}]
    api = Mock(request=Mock(return_value={'items': nodes}))
    with pytest.raises(FissionError, match='K3s'):
        Provision(api, Settings(), SetupSettings()).nodes()


@pytest.mark.parametrize(('kubeconfig', 'context'), [('', ''), ('/config with spaces', 'compute')])
def test_script_options_preserve_connection_and_cleanup_settings(kubeconfig, context):
    """Every script receives the selected cluster and configurable build lifecycle settings."""
    arguments = Provision(Mock(), Settings(kubeconfig=kubeconfig, context=context), SetupSettings(work_dir='/disk with spaces', image_timeout=20, image_cleanup_ttl=3)).command('build_image.sh')
    assert arguments[2:10] == ['--namespace', 'fission-function', '--timeout', '20', '--cleanup-ttl', '3', '--work-dir', '/disk with spaces']
    assert arguments[10:] == (['--kubeconfig', kubeconfig, '--context', context] if kubeconfig else [])


@pytest.mark.parametrize('cached', [[], ['python'], ['python', 'debian', 'ubuntu']])
def test_imports_target_only_nodes_missing_each_preset(cached, monkeypatch):
    """Each image has its own missing-node set; complete nodes require neither Docker nor Jobs."""
    nodes = [
        {'metadata': {'name': 'first'}, 'status': {'nodeInfo': {'architecture': 'arm64'}, 'images': [{'names': [Settings(image=preset).resolved_image]} for preset in cached]}},
        {'metadata': {'name': 'second'}, 'status': {'nodeInfo': {'architecture': 'arm64'}, 'images': [{'names': [Settings(image=preset).resolved_image]} for preset in ['python', 'debian', 'ubuntu']]}},
        {'metadata': {'name': 'third'}, 'status': {'nodeInfo': {'architecture': 'amd64'}, 'images': [{'names': [Settings(image=preset).resolved_image]} for preset in cached]}},
    ]
    process = Mock()
    monkeypatch.setattr('throngfission.provision.run', process)
    api = Mock(request=Mock(return_value={'status': {'images': [{'names': [Settings(image=preset).resolved_image for preset in ['python', 'debian', 'ubuntu']]}]}}))
    Provision(api, Settings(), SetupSettings()).images(nodes)
    assert len(process.call_args_list) == 2 * (3 - len(cached))
    for call in process.call_args_list:
        arguments = call.args[0]
        assert arguments[arguments.index('--nodes') + 1] in ['first', 'third']
        assert arguments[arguments.index('--image') + 1] not in cached
        assert call.kwargs == {'check': True}


def test_failed_import_does_not_mark_the_node_prepared(monkeypatch):
    """An interrupted importer propagates its error without leaving a successful checkpoint."""
    process = Mock(side_effect=CalledProcessError(1, 'import'))
    monkeypatch.setattr('throngfission.provision.run', process)
    api = Mock()
    with pytest.raises(CalledProcessError):
        Provision(api, Settings(), SetupSettings()).images([{'metadata': {'name': 'new'}, 'status': {'nodeInfo': {'architecture': 'arm64'}}}])
    api.request.assert_not_called()
    process.assert_called_once()


@pytest.mark.parametrize('inventory', [None, [], [{}], [{'names': None}]])
def test_import_waits_until_kubelet_reports_the_image(inventory, monkeypatch):
    """Absent or nameless cached images cannot acknowledge a completed import."""
    api = Mock(request=Mock(side_effect=[{'status': {'images': inventory}}, {'status': {'images': [{'names': ['requested']}]}}]))
    sleeper = Mock()
    monkeypatch.setattr('throngfission.provision.sleep', sleeper)
    Provision(api, Settings(), SetupSettings(setup_poll_interval=0.25)).wait_image('new-node', 'requested')
    sleeper.assert_called_once_with(0.25)
    assert api.request.call_count == 2
    api.request.assert_called_with('GET', '/api/v1/nodes/new-node')


@pytest.mark.parametrize('inventory', [None, [], [{}], [{'names': None}]])
def test_removed_image_names_do_not_break_cache_repair(inventory, monkeypatch):
    """Kubelet's nameless entries after image deletion count as missing, not as malformed nodes."""
    process = Mock()
    monkeypatch.setattr('throngfission.provision.run', process)
    api = Mock(request=Mock(return_value={'status': {'images': [{'names': [Settings(image=preset).resolved_image for preset in ['python', 'debian', 'ubuntu']]}]}}))
    Provision(api, Settings(), SetupSettings()).images([{'metadata': {'name': 'node'}, 'status': {'nodeInfo': {'architecture': 'arm64'}, 'images': inventory}}])
    assert [call.args[0][call.args[0].index('--image') + 1] for call in process.call_args_list] == ['python', 'debian', 'ubuntu']


@pytest.mark.parametrize(('configured', 'distribution', 'expected'), [('auto', 'Alpine Linux', ''), ('', 'Ubuntu 26.04 LTS', ''), ('custom', 'Ubuntu 26.04 LTS', 'custom')])
def test_apparmor_detection_preserves_explicit_choices(configured, distribution, expected, monkeypatch):
    """Unaffected nodes and explicit profiles do not run the Ubuntu workaround installer."""
    process = Mock()
    monkeypatch.setattr('throngfission.provision.run', process)
    nodes = [{'status': {'nodeInfo': {'osImage': distribution}}}]
    assert Provision(Mock(), Settings(), SetupSettings(apparmor_profile=configured)).apparmor(nodes) == expected
    process.assert_not_called()


@pytest.mark.parametrize(('configured', 'prepared'), [('auto', False), ('throngfission-stacked-v1', False), ('auto', True)])
def test_apparmor_checkpoint_is_written_only_after_success(configured, prepared, monkeypatch):
    """The workaround is installed only on unprepared nodes and subsequent setup is read-only."""
    annotations = {'throngfission.io/apparmor-profile': 'throngfission-stacked-v1'}
    nodes = [{'metadata': {'name': 'pi', 'annotations': annotations if prepared else {}}, 'status': {'nodeInfo': {'osImage': 'Ubuntu 26.04 LTS'}}}]
    process = Mock()
    monkeypatch.setattr('throngfission.provision.run', process)
    api = Mock()
    assert Provision(api, Settings(), SetupSettings(apparmor_profile=configured)).apparmor(nodes) == 'throngfission-stacked-v1'
    assert process.call_count == api.request.call_count == (0 if prepared else 1)
    if not prepared:
        assert process.call_args.args[0][-2:] == ['--nodes', 'pi']
        api.request.assert_called_once_with('PATCH', '/api/v1/nodes/pi', {'metadata': {'annotations': annotations}})
