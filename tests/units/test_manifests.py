from unittest.mock import Mock

import pytest
from cantok import CancellationError, SimpleToken

from throngfission.errors import APIError, FissionError
from throngfission.manifests import Manifests, contains
from throngfission.settings import SetupSettings


@pytest.mark.parametrize(('actual', 'desired', 'expected'), [
    ({'spec': {'enabled': True, 'default': 1}}, {'spec': {'enabled': True}}, True),
    ({}, {'required': 1}, False), ([], {}, False), ({}, [], False),
    ([{'value': 1, 'default': 2}], [{'value': 1}], True), ([1, 2], [1], False),
    ([1], [2], False), (True, False, False), (None, None, True),
])
def test_manifest_comparison_allows_defaults_without_ignoring_changes(actual, desired, expected):
    """Server defaults are harmless, but missing fields, changed values and extra list items differ."""
    assert contains(actual, desired) is expected


@pytest.mark.parametrize(('version', 'namespaced', 'expected'), [('v1', True, '/api/v1/namespaces/work/pods'), ('apps/v1', False, '/apis/apps/v1/pods')])
def test_discovery_caches_collections_and_ignores_subresources(version, namespaced, expected):
    """Discovery resolves scopes once per API version and never selects a subresource."""
    api = Mock(request=Mock(return_value={'resources': [{'kind': 'Pod', 'name': 'pods/status', 'namespaced': namespaced}, {'kind': 'Pod', 'name': 'pods', 'namespaced': namespaced}]}))
    manifests = Manifests(api, SetupSettings())
    body = {'apiVersion': version, 'kind': 'Pod', 'metadata': {'name': 'worker', 'namespace': 'work'}}
    assert manifests.path(body) == manifests.path(body) == expected
    api.request.assert_called_once()


@pytest.mark.parametrize(('responses', 'created', 'methods'), [
    ([{'existing': True}], False, ['GET']),
    ([APIError(404, 'missing'), {'created': True}], True, ['GET', 'POST']),
    ([APIError(404, 'missing'), APIError(409, 'race'), {'winner': True}], False, ['GET', 'POST', 'GET']),
])
def test_reconciliation_only_creates_missing_objects(responses, created, methods, monkeypatch):
    """Existing resources are read-only, including resources created by a competing setup."""
    api = Mock(request=Mock(side_effect=responses))
    manifests = Manifests(api, SetupSettings())
    monkeypatch.setattr(manifests, 'path', Mock(return_value='/pods'))
    body = {'metadata': {'name': 'worker'}}
    assert manifests.ensure(body) == (responses[-1], created)
    assert [call.args[0] for call in api.request.call_args_list] == methods
    assert api.request.call_args_list[0].args == ('GET', '/pods/worker')
    if 'POST' in methods:
        assert api.request.call_args_list[1].args == ('POST', '/pods', body)


@pytest.mark.parametrize('responses', [[APIError(403, 'denied')], [APIError(404, 'missing'), APIError(422, 'invalid')]])
def test_read_and_creation_errors_cannot_appear_successful(responses, monkeypatch):
    """Only NotFound and creation races are recoverable installation responses."""
    manifests = Manifests(Mock(request=Mock(side_effect=responses)), SetupSettings())
    monkeypatch.setattr(manifests, 'path', Mock(return_value='/pods'))
    with pytest.raises(APIError) as error:
        manifests.ensure({'metadata': {'name': 'worker'}})
    assert error.value is responses[-1]


@pytest.mark.parametrize(('kind', 'condition'), [('Deployment', 'Available'), ('CustomResourceDefinition', 'Established')])
def test_readiness_requires_the_current_generation(kind, condition, monkeypatch):
    """Readiness waits through absent conditions and, for deployments, stale observations."""
    ready = {'metadata': {'generation': 2}, 'status': {'observedGeneration': 2, 'conditions': [{'type': condition, 'status': 'True'}]}}
    pending = [{'metadata': {}}, {'metadata': {}, 'status': {'conditions': None}}, {'metadata': {'generation': 2}, 'status': {'observedGeneration': 1, 'conditions': [{'type': condition, 'status': 'False'}]}}]
    if kind == 'Deployment':
        pending.append({'metadata': {'generation': 2}, 'status': {'observedGeneration': 1, 'conditions': [{'type': condition, 'status': 'True'}]}})
    api = Mock(request=Mock(side_effect=[*pending, ready]))
    manifests = Manifests(api, SetupSettings(setup_poll_interval=0.25))
    monkeypatch.setattr(manifests, 'path', Mock(return_value='/objects'))
    sleeper = Mock()
    monkeypatch.setattr('throngfission.manifests.sleep', sleeper)
    manifests.wait({'kind': kind, 'metadata': {'name': 'worker'}}, condition)
    assert sleeper.call_count == len(pending)
    sleeper.assert_called_with(0.25)


def test_failed_rollout_reports_the_resource(monkeypatch):
    """A failed deployment reports its identity instead of waiting for the whole deadline."""
    api = Mock(request=Mock(return_value={'metadata': {}, 'status': {'conditions': [{'type': 'Progressing', 'status': 'False', 'reason': 'ProgressDeadlineExceeded'}]}}))
    manifests = Manifests(api, SetupSettings())
    monkeypatch.setattr(manifests, 'path', Mock(return_value='/deployments'))
    with pytest.raises(FissionError, match='/deployments/router'):
        manifests.wait({'kind': 'Deployment', 'metadata': {'name': 'router'}}, 'Available')


def test_readiness_deadline_cancels_further_requests(monkeypatch):
    """A setup deadline prevents another request when readiness never arrives."""
    api = Mock()
    manifests = Manifests(api, SetupSettings())
    monkeypatch.setattr(manifests, 'path', Mock(return_value='/objects'))
    monkeypatch.setattr('throngfission.manifests.TimeoutToken', Mock(return_value=SimpleToken(cancelled=True)))
    with pytest.raises(CancellationError):
        manifests.wait({'metadata': {'name': 'waiting'}}, 'Available')
    api.request.assert_not_called()
