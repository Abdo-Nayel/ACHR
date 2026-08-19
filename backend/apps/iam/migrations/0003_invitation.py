"""
Add :class:`apps.iam.models.Invitation` and put it under Row-Level Security.

The RLS half is not optional and not cosmetic. ``iam_invitation`` carries a
``tenant_id``, so it is exactly the shape of table
``0002_row_level_security`` exists to protect: without a policy, one
organisation's administrator could read (or, worse, INSERT) another's pending
invitations through any code path that misses the ORM filter — and an
invitation row names a real person's email address and the role they are being
offered.

The predicate is the *strict* one from ``0002_row_level_security``
(``tenant_id`` is NOT NULL here; an invitation always belongs to exactly one
organisation), repeated in ``WITH CHECK`` so that INSERT and UPDATE are
constrained too. ``USING`` alone would let a caller write a row into another
tenant and never see it again — a silent, unattributable write primitive.

The GRANT block mirrors ``0002_row_level_security``: the runtime role owns no
DDL and is given DML only. It is written defensively (``DO $$`` guarded by a
``pg_roles`` lookup) because the role name differs between docker-compose,
CI and a developer's own database, and a missing role must not fail the
migration.
"""


from __future__ import annotations

import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


TABLE = "iam_invitation"

#: Identical to ``TENANT_PREDICATE`` in ``tenancy.0002_row_level_security``.
#: NULLIF guards the empty string ``bind_database_session`` writes when no
#: tenant is bound: ``''::uuid`` raises rather than returning no rows, which
#: would turn "unauthenticated" into a 500 instead of an empty result.
PREDICATE = (
    "(tenant_id = NULLIF(current_setting('app.current_tenant', true), '')::uuid"
    " OR current_setting('app.rls_bypass', true) = 'on')"
)

FORWARD_SQL = f"""
ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY;
-- FORCE: without it the table owner bypasses the policy and the isolation
-- test suite passes against a system that has none.
ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation ON {TABLE};
CREATE POLICY tenant_isolation ON {TABLE}
    AS PERMISSIVE
    FOR ALL
    TO PUBLIC
    USING {PREDICATE}
    WITH CHECK {PREDICATE};
"""

REVERSE_SQL = f"""
DROP POLICY IF EXISTS tenant_isolation ON {TABLE};
ALTER TABLE {TABLE} NO FORCE ROW LEVEL SECURITY;
ALTER TABLE {TABLE} DISABLE ROW LEVEL SECURITY;
"""

GRANT_SQL = f"""
DO $$
DECLARE
    app_role text := current_setting('app.app_role', true);
BEGIN
    IF app_role IS NOT NULL AND app_role <> ''
       AND EXISTS (SELECT 1 FROM pg_roles WHERE rolname = app_role) THEN
        EXECUTE format(
            'GRANT SELECT, INSERT, UPDATE, DELETE ON {TABLE} TO %I', app_role
        );
    END IF;
END
$$;
"""

REVOKE_SQL = f"""
DO $$
DECLARE
    app_role text := current_setting('app.app_role', true);
BEGIN
    IF app_role IS NOT NULL AND app_role <> ''
       AND EXISTS (SELECT 1 FROM pg_roles WHERE rolname = app_role) THEN
        EXECUTE format(
            'REVOKE SELECT, INSERT, UPDATE, DELETE ON {TABLE} FROM %I', app_role
        );
    END IF;
END
$$;
"""


class Migration(migrations.Migration):

    dependencies = [
        ("hr", "0002_initial"),
        ("iam", "0002_initial"),
        ("tenancy", "0003_rls_nullable_tenant"),
    ]

    operations = [
        migrations.CreateModel(
            name="Invitation",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("email", models.EmailField(max_length=254)),
                ("token_hash", models.CharField(max_length=128)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("accepted", "Accepted"),
                            ("revoked", "Revoked"),
                            ("expired", "Expired"),
                        ],
                        db_index=True,
                        default="pending",
                        max_length=16,
                    ),
                ),
                ("expires_at", models.DateTimeField()),
                ("accepted_at", models.DateTimeField(blank=True, null=True)),
                (
                    "department",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="invitations",
                        to="hr.department",
                    ),
                ),
                (
                    "invited_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "role",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="invitations",
                        to="iam.role",
                    ),
                ),
                (
                    "tenant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="invitations",
                        to="tenancy.tenant",
                    ),
                ),
            ],
            options={
                "db_table": "iam_invitation",
                "ordering": ["-created_at"],
                "indexes": [
                    models.Index(
                        fields=["tenant", "-created_at"], name="ix_invite_tenant_time"
                    ),
                    models.Index(
                        fields=["tenant", "status"], name="ix_invite_tenant_status"
                    ),
                    models.Index(
                        fields=["email", "status"], name="ix_invite_email_status"
                    ),
                ],
                "constraints": [
                    models.UniqueConstraint(
                        condition=models.Q(("status", "pending")),
                        fields=("tenant", "email"),
                        name="uq_invitation_one_pending_per_email",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            models.Q(
                                ("accepted_at__isnull", False), ("status", "accepted")
                            ),
                            models.Q(
                                models.Q(("status", "accepted"), _negated=True),
                                ("accepted_at__isnull", True),
                            ),
                            _connector="OR",
                        ),
                        name="ck_invitation_accepted_at_matches_status",
                    ),
                ],
            },
        ),
        migrations.RunSQL(sql=FORWARD_SQL, reverse_sql=REVERSE_SQL),
        migrations.RunSQL(sql=GRANT_SQL, reverse_sql=REVOKE_SQL),
    ]
