"""Create the non-superuser PostgreSQL role the application connects as.

Why this command exists
=======================
``apps/tenancy/migrations/0002_row_level_security.py`` installs ``ENABLE`` +
``FORCE ROW LEVEL SECURITY`` and a ``USING``/``WITH CHECK`` policy on every
tenant-scoped table. That machinery is complete and correct, and it is
*entirely inert* against a PostgreSQL superuser: ``rolsuper`` and
``rolbypassrls`` skip policy evaluation before it starts. There is no SQL you
can write in a migration that closes that hole, because the hole is a property
of the connecting role, not of the table.

So a deployment that runs the app as ``postgres`` has every line of the
isolation model in place and no isolation. Worse, it fails silently and in the
safe-looking direction: the ORM's ``TenantManager`` still filters, so the UI
looks right, the tenant tests pass, and the first evidence is one customer
reading another customer's ledger through a code path that used ``.raw()`` or
a Celery task that forgot to bind context.

``RUNNING.md`` §2 documents the fix as a block of SQL to paste into psql. That
is a step people skip. This command is the same SQL, idempotent, runnable, and
verifiable -- and it ends by *proving* the role cannot bypass RLS rather than
asserting that it cannot.

Usage
-----
Run it while ``.env`` still points at a superuser (this connection needs
``CREATEROLE`` and ``GRANT`` rights), then switch ``.env`` to the role it
created::

    python manage.py provision_db_roles
    #   -> then set POSTGRES_APP_USER=erp_app in backend/.env

Ownership
---------
This command does **not** move table ownership. It does not need to: the
migration sets ``FORCE ROW LEVEL SECURITY``, which subjects the owner to the
policy as well, so ownership is no longer a bypass. Migrations therefore keep
running as the existing owner (``postgres`` locally, ``erp_migrator`` under
docker-compose) while the server connects as the restricted role.

CREATEDB
--------
Granted by default because pytest-django creates ``test_erp`` using the
``default`` connection, and the suite must run as the *app* role -- a suite
that runs as a superuser proves nothing about RLS, which is precisely the bug
this command fixes. ``CREATEDB`` is orthogonal to ``rolbypassrls``: it confers
no ability to see another tenant's rows. Pass ``--no-createdb`` for a
production-shaped role.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection

#: Privileges the runtime role needs and no more. Notably absent: any DDL,
#: TRUNCATE (which bypasses row-level DELETE policies), and ownership.
_DML = "SELECT, INSERT, UPDATE, DELETE"


class Command(BaseCommand):
    help = "Create/repair the non-superuser application role so RLS is enforced."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--app-user", default="erp_app")
        parser.add_argument("--app-password", default="erp_app")
        parser.add_argument(
            "--no-createdb",
            action="store_true",
            help="Omit CREATEDB. The role is then production-shaped but cannot "
                 "create the pytest test database.",
        )
        parser.add_argument(
            "--owner",
            default=None,
            help="Role that owns the tables and runs migrations. Defaults to "
                 "the role this command connects as.",
        )

    def handle(self, *args: Any, **opts: Any) -> None:
        if connection.vendor != "postgresql":
            raise CommandError(
                f"RLS roles are a PostgreSQL concept; this connection is "
                f"{connection.vendor!r}."
            )

        app_user: str = opts["app_user"]
        app_password: str = opts["app_password"]
        want_createdb: bool = not opts["no_createdb"]

        with connection.cursor() as cur:
            cur.execute(
                "SELECT current_user, rolsuper FROM pg_roles WHERE rolname = current_user"
            )
            me, me_super = cur.fetchone()
            if not me_super:
                # CREATEROLE alone is enough in practice, but saying so up
                # front beats a permission error four statements in.
                self.stdout.write(self.style.WARNING(
                    f"Connected as {me!r}, which is not a superuser. This will "
                    f"work only if {me!r} holds CREATEROLE."
                ))
            owner: str = opts["owner"] or me

            if app_user == me:
                raise CommandError(
                    f"--app-user is {app_user!r}, the role this command is "
                    f"connected as. The application role must be a *different*, "
                    f"non-superuser role, or nothing changes."
                )

            # -- role ------------------------------------------------------
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", [app_user])
            exists = cur.fetchone() is not None
            verb = "ALTER" if exists else "CREATE"

            # Identifiers cannot be parameterised; quote_ident is PostgreSQL's
            # own escaping, so a hostile --app-user cannot break out.
            cur.execute("SELECT quote_ident(%s), quote_literal(%s)", [app_user, app_password])
            ident, literal = cur.fetchone()

            createdb = "CREATEDB" if want_createdb else "NOCREATEDB"
            cur.execute(
                f"{verb} ROLE {ident} LOGIN PASSWORD {literal} "
                f"NOSUPERUSER NOBYPASSRLS NOCREATEROLE INHERIT {createdb}"
            )
            self.stdout.write(
                f"{'Updated' if exists else 'Created'} role {app_user} "
                f"(NOSUPERUSER NOBYPASSRLS {createdb})"
            )

            # -- grants ----------------------------------------------------
            cur.execute("SELECT quote_ident(%s)", [owner])
            (owner_ident,) = cur.fetchone()
            db_name = settings.DATABASES["default"]["NAME"]
            cur.execute("SELECT quote_ident(%s)", [db_name])
            (db_ident,) = cur.fetchone()

            for stmt in (
                f"GRANT CONNECT ON DATABASE {db_ident} TO {ident}",
                f"GRANT USAGE ON SCHEMA public TO {ident}",
                f"GRANT {_DML} ON ALL TABLES IN SCHEMA public TO {ident}",
                f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {ident}",
                # Without the DEFAULT PRIVILEGES pair, the next migration that
                # adds a table produces a role that can read every existing
                # table and none of the new one -- a failure that shows up as a
                # permission error in one endpoint days after the deploy.
                f"ALTER DEFAULT PRIVILEGES FOR ROLE {owner_ident} IN SCHEMA public "
                f"GRANT {_DML} ON TABLES TO {ident}",
                f"ALTER DEFAULT PRIVILEGES FOR ROLE {owner_ident} IN SCHEMA public "
                f"GRANT USAGE, SELECT ON SEQUENCES TO {ident}",
            ):
                cur.execute(stmt)
            self.stdout.write(
                f"Granted {_DML} on public (+ default privileges from {owner})"
            )

            # -- verify ----------------------------------------------------
            # Assert the property we actually care about, rather than trusting
            # that the CREATE ROLE above said what we meant.
            cur.execute(
                "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = %s",
                [app_user],
            )
            is_super, can_bypass = cur.fetchone()
            if is_super or can_bypass:
                raise CommandError(
                    f"{app_user} still has "
                    f"{'SUPERUSER ' if is_super else ''}"
                    f"{'BYPASSRLS' if can_bypass else ''}"
                    f" -- RLS would remain unenforced. Refusing to report success."
                )

            cur.execute(
                "SELECT count(*) FROM pg_class "
                "WHERE relrowsecurity AND relforcerowsecurity"
            )
            (forced,) = cur.fetchone()

        self.stdout.write(self.style.SUCCESS(
            f"\n{app_user} verified NOSUPERUSER + NOBYPASSRLS. "
            f"{forced} tables have FORCE ROW LEVEL SECURITY."
        ))
        self.stdout.write(
            f"\nNow set in backend/.env:\n"
            f"    POSTGRES_APP_USER={app_user}\n"
            f"    POSTGRES_APP_PASSWORD={app_password}\n"
            f"and keep migrations running as {owner} "
            f"(POSTGRES_APP_USER={owner} python manage.py migrate)."
        )
