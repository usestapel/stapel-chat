"""Per-module contract triad + drift gate (contract-pipeline.md §2-3).

stapel-chat emits its **own** contract triad — ``docs/schema.json``,
``docs/flows.json`` ([] — no ``@flow_step``) and ``docs/errors.json`` — plus
``docs/capabilities.json``, from a single-module ``{chat + core}`` Django
instance mounted at the canonical ``/chat/api/v1/`` prefix.

Chat is not yet mounted in stapel-example-monolith, so there is no aggregate
slice to diff against for byte-identity (contract-pipeline.md §9 fallback):
standalone validation (determinism, self-contained $ref closure, JWT security
on every protected op, canonical prefix) substitutes.

Regenerate after any serializer/view/url/error/axis change:

    make contract        # or: python -m stapel_chat._codegen --out docs

then commit ``docs/{schema,flows,errors,capabilities}.json``.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

_PY = sys.version_info[:2]
if _PY != (3, 12):
    _GOT = f"{_PY[0]}.{_PY[1]}"
    pytest.skip(
        "stapel-chat contract tests require Python 3.12 (the CI/monolith pin) — "
        f"running {_GOT}. drf-spectacular renders component descriptions "
        "differently across Python minor versions, so drift/identity checks "
        "produce false diffs on any other minor.",
        allow_module_level=True,
    )

REPO = Path(__file__).resolve().parent.parent
DOCS = REPO / "docs"
TRIAD = ("schema.json", "flows.json", "errors.json")
ARTIFACTS = TRIAD + ("capabilities.json",)


def _emit(out_dir: Path) -> None:
    for module in ("stapel_chat._codegen", "stapel_chat._capabilities"):
        subprocess.run(
            [sys.executable, "-m", module, "--out", str(out_dir)],
            cwd=str(REPO),
            check=True,
            capture_output=True,
        )


def test_contract_artifacts_committed():
    for name in ARTIFACTS:
        assert (DOCS / name).is_file(), f"missing docs/{name} — run `make contract`"
    assert (DOCS / "capabilities.meta.json").is_file()


def test_contract_has_no_drift(tmp_path):
    _emit(tmp_path)
    for name in ARTIFACTS:
        committed = (DOCS / name).read_bytes()
        regenerated = (tmp_path / name).read_bytes()
        assert committed == regenerated, (
            f"docs/{name} drifted — run `make contract` and commit docs/{name}"
        )


def test_emission_is_deterministic(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    _emit(a)
    _emit(b)
    for name in ARTIFACTS:
        assert (a / name).read_bytes() == (b / name).read_bytes()


def test_paths_carry_canonical_prefix():
    schema = json.loads((DOCS / "schema.json").read_text())
    assert schema["paths"], "schema has no paths"
    assert all(p.startswith("/chat/api/v1/") for p in schema["paths"]), (
        "schema paths are not mounted at the canonical /chat/api/v1/ prefix"
    )


def test_flows_are_empty_no_flow_step_annotations():
    flows = json.loads((DOCS / "flows.json").read_text())
    assert flows == []


def _all_refs(obj) -> set:
    return set(re.findall(r'"#/components/schemas/([^"]+)"', json.dumps(obj)))


def test_schema_refs_are_self_contained():
    schema = json.loads((DOCS / "schema.json").read_text())
    comps = schema.get("components", {}).get("schemas", {})
    seen: set = set()
    stack = list(_all_refs(schema["paths"]))
    while stack:
        name = stack.pop()
        if name in seen:
            continue
        seen.add(name)
        if name in comps:
            stack.extend(_all_refs(comps[name]))
    dangling = seen - set(comps)
    assert not dangling, f"dangling $ref(s): {dangling}"


def test_protected_paths_carry_jwt_security():
    schema = json.loads((DOCS / "schema.json").read_text())
    missing = []
    for path, operations in schema["paths"].items():
        for method, op in operations.items():
            if method not in ("get", "post", "put", "patch", "delete"):
                continue
            security = op.get("security") or []
            if not any("JWTCookieAuth" in entry for entry in security):
                missing.append(f"{method.upper()} {path}")
    assert not missing, f"operations missing JWTCookieAuth security: {missing}"


# --- capabilities.json content sanity (capability-config.md §2) ---------------


def _capabilities() -> dict:
    return json.loads((DOCS / "capabilities.json").read_text())


def test_capabilities_axes():
    axes = {a["key"]: a for a in _capabilities()["axes"]}
    assert set(axes) == {"CHAT_KINDS", "ATTACHMENTS", "MAX_BODY_LENGTH"}
    assert axes["CHAT_KINDS"]["kind"] == "list"
    assert axes["ATTACHMENTS"]["kind"] == "bool"
    # Behavioral, not gating: they change what endpoints accept, not which exist.
    for axis in axes.values():
        assert axis["gates"]["operations"] == []
        assert axis["gates"]["behavior"]
        assert axis["curated"]["business_label"]


def test_capabilities_extension_points_cover_the_seam():
    names = {e["name"] for e in _capabilities()["extension_points"]}
    assert {"SCOPE_PROVIDER", "chat.message", "chat.support.assigned"} <= names


def test_capabilities_operations_total_matches_schema():
    schema = json.loads((DOCS / "schema.json").read_text())
    methods = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
    total = sum(1 for item in schema["paths"].values() for m in item if m in methods)
    assert _capabilities()["operations_total"] == total


def test_capabilities_envelope():
    import tomllib

    doc = _capabilities()
    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text())
    assert doc["module"] == pyproject["project"]["name"]
    assert doc["version"] == pyproject["project"]["version"]
    assert doc["provides"]
    assert doc["extension_points"]
    assert doc["requires"]
