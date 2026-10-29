from covenant.ir import Covenant
from covenant.validate import validate

from conftest import EXAMPLE


def cov(*clauses):
    return Covenant.model_validate({"instruction": "x", "goal": {"action": "a"}, "clauses": list(clauses)})


def errors(issues):
    return [i.message for i in issues if i.level == "error"]


def test_example_has_no_errors():
    issues = validate(Covenant.load(EXAMPLE / "covenant.yaml"), EXAMPLE)
    assert errors(issues) == []


def test_unknown_checker():
    issues = validate(
        cov(
            {"id": "A", "kind": "SemanticInvariant", "source_span": "x", "checker": "magic"},
            {"id": "B", "kind": "SemanticInvariant", "source_span": "x", "checker": "schema_guard"},
        ),
        EXAMPLE,
    )
    msgs = errors(issues)
    assert len(msgs) == 1 and "unknown checker 'magic'" in msgs[0]


def test_kind_mismatch_and_missing_params():
    issues = validate(cov({"id": "A", "kind": "EffectProhibition", "source_span": "x", "checker": "trace_monitor"}), EXAMPLE)
    msgs = errors(issues)
    assert any("enforces TemporalObligation" in m for m in msgs)
    assert any("needs param 'formula'" in m for m in msgs)


def test_bad_formula():
    issues = validate(
        cov({"id": "A", "kind": "TemporalObligation", "source_span": "x", "checker": "trace_monitor",
             "params": {"formula": "commit -> nonsense", "test_cmd": "pytest"}}),
        EXAMPLE,
    )
    assert any("bad formula" in m for m in errors(issues))


def test_vacuous_path_is_a_warning(tmp_path):
    issues = validate(
        cov({"id": "A", "kind": "EffectProhibition", "source_span": "x", "checker": "effect_monitor",
             "params": {"paths": ["data/**"]}}),
        tmp_path,
    )
    assert errors(issues) == []
    assert any("matches nothing" in i.message for i in issues)


def test_missing_package():
    issues = validate(
        cov({"id": "A", "kind": "SemanticInvariant", "source_span": "x", "checker": "api_surface_diff",
             "params": {"package": "nope"}}),
        EXAMPLE,
    )
    assert any("not found" in m for m in errors(issues))
