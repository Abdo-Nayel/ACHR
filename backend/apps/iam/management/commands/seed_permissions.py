"""Load ``config/permissions.json`` into the RBAC/ABAC tables.

    python manage.py seed_permissions [--file PATH] [--prune] [--dry-run]

The JSON file is the *source of truth* for the permission catalogue and for
the system roles; these tables are its materialisation. That direction matters:
the catalogue of things the software can do is a property of the software, so
it is reviewed in a pull request and applied by a deploy, never edited in a
production admin screen. ``scripts/validate_permissions.py`` runs the same
checks in CI before the file is ever merged.

What it writes
--------------
``iam.Permission``      one row per ``permissions[]`` entry (PK is the codename)
``iam.Role``            one row per ``roles[]`` entry, with ``tenant=NULL``
                        and ``is_system=True`` — the ``ck_role_system_has_no_tenant``
                        constraint requires exactly that pairing
``iam.RolePermission``  the grant edges
``iam.ScopeRule``       one row per ``roles[].scopes[]`` entry

Fail loud, not closed
---------------------
A role that references a codename absent from ``permissions[]`` is *not*
skipped with a warning. It is a role that looks correct in the admin UI and
silently denies in production — the single most confusing authorisation bug
there is, because the grant is visibly present and demonstrably ineffective.
So the command validates the whole document first and raises
``CommandError`` before writing anything.

Idempotency
-----------
Every write is ``update_or_create`` keyed on the natural key its unique
constraint uses (``codename``; ``(tenant=NULL, code)``; ``(role, permission)``;
``(role, resource)``). Re-running after adding a permission to the file grants
it; re-running after *removing* one revokes it only with ``--prune``, because
deleting permission rows cascades to every tenant's role assignments and that
should be a deliberate act.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.iam.models import Permission, Role, RolePermission, ScopeRule

#: Closed vocabularies, mirrored from the models so a typo in the JSON is a
#: startup-time error rather than a row the ORM will happily store and the
#: policy compiler will later fail to interpret.
VALID_DOMAINS: frozenset[str] = frozenset(Permission.Domain.values)
VALID_STRATEGIES: frozenset[str] = frozenset(ScopeRule.Strategy.values)


def default_permissions_path() -> pathlib.Path:
    """``backend/config/permissions.json``, located from the settings module."""
    backend_dir = getattr(settings, "BACKEND_DIR", None)
    if backend_dir is not None:
        return pathlib.Path(backend_dir) / "config" / "permissions.json"
    # settings without BACKEND_DIR (a bare test settings module): fall back to
    # this file's position — apps/iam/management/commands -> backend/.
    return pathlib.Path(__file__).resolve().parents[4] / "config" / "permissions.json"


class Command(BaseCommand):
    help = (
        "Seed iam.Permission / Role / RolePermission / ScopeRule from "
        "config/permissions.json. Idempotent; fails loudly on a dangling "
        "codename."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--file",
            default=None,
            help="Path to permissions.json. Defaults to backend/config/permissions.json.",
        )
        parser.add_argument(
            "--prune",
            action="store_true",
            help="Delete system roles, grants and scope rules that the file no "
                 "longer declares. Off by default: pruning a permission "
                 "cascades to every tenant's role assignments.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Validate and report, then roll back without writing.",
        )

    # -- entry point ------------------------------------------------------

    def handle(self, *args, **options) -> None:
        path = pathlib.Path(options["file"]) if options["file"] else default_permissions_path()
        document = self._load(path)
        self._validate(document, path)

        summary: dict[str, int] = {}
        try:
            with transaction.atomic():
                summary = self._apply(document, prune=options["prune"])
                if options["dry_run"]:
                    raise _DryRun()
        except _DryRun:
            self.stdout.write(self.style.WARNING("DRY RUN — everything rolled back."))

        if int(options.get("verbosity", 1)) == 0:
            return
        self._report(path, document, summary, dry_run=options["dry_run"])

    # -- load / validate --------------------------------------------------

    def _load(self, path: pathlib.Path) -> dict[str, Any]:
        if not path.exists():
            raise CommandError(f"Permission catalogue not found at {path}.")
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise CommandError(f"{path} is not valid JSON: {exc}") from exc

    def _validate(self, document: dict[str, Any], path: pathlib.Path) -> None:
        """Every check that can be made before a single row is written.

        Mirrors ``scripts/validate_permissions.py`` so that a file which
        bypassed CI still cannot half-load: the failure modes here are
        privilege escalation and silent denial, and both are far cheaper to
        catch at seed time than in an incident.
        """
        errors: list[str] = []

        for name in ("permissions", "roles"):
            if not isinstance(document.get(name), list):
                raise CommandError(f"{path}: '{name}' must be a list.")

        known: set[str] = set()
        for entry in document["permissions"]:
            codename = entry.get("codename", "")
            if codename in known:
                errors.append(f"duplicate codename {codename!r}")
            known.add(codename)
            expected = f"{entry.get('domain')}.{entry.get('resource')}.{entry.get('action')}"
            if codename != expected:
                errors.append(
                    f"codename {codename!r} disagrees with its "
                    f"domain/resource/action ({expected!r}); the middleware "
                    f"derives one from the other, so they cannot differ"
                )
            if entry.get("domain") not in VALID_DOMAINS:
                errors.append(
                    f"{codename}: domain {entry.get('domain')!r} is not in "
                    f"Permission.Domain"
                )

        for role in document["roles"]:
            code = role.get("code", "")
            for codename in role.get("permissions", []):
                if codename not in known:
                    # THE check this command exists for. See the module docstring.
                    errors.append(
                        f"role {code!r} grants {codename!r}, which is not "
                        f"declared in permissions[] — the grant would be a no-op"
                    )
            seen_resources: set[str] = set()
            for scope in role.get("scopes", []):
                resource = scope.get("resource", "")
                if scope.get("strategy") not in VALID_STRATEGIES:
                    errors.append(
                        f"role {code!r} resource {resource!r}: strategy "
                        f"{scope.get('strategy')!r} is not in ScopeRule.Strategy"
                    )
                if resource in seen_resources:
                    errors.append(
                        f"role {code!r} has two scope rules for {resource!r} "
                        f"(violates uq_scope_rule_role_resource)"
                    )
                seen_resources.add(resource)

        if errors:
            joined = "\n  - ".join(errors)
            raise CommandError(
                f"{path} is inconsistent; refusing to seed:\n  - {joined}"
            )

    # -- apply ------------------------------------------------------------

    def _apply(self, document: dict[str, Any], *, prune: bool) -> dict[str, int]:
        counts = {
            "permissions_created": 0,
            "permissions_updated": 0,
            "roles_created": 0,
            "roles_updated": 0,
            "grants_created": 0,
            "grants_removed": 0,
            "scopes_created": 0,
            "scopes_updated": 0,
            "scopes_removed": 0,
            "permissions_pruned": 0,
            "roles_pruned": 0,
        }

        declared_codenames: set[str] = set()
        for entry in document["permissions"]:
            declared_codenames.add(entry["codename"])
            _, created = Permission.objects.update_or_create(
                codename=entry["codename"],
                defaults={
                    "domain": entry["domain"],
                    "resource": entry["resource"],
                    "action": entry["action"],
                    "description": entry.get("description", ""),
                    "is_sensitive": bool(entry.get("is_sensitive", False)),
                },
            )
            counts["permissions_created" if created else "permissions_updated"] += 1

        declared_role_codes: set[str] = set()
        for entry in document["roles"]:
            declared_role_codes.add(entry["code"])
            role, created = Role.objects.update_or_create(
                tenant=None,
                code=entry["code"],
                defaults={
                    "name": entry["name"],
                    "description": entry.get("description", "")[:255],
                    "rank": entry.get("rank", 100),
                    # A system role must have no tenant; the pairing is a
                    # CHECK constraint, not a convention.
                    "is_system": True,
                },
            )
            counts["roles_created" if created else "roles_updated"] += 1

            wanted = set(entry.get("permissions", []))
            existing = set(
                RolePermission.objects.filter(role=role).values_list(
                    "permission_id", flat=True
                )
            )
            for codename in sorted(wanted - existing):
                RolePermission.objects.create(role=role, permission_id=codename)
                counts["grants_created"] += 1
            stale = existing - wanted
            if stale:
                # Revoking a grant the file no longer declares is always safe:
                # the role simply loses a capability the product no longer
                # says it has. This is not the destructive case ``--prune``
                # guards; that is deleting the Permission row itself.
                RolePermission.objects.filter(
                    role=role, permission_id__in=stale
                ).delete()
                counts["grants_removed"] += len(stale)

            wanted_resources: set[str] = set()
            for scope in entry.get("scopes", []):
                wanted_resources.add(scope["resource"])
                _, scope_created = ScopeRule.objects.update_or_create(
                    role=role,
                    resource=scope["resource"],
                    defaults={
                        "strategy": scope["strategy"],
                        "parameters": scope.get("parameters", {}),
                    },
                )
                counts["scopes_created" if scope_created else "scopes_updated"] += 1
            removed, _ = (
                ScopeRule.objects.filter(role=role)
                .exclude(resource__in=wanted_resources)
                .delete()
            )
            counts["scopes_removed"] += removed

        if prune:
            pruned_roles, _ = (
                Role.objects.filter(tenant__isnull=True, is_system=True)
                .exclude(code__in=declared_role_codes)
                .delete()
            )
            counts["roles_pruned"] += pruned_roles
            pruned_perms, _ = (
                Permission.objects.exclude(codename__in=declared_codenames).delete()
            )
            counts["permissions_pruned"] += pruned_perms

        return counts

    # -- output -----------------------------------------------------------

    def _report(
        self,
        path: pathlib.Path,
        document: dict[str, Any],
        counts: dict[str, int],
        *,
        dry_run: bool,
    ) -> None:
        write = self.stdout.write
        write(self.style.MIGRATE_HEADING(
            f"Permission catalogue {path} (version {document.get('version', '?')})"
        ))
        write(f"  permissions  {len(document['permissions']):>4} declared  "
              f"(+{counts.get('permissions_created', 0)} new, "
              f"{counts.get('permissions_updated', 0)} refreshed)")
        write(f"  roles        {len(document['roles']):>4} declared  "
              f"(+{counts.get('roles_created', 0)} new, "
              f"{counts.get('roles_updated', 0)} refreshed)")
        write(f"  grants       +{counts.get('grants_created', 0)} granted, "
              f"-{counts.get('grants_removed', 0)} revoked")
        write(f"  scope rules  +{counts.get('scopes_created', 0)} new, "
              f"{counts.get('scopes_updated', 0)} refreshed, "
              f"-{counts.get('scopes_removed', 0)} removed")
        if counts.get("permissions_pruned") or counts.get("roles_pruned"):
            write(self.style.WARNING(
                f"  pruned       {counts['permissions_pruned']} permission(s), "
                f"{counts['roles_pruned']} role(s)"
            ))
        for role in document["roles"]:
            write(f"    {role['code']:<20} rank {role.get('rank', 100):<4} "
                  f"{len(role.get('permissions', [])):>3} permissions, "
                  f"{len(role.get('scopes', [])):>2} scope rules")
        if not dry_run:
            write(self.style.SUCCESS("  seeded."))


class _DryRun(Exception):
    """Internal control-flow signal used to roll back a --dry-run."""
