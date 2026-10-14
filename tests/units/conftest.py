from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from throngfission import FissionManager, Settings


@pytest.fixture
def worker(monkeypatch, tmp_path):
    """Exercise real managers and isolates against a scripted worker transport."""
    settings = Settings(poll_interval=0.001)
    connection = Mock()
    cluster = Mock(settings=settings, connect=Mock(return_value=(connection, 'worker', 'uid')))
    factory = Mock(return_value=cluster)
    monkeypatch.setattr('throngfission.execution.manager.Cluster', factory)
    monkeypatch.setattr('throngfission.execution.isolate.Thread', Mock())
    commands = []
    poll = Mock(return_value={'done': True, 'returncode': 0, 'stdout': 'out', 'stderr': ''})

    def request(message):
        if message['op'] == 'start':
            commands.append(message['argv'])
        if message['op'] == 'poll':
            return poll()
        return {}

    connection.request.side_effect = request
    return SimpleNamespace(manager=FissionManager(tmp_path, settings=settings), cluster=cluster, connection=connection, factory=factory, commands=commands, poll=poll)
