"""``manage.py check_rls`` — fail loudly if tenant isolation is not enforced.

The Makefile's ``rls-verify`` target calls this. It runs the deploy-time system
checks that assert the app role cannot bypass RLS, that every tenant table has
FORCE ROW LEVEL SECURITY, and that the ledger guard triggers are installed —
the same ``@register`` checks ``manage.py check --deploy`` runs, exposed as a
single purpose-named command so an operator (or CI) has one obvious thing to run.
"""

from __future__ import annotations

from typing import Any

from django.core.checks import Error, Warning, run_checks
from django.core.management.base import BaseCommand, CommandError

# The checks this command is about, by id prefix.
_RLS_CHECK_PREFIXES = ("tenancy.", "accounting.")


class Command(BaseCommand):
    help = "Assert Row-Level Security and the ledger guards are installed and enforced."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--strict",
            action="store_true",
            help="Treat warnings as failures too.",
        )

    def handle(self, *args: Any, **opts: Any) -> None:
        # deploy=True so the RLS/trigger checks (registered deploy-only) run.
        messages = [
            m for m in run_checks(include_deployment_checks=True)
            if any(str(getattr(m, "id", "")).startswith(p) for p in _RLS_CHECK_PREFIXES)
        ]
        errors = [m for m in messages if isinstance(m, Error)]
        warnings = [m for m in messages if isinstance(m, Warning)]

        for message in messages:
            self.stdout.write(str(message))

        if not messages:
            self.stdout.write(self.style.SUCCESS(
                "RLS verified: app role cannot bypass, every tenant table forces "
                "RLS, ledger triggers installed."
            ))
            return

        if errors or (opts["strict"] and warnings):
            raise CommandError(
                f"RLS verification failed: {len(errors)} error(s), "
                f"{len(warnings)} warning(s)."
            )
        self.stdout.write(self.style.WARNING(
            f"RLS check passed with {len(warnings)} warning(s)."
        ))
