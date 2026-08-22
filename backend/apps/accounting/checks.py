"""Boot/CI assertion that the ledger's database guards are installed.

The Python posting engine enforces the double-entry invariants as a convention;
the PL/pgSQL triggers enforce them as a guarantee, holding even against a
``.raw()`` write, a Celery task or an analyst in psql. If a migration is missed
or a trigger is dropped, the guarantee is gone and nothing says so until an
unbalanced entry is committed. This check fails the deploy instead.

Registered ``deploy=True`` so it runs on ``manage.py check --deploy`` and in CI,
not during ``migrate`` (when the triggers do not exist yet).
"""

from __future__ import annotations

from django.core.checks import Error, Tags, register
from django.db import OperationalError, ProgrammingError, connection

#: The five guards installed by accounting/migrations/0004_ledger_guards.py.
REQUIRED_LEDGER_TRIGGERS = frozenset({
    "trg_entry_balanced",
    "trg_journal_entry_immutable",
    "trg_journal_entry_no_delete",
    "trg_journal_line_immutable",
    "trg_period_locked",
})


@register(Tags.database, deploy=True)
def check_ledger_triggers_installed(app_configs, **kwargs):
    if connection.vendor != "postgresql":
        return []
    try:
        with connection.cursor() as cur:
            cur.execute(
                "SELECT tgname FROM pg_trigger "
                "WHERE NOT tgisinternal AND tgname = ANY(%s)",
                [sorted(REQUIRED_LEDGER_TRIGGERS)],
            )
            present = {r[0] for r in cur.fetchall()}
    except (OperationalError, ProgrammingError):
        return []
    missing = sorted(REQUIRED_LEDGER_TRIGGERS - present)
    if not missing:
        return []
    return [Error(
        f"{len(missing)} ledger guard trigger(s) are missing: {', '.join(missing)}. "
        "The database is no longer enforcing the double-entry invariants.",
        hint="Re-run migrations; see accounting/migrations/0004_ledger_guards.py.",
        id="accounting.E001",
    )]
