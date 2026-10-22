"""Qualification decisions do not promote unavailable or unqualified methods."""

import pytest

from future_agent.domain.assessment import AssessmentReview, ReviewedEvidence, qualify_methods


@pytest.mark.parametrize(
    "target,current,critical,numeric,expected",
    [
        (True, True, False, True, True),
        (False, True, False, True, False),
        (True, False, False, True, False),
        (True, True, True, True, False),
        (True, True, False, False, False),
    ],
)
def test_method_gates(target, current, critical, numeric, expected):
    result = qualify_methods(
        target_complete=target,
        current_evidence=current,
        critical_unknowns=critical,
        allow_numeric=numeric,
    )
    assert result[0].data_eligible == target
    assert result[1].data_eligible == expected
    assert not any(m.data_eligible for m in result[2:])
    assert all(m.implemented for m in result[:2])
    assert not any(m.implemented for m in result[2:])
    assert all("not_implemented" in m.reasons for m in result[2:])


def test_review_rejects_duplicate_and_excess_questions():
    from uuid import uuid4

    evidence = ReviewedEvidence(span_id=uuid4(), purpose="current_state", relevance_reason="相关")
    with pytest.raises(ValueError):
        AssessmentReview(reviewer="人工", evidence=[evidence, evidence])
    with pytest.raises(ValueError):
        AssessmentReview(reviewer="人工", critical_questions=["问题"] * 4)
