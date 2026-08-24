"""Sibling modules this test suite reaches for, and the one rule about them.

Three releases in one night died on the same defect: a test imported a sibling
package that the shared development virtualenv happened to have installed and
that nothing declared, so it passed here and failed on a clean runner —
``ModuleNotFoundError`` at test setup, a red release, a re-release. Its twin is
quieter and worse: a cross-module agreement test wrapped in
``try: import … except ImportError: pytest.skip(...)``, which never fails, and
also never RUNS on CI, so the agreement it claims to assert is asserted
nowhere.

The rule this module encodes:

1. Every sibling the suite touches is **declared** in the ``test`` extra of
   ``pyproject.toml`` (``pip install -e ".[test]"``).
   ``tests/test_test_dependencies.py`` reads both and fails if they disagree,
   so a new import cannot be added without declaring it.
2. Reaching for one goes through :func:`requires`, never a bare import at
   module scope, so a contributor without the extra gets a named skip instead
   of a collection error.
3. CI sets ``STAPEL_TEST_STRICT_SIBLINGS=1``. In strict mode a missing sibling
   **fails** instead of skipping — because on CI the extra is installed, and a
   skip there means the install did not do what the workflow says it did.
"""
from __future__ import annotations

import os
from importlib.util import find_spec

import pytest

#: CI sets this. Strict mode turns "this environment lacks the sibling" from a
#: skip into a failure — see the module docstring, rule 3.
STRICT = os.environ.get("STAPEL_TEST_STRICT_SIBLINGS", "") == "1"


def installed(module: str) -> bool:
    """Is ``module`` importable here? Never raises, never imports it."""
    try:
        return find_spec(module) is not None
    except (ImportError, ValueError):  # pragma: no cover - defensive
        return False


def requires(module: str, dist: str):
    """Decorator: this test needs the sibling ``module`` (package ``dist``).

    Present → the test runs untouched. Absent → a named skip, or in strict
    mode a failure that says which declared package the environment is
    missing.
    """
    if installed(module):
        return lambda func: func

    message = (
        f"{dist} is not installed. It is declared in this package's `test` "
        f"extra — install it with `pip install -e \".[test]\"`."
    )

    if not STRICT:
        return pytest.mark.skip(reason=message)

    def _decorator(func):
        # Deliberately NOT functools.wraps: pytest reads the wrapped
        # signature and would try to build fixtures that themselves import
        # the missing module. A no-argument stub needs nothing.
        def _missing_sibling():
            pytest.fail(
                f"{message} STAPEL_TEST_STRICT_SIBLINGS=1 is set, so this is a "
                f"failure rather than a skip: on CI the extra is installed, "
                f"and a skip here would mean it silently was not."
            )

        _missing_sibling.__name__ = func.__name__
        _missing_sibling.__doc__ = func.__doc__
        return _missing_sibling

    return _decorator


__all__ = ["STRICT", "installed", "requires"]
