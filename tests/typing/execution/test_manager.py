import pytest
from cantok import AbstractToken
from throng import AbstractIsolate, AbstractManager, throng

from throngfission import FissionIsolate, FissionManager, Settings
from throngfission.execution.isolate import RunResult


@pytest.mark.mypy_testing
def mypy_concrete_results(manager: FissionManager, token: AbstractToken) -> None:
    isolate: FissionIsolate = manager.get(b'', token=token)
    result: RunResult = isolate.run('command', token=token, exception=True)
    cancelled: bool = result.killed_by_token
    reveal_type(isolate)  # N: Revealed type is "throngfission.execution.isolate.FissionIsolate"
    reveal_type(result)  # N: Revealed type is "throngfission.execution.isolate.RunResult"
    reveal_type(cancelled)  # N: Revealed type is "builtins.bool"
    isolate.install('package', token=token)


@pytest.mark.mypy_testing
def mypy_public_api(token: AbstractToken) -> None:
    manager = FissionManager('.', [], ['prepare'], ['package'], settings=Settings())
    abstract: AbstractManager = manager
    manager.run('command', token=token, exception=ValueError)
    manager.chain('first', 'second', token=token, exception=ValueError('failed'))
    abstract.get(b'', token).install('package', token=token)
    with manager.scope(token) as isolate:
        interface: AbstractIsolate = isolate
        interface.run('command', token=token, exception=True)
    throng('.', prepare=['prepare'], packages=['package'])['fission'].run('command', exception=True)


@pytest.mark.mypy_testing
def mypy_rejects_invalid_arguments(manager: FissionManager, isolate: FissionIsolate) -> None:
    manager.get(b'', token='invalid')  # E: [arg-type]
    isolate.run('command', exception='invalid')  # E: [arg-type]
    isolate.install('package', token='invalid')  # E: [arg-type]
    FissionManager(packages='package')  # E: [arg-type]
    FissionManager(prepare='prepare')  # E: [arg-type]
