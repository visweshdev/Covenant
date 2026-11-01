"""The history replay (covenant bench replay) on a small local repo."""

import json

from covenant.bench import replay

from conftest import git


def test_replay_judges_each_commit_against_rules_compiled_before_them(tmp_path):
    src = tmp_path / "proj"
    (src / "pkg").mkdir(parents=True)
    (src / "tests").mkdir()
    (src / "pyproject.toml").write_text("[project]\nname = 'pkg'\ndependencies = []\n")
    (src / "pkg" / "__init__.py").write_text("def keep(a): ...\ndef gone(): ...\n")
    (src / "tests" / "test_pkg.py").write_text("def test_keep():\n    assert True\n")
    git(src, "init", "-q")
    for msg, change in (("base", None), ("add a feature", ("pkg/__init__.py", "def keep(a, b=1): ...\ndef gone(): ...\n")),
                        ("drop gone()", ("pkg/__init__.py", "def keep(a, b=1): ...\n"))):
        if change:
            (src / change[0]).write_text(change[1])
        git(src, "add", "-A")
        assert git(src, "commit", "-q", "-m", msg).returncode == 0

    row = replay.replay_repo(str(src), tmp_path / "work", commits=2, depth=10)
    assert row["error"] is None
    assert [c["subject"] for c in row["commits"]] == ["add a feature", "drop gone()"]
    assert row["commits"][0]["reports"] == []  # an optional parameter is compatible
    assert [r["checker"] for r in row["commits"][1]["reports"]] == ["api_surface_diff"]

    out = tmp_path / "results.jsonl"
    out.write_text(json.dumps(row) + "\n")
    summary = replay.report(out)
    assert "commits judged: 2" in summary and "flagged: 1" in summary
