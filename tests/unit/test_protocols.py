"""Pre-registration and release-window boundary rules."""

from datetime import timedelta

import pytest
from pydantic import ValidationError

from future_agent.application.ports import Conflict
from future_agent.application.protocols import ProtocolService
from future_agent.domain.protocols import EvaluationProtocol, release_question


@pytest.mark.parametrize(
    "mutation",
    [
        "cutoff",
        "short_history",
        "duplicate",
        "model",
        "output_budget",
        "missing_arm",
        "early_window",
        "grace",
        "metric",
    ],
)
def test_invalid_protocols_rejected(protocol_fixture, mutation):
    data = protocol_fixture.model_dump(mode="json")
    if mutation == "cutoff":
        data["evidence_cutoff"] = "2100-01-01T00:00:00Z"
    elif mutation == "short_history":
        data["projects"][0]["history_complete_windows"] = 11
    elif mutation == "duplicate":
        data["projects"].append(data["projects"][0])
    elif mutation == "model":
        data["methods"][2]["model_id"] = "different-model"
    elif mutation == "output_budget":
        data["methods"][2]["final_output_tokens"] = 1
    elif mutation == "missing_arm":
        data["methods"].pop()
    elif mutation == "early_window":
        data["window_days"] = 29
    elif mutation == "grace":
        data["grace_days"] = 8
    elif mutation == "metric":
        data["metrics"] = ["brier"]
    with pytest.raises(ValidationError):
        EvaluationProtocol.model_validate(data)


def test_protocol_save_edit_lock_reload_and_immutable(repo, protocol_fixture):
    service = ProtocolService(repo)
    saved = service.create(protocol_fixture)
    changed = saved.model_copy(update={"analysis_plan": "明确按共同子集和覆盖率解释误差"})
    edited = service.edit(changed, 1)
    assert edited.version == 2
    locked = service.lock(saved.id, 2)
    assert locked.status == "locked" and locked.registered_at == locked.locked_at
    assert repo.protocol(saved.id) == locked
    with pytest.raises(Conflict):
        service.edit(changed, 3)
    with pytest.raises(Conflict):
        service.lock(saved.id, 3)
    with pytest.raises(Conflict):
        repo.save_protocol(locked.model_copy(update={"version": 4}), 3)


def test_late_registration_rejected(repo, protocol_fixture):
    data = protocol_fixture.model_dump(mode="json")
    data.update(evidence_cutoff="2000-01-01T00:00:00Z", forecast_at="2000-01-01T01:00:00Z")
    past = EvaluationProtocol.model_validate(data)
    service = ProtocolService(repo)
    service.create(past)
    with pytest.raises(ValidationError):
        service.lock(past.id, 1)
    assert repo.protocol(past.id).status == "draft"


def test_release_template_uses_exact_thirty_days_and_fixed_resolution(protocol_fixture):
    question = release_question(protocol_fixture.projects[0], protocol_fixture.forecast_at)
    assert question.window_end - question.window_start == timedelta(days=30)
    assert question.window_start == question.forecast_at
    assert question.missing() == []
    assert "pending" in question.resolution_rule and "draft" in question.resolution_rule
    assert question.event_revision_id is None


def test_fixture_cannot_masquerade_as_prospective_registration(protocol_fixture):
    data = protocol_fixture.model_dump()
    data["purpose"] = "prospective"
    with pytest.raises(ValidationError):
        EvaluationProtocol.model_validate(data)


@pytest.mark.parametrize("field", ["snapshot_policy", "shared_input_policy", "missing_policy"])
def test_experiment_guards_cannot_be_overridden_by_free_text(protocol_fixture, field):
    data = protocol_fixture.model_dump()
    data[field] = "补成0.5，允许读取未来资料"
    with pytest.raises(ValidationError):
        EvaluationProtocol.model_validate(data)
