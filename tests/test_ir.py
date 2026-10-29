import pytest
from pydantic import ValidationError

from covenant.ir import ClauseKind, ClauseStatus, Covenant, OnViolation

from conftest import EXAMPLE


def test_example_loads():
    cov = Covenant.load(EXAMPLE / "covenant.yaml")
    assert [c.id for c in cov.clauses] == ["C1", "C2", "C3"]
    assert cov.clause("C2").kind is ClauseKind.EFFECT_PROHIBITION
    assert cov.clause("C2").on_violation is OnViolation.ROLLBACK
    assert len(cov.enforced(ClauseKind.TEMPORAL_OBLIGATION)) == 1


def test_round_trip(tmp_path):
    cov = Covenant.load(EXAMPLE / "covenant.yaml")
    cov.dump(tmp_path / "out.yaml")
    assert Covenant.load(tmp_path / "out.yaml") == cov


def _base(**clause):
    return {
        "instruction": "x",
        "goal": {"action": "a"},
        "clauses": [{"id": "C1", "kind": "SemanticInvariant", "source_span": "x", **clause}],
    }


def test_enforced_clause_needs_checker():
    with pytest.raises(ValidationError, match="must name a checker"):
        Covenant.model_validate(_base())


def test_unenforceable_clause_is_kept_without_checker():
    cov = Covenant.model_validate(_base(status="unenforceable"))
    assert cov.clauses[0].status is ClauseStatus.UNENFORCEABLE
    assert cov.enforced() == []


def test_duplicate_ids_rejected():
    data = _base(checker="api_surface_diff")
    data["clauses"].append(dict(data["clauses"][0]))
    with pytest.raises(ValidationError, match="duplicate"):
        Covenant.model_validate(data)


def test_unknown_fields_rejected():
    with pytest.raises(ValidationError):
        Covenant.model_validate(_base(checker="api_surface_diff", typo_field=1))
