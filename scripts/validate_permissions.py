#!/usr/bin/env python3
"""Validate backend/config/permissions.json. Run in CI; exit non-zero on drift.

Checks, in order of how badly each one bites in production:

1. The file is valid JSON.
2. Every codename in ``roles[].permissions`` exists in ``permissions[]``.
   A dangling codename is a permission that silently does nothing: the role
   looks right in the admin UI and denies in production.
3. Codenames are unique and match the ``<domain>.<resource>.<action>`` grammar
   that ``Permission.codename`` and the view-layer derivation depend on.
4. ``domain`` is one of ``Permission.Domain``'s eleven values (closed choices).
5. ``strategy`` is one of ``ScopeRule.Strategy``'s eight values (closed choices).
6. ``(role, resource)`` scope pairs are unique — mirrors
   ``uq_scope_rule_role_resource``.
7. Role ``code`` and ``rank`` are sane, and rank 0 is held by exactly one role.
8. No role except ``owner`` holds a permission the ``owner`` role lacks.
"""
from __future__ import annotations

import json
import pathlib
import re
import sys

CONFIG = pathlib.Path(__file__).resolve().parent.parent / "backend/config/permissions.json"

# apps.iam.models.Permission.Domain
DOMAINS = {
    "accounting", "sales", "purchasing", "inventory", "banking", "projects",
    "hr", "payroll", "reporting", "settings", "iam",
}
# apps.iam.models.ScopeRule.Strategy
STRATEGIES = {
    "all", "own_record", "own_department", "department_subtree",
    "assigned_projects", "managed_employees", "scoped_department", "none",
}
CODENAME_RE = re.compile(r"^[a-z_]+\.[a-z_]+\.[a-z_]+$")


def main() -> int:
    errors: list[str] = []

    try:
        doc = json.loads(CONFIG.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"FAIL  {CONFIG}: invalid JSON: {exc}")
        return 1

    perms = doc["permissions"]
    roles = doc["roles"]
    known: set[str] = set()

    for p in perms:
        cn = p["codename"]
        if cn in known:
            errors.append(f"duplicate codename {cn!r}")
        known.add(cn)
        if not CODENAME_RE.match(cn):
            errors.append(f"codename {cn!r} does not match <domain>.<resource>.<action>")
        if cn != f"{p['domain']}.{p['resource']}.{p['action']}":
            errors.append(f"codename {cn!r} disagrees with its domain/resource/action fields")
        if p["domain"] not in DOMAINS:
            errors.append(f"{cn}: domain {p['domain']!r} is not in Permission.Domain")
        if not isinstance(p["is_sensitive"], bool):
            errors.append(f"{cn}: is_sensitive must be a bool")
        if not p["description"]:
            errors.append(f"{cn}: empty description")

    owner_perms: set[str] = set()
    ranks: list[int] = []
    for r in roles:
        code = r["code"]
        ranks.append(r["rank"])
        seen_resources: set[str] = set()
        for cn in r["permissions"]:
            if cn not in known:
                errors.append(f"role {code!r} references unknown permission {cn!r}")
        if code == "owner":
            owner_perms = set(r["permissions"])
        for sc in r["scopes"]:
            if sc["strategy"] not in STRATEGIES:
                errors.append(
                    f"role {code!r} resource {sc['resource']!r}: "
                    f"strategy {sc['strategy']!r} is not in ScopeRule.Strategy"
                )
            if sc["resource"] in seen_resources:
                errors.append(
                    f"role {code!r} has two scope rules for resource {sc['resource']!r} "
                    f"(violates uq_scope_rule_role_resource)"
                )
            seen_resources.add(sc["resource"])
            if not isinstance(sc["parameters"], dict):
                errors.append(f"role {code!r} resource {sc['resource']!r}: parameters must be an object")

    if ranks.count(0) != 1:
        errors.append(f"expected exactly one rank-0 role, found {ranks.count(0)}")

    for r in roles:
        if r["code"] == "owner":
            continue
        extra = set(r["permissions"]) - owner_perms
        if extra:
            errors.append(f"role {r['code']!r} holds permissions the owner lacks: {sorted(extra)}")

    if errors:
        for e in errors:
            print(f"FAIL  {e}")
        return 1

    print(f"OK    {CONFIG}")
    print(f"OK    version={doc['version']}  permissions={len(perms)}  roles={len(roles)}")
    print(f"OK    all {sum(len(r['permissions']) for r in roles)} role->permission "
          f"references resolve to a defined codename")
    print(f"OK    all {sum(len(r['scopes']) for r in roles)} scope rules use a valid "
          f"ScopeRule.Strategy value")
    print(f"OK    {sum(1 for p in perms if p['is_sensitive'])} permissions marked is_sensitive")
    return 0


if __name__ == "__main__":
    sys.exit(main())
