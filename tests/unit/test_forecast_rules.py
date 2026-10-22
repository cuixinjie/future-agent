"""Probability rules preserve absence, reject invalid numbers and record tiny normalization."""

from uuid import uuid4

import pytest
from conftest import forecast_response as response
from conftest import forecast_spec as spec

from future_agent.domain.forecasts import (
    ForecastDraft,
    ForecastSpec,
    validate_judgment,
)


@pytest.mark.parametrize("bad", [-0.001, 1.001, float("nan"), float("inf"), True, "0.5"])
def test_probability_range_and_types(bad):
    with pytest.raises(ValueError):
        ForecastDraft.model_validate(response((bad, 0.5)))


@pytest.mark.parametrize("probabilities", [(0, 1), (1, 0), (0.3, 0.7)])
def test_binary_boundaries(probabilities):
    values, normalized = validate_judgment(
        ForecastDraft.model_validate(response(probabilities)), spec(), "llm_judgment", set()
    )
    assert values == list(probabilities) and normalized is None


def test_small_rounding_is_recorded_without_mutating_original():
    draft = ForecastDraft.model_validate(response((0.4, 0.6000005)))
    values, original = validate_judgment(draft, spec(), "llm_judgment", set())
    assert original == 1.0000005
    assert sum(values) == pytest.approx(1)
    assert draft.outcomes[1].probability == 0.6000005


@pytest.mark.parametrize("values", [(0.4, 0.61), (0, 0), (None, 0.5)])
def test_invalid_distribution_is_not_clipped_or_filled(values):
    with pytest.raises(ValueError):
        validate_judgment(
            ForecastDraft.model_validate(response(values)), spec(), "llm_judgment", set()
        )


def test_independent_probabilities_do_not_get_normalized():
    values, total = validate_judgment(
        ForecastDraft.model_validate(response((0.8, 0.7))),
        spec("independent"),
        "llm_judgment",
        set(),
    )
    assert values == [0.8, 0.7] and total is None


def test_scenario_and_numeric_abstention_are_distinct():
    draft = ForecastDraft.model_validate(response())
    assert validate_judgment(draft, spec(), "scenario_only", set()) == ([None, None], None)
    with pytest.raises(ValueError):
        validate_judgment(draft, spec(), "llm_judgment", set())
    abstain = ForecastDraft.model_validate(response(refusal="关键机制不明确，拒绝给数值"))
    assert validate_judgment(abstain, spec(), "llm_judgment", set()) == ([None, None], None)
    with pytest.raises(ValueError):
        validate_judgment(
            ForecastDraft.model_validate(response((0.5, 0.5))), spec(), "scenario_only", set()
        )


def test_unknown_citation_empty_output_and_changed_outcome_rejected():
    with pytest.raises(ValueError):
        validate_judgment(
            ForecastDraft.model_validate(response(span=uuid4())), spec(), "scenario_only", set()
        )
    with pytest.raises(ValueError):
        ForecastDraft(outcomes=[], assumptions=[])
    changed = response()
    changed["outcomes"][0]["key"] = "invented"
    with pytest.raises(ValueError):
        validate_judgment(ForecastDraft.model_validate(changed), spec(), "scenario_only", set())
    with pytest.raises(ValueError):
        ForecastSpec(outcomes=[spec().outcomes[0]] * 2, definition_review="重复定义")
