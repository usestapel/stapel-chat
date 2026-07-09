"""stapel-chat contract-emission harness (contract-pipeline.md §2-3).

Emits the module's own contract triad into ``docs/`` from a single-module
``{chat + core}`` Django instance mounted at the canonical ``chat/api/``
prefix:

  docs/schema.json   drf-spectacular OpenAPI, this module only, canonical prefix
  docs/flows.json    generate_flow_docs machine artifact ([] — no @flow_step here)
  docs/errors.json   generate_error_keys registry

Copied from stapel-calendar's adaptation of the stapel-auth reference
implementation; the *mechanism* is stapel_tools.codegen (shared), this file is
the thin per-module *config*.

Usage:
    python -m stapel_chat._codegen --out docs        # `make contract`
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def _configure() -> None:
    """Configure + boot the single-module Django instance for emission."""
    repo_root = os.path.dirname(os.path.abspath(__file__))
    sys.path[:] = [p for p in sys.path if os.path.abspath(p or os.getcwd()) != repo_root]

    from django.conf import settings

    if not settings.configured:
        from stapel_chat._codegen_settings import settings_kwargs

        settings.configure(
            **settings_kwargs(root_urlconf="stapel_chat.codegen_urls", contract=True)
        )

    import django

    django.setup()

    # Pin SCHEMA_PATH_PREFIX to the aggregate-style common prefix so operationIds
    # match the convention every other pair-backend uses (see calendar's etalon
    # note); the path keys keep /chat/api/ on both sides.
    from drf_spectacular.settings import spectacular_settings

    from stapel_chat._codegen_settings import CODEGEN_SCHEMA_PATH_PREFIX

    spectacular_settings.SCHEMA_PATH_PREFIX = CODEGEN_SCHEMA_PATH_PREFIX

    # Register drf-spectacular's JWT cookie-auth extension explicitly — this
    # module mounts alone (no sibling triggers it), and every chat endpoint
    # requires IsAuthenticated, so without it the schema would emit without the
    # `security: [{"JWTCookieAuth": []}]` entry (a real contract gap). Same
    # precedent as stapel-calendar / stapel-profiles.
    from stapel_core.django.openapi.swagger import _register_jwt_auth_extension

    _register_jwt_auth_extension()


def _require_python_312() -> None:
    """Abort emission if not running the pinned 3.12 interpreter.

    drf-spectacular's rendering of component descriptions (``Optional[X]`` vs
    ``X | None``) depends on the Python minor version — contracts emitted on
    anything other than 3.12 produce false diffs against the committed
    docs/*.json.
    """
    if sys.version_info[:2] != (3, 12):
        got = f"{sys.version_info.major}.{sys.version_info.minor}"
        raise SystemExit(
            f"stapel-chat contract emission ABORTED: running Python {got}, but "
            "contracts must be emitted on Python 3.12 (the CI/monolith pin). "
            "drf-spectacular renders component descriptions (Optional[X] vs "
            "X | None) differently across Python minor versions, so emitting on "
            "any other minor produces false diffs against the committed "
            "docs/*.json. Re-run under a 3.12 interpreter."
        )


def main(argv: list[str] | None = None) -> int:
    _require_python_312()

    parser = argparse.ArgumentParser(
        prog="stapel-chat-contract",
        description="Emit this module's contract triad (schema.json + flows.json "
        "+ errors.json) into --out, canonical /chat/api/ prefix.",
    )
    parser.add_argument(
        "--out",
        default="docs",
        help="Output directory for the triad (default: docs).",
    )
    args = parser.parse_args(argv)

    _configure()

    from stapel_tools.codegen import emit_errors, emit_flows, emit_schema

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    paths = emit_schema(out / "schema.json")
    flows = emit_flows(out / "flows.json")
    errors = emit_errors(out / "errors.json")

    print(
        f"stapel-chat contract: {paths} paths, {flows} flows, {errors} error keys "
        f"→ {out}/",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
