from pathlib import Path

import pytest

from covenant.events import Effect
from covenant.ir import Clause
from covenant.monitors.effects import EffectMonitor, compile_glob, static_prefix


def monitor(tmp_path: Path, **params) -> EffectMonitor:
    defaults = {"deny": ["delete", "truncate", "overwrite"], "paths": ["data/**", "~/.mylib/**"]}
    clause = Clause(
        id="C2", kind="EffectProhibition", source_span="don't delete user data",
        checker="effect_monitor", params={**defaults, **params},
    )
    return EffectMonitor(clause, tmp_path)


@pytest.mark.parametrize(
    "pattern, path, expected",
    [
        ("data/**", "data", True),
        ("data/**", "data/users.db", True),
        ("data/**", "data/a/b/c.json", True),
        ("data/**", "database.py", False),
        ("data/*.db", "data/users.db", True),
        ("data/*.db", "data/x/users.db", False),
        ("**/secrets.json", "secrets.json", True),
        ("**/secrets.json", "a/b/secrets.json", True),
    ],
)
def test_globs(pattern, path, expected):
    assert bool(compile_glob(pattern).match(path)) is expected


def test_static_prefix():
    assert static_prefix("data/**") == "data"
    assert static_prefix("~/.mylib/**") == "~/.mylib"
    assert static_prefix("src/*/fixtures") == "src"


def test_delete_in_protected_dir(tmp_path):
    m = monitor(tmp_path)
    assert m.violates(Effect(op="delete", target="data/users.db"))
    assert m.violates(Effect(op="delete", target=str(tmp_path / "data" / "users.db")))
    assert not m.violates(Effect(op="delete", target="build/out.o"))
    assert not m.violates(Effect(op="create", target="data/new.json"))


def test_rename_counts_as_delete(tmp_path):
    assert monitor(tmp_path).violates(Effect(op="rename", target="data/users.db"))


def test_home_paths_and_exclude(tmp_path):
    m = monitor(tmp_path, exclude=["~/.mylib/cache/**"])
    home = Path.home()
    assert m.violates(Effect(op="delete", target=str(home / ".mylib" / "settings.toml")))
    assert not m.violates(Effect(op="delete", target=str(home / ".mylib" / "cache" / "model.bin")))


def test_db_tables(tmp_path):
    m = monitor(tmp_path, db_tables=["users"])
    assert m.violates(Effect(op="drop", target="users", channel="db"))
    assert m.violates(Effect(op="truncate", target="users", channel="db"))
    assert not m.violates(Effect(op="delete", target="sessions", channel="db"))
