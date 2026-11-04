"""Fixed-prior estimation boundaries; no real-service or accuracy claims."""

import pytest

from future_agent.domain.reference import ReleasePosterior


@pytest.mark.parametrize("successes,expected", [(0, 1 / 14), (8, 9 / 14), (12, 13 / 14)])
def test_predictive_probability_including_extreme_samples(successes, expected):
    posterior = ReleasePosterior.from_successes(successes)
    assert posterior.predictive_probability == expected
    assert 0 < posterior.predictive_probability < 1
    assert posterior.posterior_alpha + posterior.posterior_beta == 14
    assert ReleasePosterior.model_validate_json(posterior.model_dump_json()) == posterior


@pytest.mark.parametrize("value", [None, True, False, "8", 8.5, -1, 13])
def test_missing_or_invalid_count_is_not_a_prior_only_prediction(value):
    with pytest.raises(ValueError):
        ReleasePosterior.from_successes(value)


@pytest.mark.parametrize(
    "field,value",
    [
        ("prior_alpha", 2),
        ("prior_beta", 0.5),
        ("prior_alpha", True),
        ("successes", True),
        ("trials", 0),
        ("trials", 13),
        ("posterior_alpha", 1),
        ("posterior_beta", 1),
        ("predictive_probability", 0.99),
        ("predictive_probability", float("nan")),
        ("predictive_probability", float("inf")),
        ("predictive_probability", True),
    ],
)
def test_tampered_or_nonfinite_parameters_are_rejected(field, value):
    data = ReleasePosterior.from_successes(8).model_dump(mode="json")
    data[field] = value
    with pytest.raises(ValueError):
        ReleasePosterior.model_validate(data)
