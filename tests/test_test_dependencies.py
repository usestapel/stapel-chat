"""The gate that stops a green suite here from being a red one on a runner.

This is the fix for a defect class, not for one import. Three releases in the
same night — stapel-core 0.44.0, this package's own 0.5.0, a stapel-tools
nav-manifest test — shipped red on CI with ``ModuleNotFoundError`` at test
setup, because a test imported a sibling package that the shared development
virtualenv happened to have installed and that no file declared. The suite is
honest here and dishonest everywhere else, and nothing catches that: pytest is
perfectly happy, and so is every reviewer.

So the declaration is made checkable. This test parses every file in the suite,
collects the ``stapel_*`` packages it imports (at any depth — inside functions,
inside ``try`` blocks, inside fixtures, which is exactly where the ones that
bit us were hiding), and asserts each one is declared either as a runtime
dependency or in the ``test`` extra of ``pyproject.toml``. Add an import
without declaring it and this fails locally, before the tag exists.
"""
from __future__ import annotations

import ast
import pathlib
import re
import tomllib

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_PYPROJECT = _ROOT / "pyproject.toml"

#: This package itself, and the distribution name it is installed under.
_SELF_MODULE = "stapel_chat"

#: The suite is these files. conftest.py sits outside tests/ and is where the
#: 0.5.0 breakage was half-fixed, so it is very much in scope.
def _suite_files() -> list[pathlib.Path]:
    files = sorted((_ROOT / "tests").glob("*.py"))
    files.append(_ROOT / "conftest.py")
    return [f for f in files if f.exists()]


def _dist_name(module: str) -> str:
    """``stapel_moderation`` -> ``stapel-moderation``."""
    return module.replace("_", "-")


def _imported_stapel_modules() -> dict[str, set[str]]:
    """``{top-level stapel module: {files that import it}}``, at any depth."""
    found: dict[str, set[str]] = {}
    for path in _suite_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            for name in names:
                top = name.split(".")[0]
                if top.startswith("stapel_") and top != _SELF_MODULE:
                    found.setdefault(top, set()).add(path.name)
    return found


def _declared() -> tuple[set[str], set[str]]:
    """``(runtime distributions, test-extra distributions)``, names only."""
    data = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    project = data["project"]

    def _names(specs) -> set[str]:
        out = set()
        for spec in specs or []:
            name = re.split(r"[\[<>=!~;\s]", spec, maxsplit=1)[0].strip()
            if name:
                out.add(name)
        return out

    runtime = _names(project.get("dependencies"))
    extras = project.get("optional-dependencies") or {}
    return runtime, _names(extras.get("test"))


def test_every_sibling_the_suite_imports_is_declared():
    """The gate. A test may import any sibling it likes — and must say so.

    "It works in my venv" is not a dependency declaration, and the shared
    development virtualenv of this fleet has every module installed, so it
    can never be the thing that tells us. ``pyproject.toml`` is.
    """
    runtime, test_extra = _declared()
    declared = runtime | test_extra

    undeclared = {
        module: sorted(files)
        for module, files in _imported_stapel_modules().items()
        if _dist_name(module) not in declared
    }

    assert not undeclared, (
        "These sibling packages are imported by the test suite and declared "
        "nowhere, so CI installs a runner without them and the suite errors "
        "at setup: "
        + "; ".join(
            f"{_dist_name(m)} (imported by {', '.join(f)})"
            for m, f in sorted(undeclared.items())
        )
        + ". Add each to [project.optional-dependencies].test in "
        "pyproject.toml — or stop importing it."
    )


def test_the_test_extra_declares_nothing_the_suite_does_not_use():
    """The other direction, so the extra cannot rot into a wish list.

    Only siblings are checked: pytest, channels and friends are the harness,
    not modules under contract with this one.
    """
    _, test_extra = _declared()
    imported = {_dist_name(m) for m in _imported_stapel_modules()}
    runtime, _ = _declared()

    stale = {
        dist
        for dist in test_extra
        if dist.startswith("stapel-") and dist not in imported and dist not in runtime
    }

    assert not stale, (
        "The `test` extra declares sibling packages no test imports: "
        f"{sorted(stale)}. Remove them, or the extra stops describing anything."
    )


@pytest.mark.parametrize("module", sorted(_imported_stapel_modules()))
def test_a_declared_sibling_is_actually_importable_here(module):
    """Locally this is a reminder; on CI (``STAPEL_TEST_STRICT_SIBLINGS=1``)
    it is the assertion that the workflow installed what it claims to.

    Without it, the ``test`` extra could go missing from the CI step and the
    only symptom would be a handful of quiet skips in a green run — which is
    the second face of this same defect class.
    """
    from .siblings import STRICT, installed

    if installed(module):
        return
    if STRICT:
        pytest.fail(
            f"{_dist_name(module)} is declared and not installed, with "
            "STAPEL_TEST_STRICT_SIBLINGS=1 set. The CI step that installs "
            "the `test` extra did not do what the workflow says it does."
        )
    pytest.skip(f"{_dist_name(module)} not installed: `pip install -e '.[test]'`")
