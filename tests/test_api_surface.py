import pytest

from covenant.verifiers.api_surface import diff_surfaces, extract_surface, signatures_compatible


def reader(files: dict[str, str]):
    return files.get


BASE = {
    "pkg/__init__.py": "from .core import parse, Doc\nfrom ._impl import helper as _helper\nVERSION = '1'\n",
    "pkg/core.py": (
        "from dataclasses import dataclass\n"
        "@dataclass\nclass Doc:\n    text: str\n    def render(self) -> str: ...\n    def _private(self): ...\n"
        "def parse(text: str, strict: bool = False) -> Doc: ...\n"
    ),
}


def test_extracts_exports_through_reexports():
    surface = extract_surface(reader(BASE), "pkg")
    assert surface["pkg.parse"] == "def parse(text: str, strict: bool=False) -> Doc"
    assert surface["pkg.Doc"] == "@dataclass class Doc()"
    assert surface["pkg.Doc.text"] == "text: str"
    assert "pkg.Doc.render" in surface
    assert "pkg.Doc._private" not in surface
    assert "pkg._helper" not in surface
    assert "pkg.VERSION" in surface


def test_all_limits_exports():
    files = {**BASE, "pkg/__init__.py": BASE["pkg/__init__.py"] + "__all__ = ['parse']\n"}
    assert set(extract_surface(reader(files), "pkg")) == {"pkg.parse"}


def test_signature_change_and_removal():
    after_files = {
        **BASE,
        "pkg/core.py": BASE["pkg/core.py"]
        .replace("strict: bool = False", "*, mode: str")
        .replace("    def render(self) -> str: ...\n", ""),
    }
    diff = diff_surfaces(extract_surface(reader(BASE), "pkg"), extract_surface(reader(after_files), "pkg"))
    assert set(diff.changed) == {"pkg.parse"}
    assert set(diff.removed) == {"pkg.Doc.render"}
    assert diff.added == {}


def test_additions_are_detected():
    after_files = {**BASE, "pkg/core.py": BASE["pkg/core.py"] + "def extra() -> None: ...\n",
                   "pkg/__init__.py": BASE["pkg/__init__.py"] + "from .core import extra\n"}
    diff = diff_surfaces(extract_surface(reader(BASE), "pkg"), extract_surface(reader(after_files), "pkg"))
    assert set(diff.added) == {"pkg.extra"}
    assert not diff.changed and not diff.removed



@pytest.mark.parametrize(
    "before, after, ok",
    [
        ("def parse(text: str, strict: bool=False) -> Doc", "def parse(text: str, strict: bool=False, *, mode: str='lenient') -> Doc", True),
        ("def parse(text: str, strict: bool=False) -> Doc", "def parse(text: str, strict: bool=False, extra: int=0) -> Doc", True),
        ("def f(a, *, b=1)", "def f(a, *, b=1, c=2)", True),
        ("def f(a, **kw)", "def f(a, b=None, **kw)", True),
        ("def parse(text: str, strict: bool=False) -> Doc", "def parse(text: str, *, mode: str='lenient') -> Doc", False),
        ("def f(a, b=1)", "def f(a, b=2)", False),
        ("def f(a, b=1)", "def f(a, c=None, b=1)", False),
        ("def f(a)", "def f(a, b)", False),
        ("def f(a)", "def f(a, *, b)", False),
        ("def f(a, b)", "def f(a, /, b)", False),
        ("def f(a) -> int", "def f(a) -> str", False),
        ("def f(a: int)", "def f(a: str)", False),
        ("def f(*args)", "def f(a=1, *args)", False),
        ("def f(a, **kw)", "def f(a)", False),
        ("@staticmethod def f(a)", "def f(a)", False),
        ("class Doc()", "class Doc(Base)", False),
    ],
)
def test_signature_compatibility(before, after, ok):
    assert signatures_compatible(before, after) is ok
