"""
Identity & Access Management: users, memberships, roles, permissions.

Authorisation is two-layered:

**RBAC — "may this actor perform this action at all?"**
    A permission is a string ``<domain>.<resource>.<action>``
    (e.g. ``accounting.journal_entry.post``). Roles bundle permissions.
    A user holds roles *per tenant*, so the same person can be an Accountant
    at one client and a Read-Only Auditor at another.

**ABAC — "on which rows?"**
    Even with ``hr.payslip.read``, an Employee may only read payslips whose
    ``employee_id`` is their own; a Department Manager may only approve leave
    for employees in the department subtree they own. That row-level
    narrowing is expressed as a :class:`ScopeRule` attached to the role
    assignment and compiled into an ORM ``Q`` object by
    ``apps.iam.services.abac.build_scope_q()``.

Splitting the two matters: RBAC alone forces a combinatorial explosion of
roles ("HR manager for Cairo branch", "HR manager for Alex branch"), and
ABAC alone makes it impossible to answer "what can this role do?" in an
audit. Together the matrix stays small and the answer stays computable.
"""

from __future__ import annotations

import uuid

from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models
from django.utils import timezone as dj_timezone

from apps.core.models import TimeStampedModel, UUIDModel

# NOTE: imported as `dj_timezone`, not `timezone`. `User` declares a *field*
# named `timezone`, and inside a class body a name assigned earlier shadows the
# module-level import — so a later `default=timezone.now` resolves to the
# CharField instance and raises AttributeError at import time, before Django can
# even build the app registry. Aliasing removes the trap permanently instead of
# making it depend on the order the fields happen to be declared in.


# ---------------------------------------------------------------------------
# User
# ---------------------------------------------------------------------------

class UserManager(BaseUserManager):
    use_in_migrations = True

    def create_user(self, email, password=None, **extra):
        if not email:
            raise ValueError("Email is required.")
        user = self.model(email=self.normalize_email(email).lower(), **extra)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra):
        extra.setdefault("is_platform_admin", True)
        extra.setdefault("is_staff", True)
        extra.setdefault("is_superuser", True)
        extra.setdefault("is_active", True)
        return self.create_user(email, password, **extra)


class User(UUIDModel, AbstractBaseUser, PermissionsMixin, TimeStampedModel):
    """A human login. Global, not tenant-scoped.

    A single identity can belong to several tenants (an outsourced accountant
    serving five companies). Tenant-specific attributes therefore live on
    :class:`TenantMembership`, never here.
    """

    email = models.EmailField(unique=True)
    full_name = models.CharField(max_length=200)
    phone = models.CharField(max_length=32, blank=True)
    locale = models.CharField(max_length=10, default="en")
    timezone = models.CharField(max_length=64, default="UTC")

    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    #: Operator of the platform itself: may cross tenant boundaries. Guarded by
    #: mandatory MFA, and every action taken is written to TenantAuditLog.
    is_platform_admin = models.BooleanField(default=False)

    mfa_enabled = models.BooleanField(default=False)
    mfa_secret = models.CharField(max_length=128, blank=True)
    last_login_ip = models.GenericIPAddressField(null=True, blank=True)
    password_changed_at = models.DateTimeField(default=dj_timezone.now)
    failed_login_count = models.PositiveSmallIntegerField(default=0)
    locked_until = models.DateTimeField(null=True, blank=True)

    objects = UserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["full_name"]

    class Meta:
        db_table = "iam_user"
        ordering = ["email"]
        indexes = [models.Index(fields=["is_active"], name="ix_user_active")]

    def __str__(self) -> str:  # pragma: no cover
        return self.email

    @property
    def is_locked(self) -> bool:
        return bool(self.locked_until and self.locked_until > dj_timezone.now())


class TenantMembership(UUIDModel, TimeStampedModel):
    """Links a :class:`User` to a tenant. The join row a session is built on.

    Deactivating a membership must revoke access immediately, so the token
    refresh path re-reads this row rather than trusting the JWT claim.
    """

    tenant = models.ForeignKey(
        "tenancy.Tenant", on_delete=models.CASCADE, related_name="memberships"
    )
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="memberships")
    #: Set when the member is also an HR-tracked employee. One-way link:
    #: not every user is an employee (external auditor) and not every
    #: employee has a login (factory floor staff clocked in by a supervisor).
    employee = models.OneToOneField(
        "hr.Employee",
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name="membership",
    )
    is_active = models.BooleanField(default=True)
    is_owner = models.BooleanField(
        default=False, help_text="Billing owner; cannot be removed by others."
    )
    invited_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    invitation_accepted_at = models.DateTimeField(null=True, blank=True)
    last_active_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "iam_tenant_membership"
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "user"], name="uq_membership_tenant_user"
            ),
            # At least one owner must survive; enforced at service level too,
            # but the partial index makes the "exactly one primary owner"
            # invariant checkable.
            models.UniqueConstraint(
                fields=["tenant", "employee"],
                condition=models.Q(employee__isnull=False),
                name="uq_membership_tenant_employee",
            ),
        ]
        indexes = [
            models.Index(fields=["user", "is_active"], name="ix_membership_user_active"),
            models.Index(fields=["tenant", "is_active"], name="ix_membership_tenant_act"),
        ]


# ---------------------------------------------------------------------------
# RBAC
# ---------------------------------------------------------------------------

class Permission(models.Model):
    """A single atomic capability, seeded from ``config/permissions.json``.

    Global (not tenant-scoped) because the catalogue of things the software
    *can* do is a property of the software, not of a customer.

    Codename grammar: ``<domain>.<resource>.<action>``. The grammar is
    load-bearing — the middleware derives the required permission for a
    DRF view from its ``resource`` + HTTP method, so a typo becomes a
    startup-time error rather than a silent authorisation bypass.
    """

    class Domain(models.TextChoices):
        ACCOUNTING = "accounting", "Accounting"
        SALES = "sales", "Sales"
        PURCHASING = "purchasing", "Purchasing"
        INVENTORY = "inventory", "Inventory"
        BANKING = "banking", "Banking"
        PROJECTS = "projects", "Projects"
        HR = "hr", "Human Resources"
        PAYROLL = "payroll", "Payroll"
        REPORTING = "reporting", "Reporting"
        SETTINGS = "settings", "Settings"
        IAM = "iam", "Access control"

    codename = models.CharField(max_length=100, primary_key=True)
    domain = models.CharField(max_length=20, choices=Domain.choices, db_index=True)
    resource = models.CharField(max_length=50)
    action = models.CharField(max_length=30)
    description = models.CharField(max_length=255)
    #: Actions that move money or alter posted books. The UI shows a
    #: confirmation and the API demands a re-authentication for these.
    is_sensitive = models.BooleanField(default=False)

    class Meta:
        db_table = "iam_permission"
        ordering = ["domain", "resource", "action"]
        indexes = [models.Index(fields=["resource", "action"], name="ix_perm_res_action")]

    def __str__(self) -> str:  # pragma: no cover
        return self.codename


class Role(UUIDModel, TimeStampedModel):
    """A named bundle of permissions.

    ``tenant`` is nullable: a NULL tenant means a **system role** shipped with
    the product (Admin, Accountant, HR Manager, Employee, Auditor). Tenants may
    clone a system role into a custom one, but may not edit the originals —
    otherwise a product update that adds a permission would silently grant it
    to a customer-modified role.
    """

    tenant = models.ForeignKey(
        "tenancy.Tenant",
        null=True, blank=True,
        on_delete=models.CASCADE,
        related_name="roles",
    )
    code = models.SlugField(max_length=50)
    name = models.CharField(max_length=100)
    description = models.CharField(max_length=255, blank=True)
    is_system = models.BooleanField(default=False)
    #: Lower number = more authority. Used to stop privilege escalation:
    #: a user may only grant roles with a rank strictly greater than their own.
    rank = models.PositiveSmallIntegerField(default=100)
    permissions = models.ManyToManyField(
        Permission, through="RolePermission", related_name="roles"
    )

    class Meta:
        db_table = "iam_role"
        ordering = ["rank", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "code"], name="uq_role_tenant_code"
            ),
            models.UniqueConstraint(
                fields=["code"],
                condition=models.Q(tenant__isnull=True),
                name="uq_role_system_code",
            ),
            models.CheckConstraint(
                condition=models.Q(is_system=True, tenant__isnull=True)
                | models.Q(is_system=False, tenant__isnull=False),
                name="ck_role_system_has_no_tenant",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return self.name


class RolePermission(models.Model):
    """Explicit through-model so grants are auditable and revocable."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    role = models.ForeignKey(Role, on_delete=models.CASCADE, related_name="+")
    permission = models.ForeignKey(Permission, on_delete=models.CASCADE, related_name="+")
    granted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "iam_role_permission"
        constraints = [
            models.UniqueConstraint(
                fields=["role", "permission"], name="uq_role_permission"
            )
        ]


class RoleAssignment(UUIDModel, TimeStampedModel):
    """Grants a role to a membership, optionally narrowed by ABAC scope.

    Time-bounded on purpose: a temporary auditor gets
    ``valid_until = quarter_end`` and access lapses without anyone having to
    remember to revoke it.
    """

    membership = models.ForeignKey(
        TenantMembership, on_delete=models.CASCADE, related_name="role_assignments"
    )
    role = models.ForeignKey(Role, on_delete=models.PROTECT, related_name="assignments")
    #: Restricts the role to a branch of the org chart. NULL = whole tenant.
    department = models.ForeignKey(
        "hr.Department",
        null=True, blank=True,
        on_delete=models.CASCADE,
        related_name="role_assignments",
    )
    #: Restricts to specific projects (e.g. a project-level billing approver).
    project = models.ForeignKey(
        "projects.Project",
        null=True, blank=True,
        on_delete=models.CASCADE,
        related_name="role_assignments",
    )
    valid_from = models.DateTimeField(default=dj_timezone.now)
    valid_until = models.DateTimeField(null=True, blank=True)
    granted_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        db_table = "iam_role_assignment"
        constraints = [
            models.UniqueConstraint(
                fields=["membership", "role", "department", "project"],
                name="uq_assignment_unique_scope",
            ),
            models.CheckConstraint(
                condition=models.Q(valid_until__isnull=True)
                | models.Q(valid_until__gt=models.F("valid_from")),
                name="ck_assignment_validity_order",
            ),
        ]
        indexes = [
            models.Index(fields=["membership", "valid_until"], name="ix_assign_member"),
        ]

    @property
    def is_currently_valid(self) -> bool:
        now = dj_timezone.now()
        return self.valid_from <= now and (self.valid_until is None or self.valid_until > now)


class ScopeRule(UUIDModel, TimeStampedModel):
    """ABAC predicate attached to a (role, resource) pair.

    ``strategy`` is a small closed vocabulary rather than free-form
    expression text. Allowing arbitrary expressions in an authorisation
    layer means an injection bug is a full data breach; a fixed enum means
    every possible predicate has been reviewed once, in code.
    """

    class Strategy(models.TextChoices):
        ALL = "all", "All rows in tenant"
        OWN_RECORD = "own_record", "Rows whose employee/user is the actor"
        OWN_DEPARTMENT = "own_department", "Actor's department only"
        DEPARTMENT_SUBTREE = "department_subtree", "Actor's department and children"
        ASSIGNED_PROJECTS = "assigned_projects", "Projects the actor is a member of"
        MANAGED_EMPLOYEES = "managed_employees", "Employees reporting to the actor"
        SCOPED_DEPARTMENT = "scoped_department", "The department named on the assignment"
        NONE = "none", "No rows"

    role = models.ForeignKey(Role, on_delete=models.CASCADE, related_name="scope_rules")
    #: Matches ``Permission.resource`` (e.g. "payslip", "leave_request").
    resource = models.CharField(max_length=50, db_index=True)
    strategy = models.CharField(max_length=32, choices=Strategy.choices)
    #: Optional extra conditions, e.g. {"max_amount": "5000.00"} for an
    #: approval limit. Interpreted by the resource's policy class.
    parameters = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "iam_scope_rule"
        constraints = [
            models.UniqueConstraint(
                fields=["role", "resource"], name="uq_scope_rule_role_resource"
            )
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.role.code}:{self.resource}={self.strategy}"


class ApiKey(UUIDModel, TimeStampedModel):
    """Machine credential for server-to-server integrations.

    Only a hash is stored — a leaked database backup must not yield working
    keys. The plaintext is shown exactly once at creation.
    """

    tenant = models.ForeignKey(
        "tenancy.Tenant", on_delete=models.CASCADE, related_name="api_keys"
    )
    name = models.CharField(max_length=100)
    prefix = models.CharField(max_length=12, unique=True, db_index=True)
    key_hash = models.CharField(max_length=128)
    role = models.ForeignKey(Role, on_delete=models.PROTECT, related_name="+")
    created_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="+")
    last_used_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "iam_api_key"
        indexes = [models.Index(fields=["tenant", "revoked_at"], name="ix_apikey_tenant")]


class Invitation(UUIDModel, TimeStampedModel):
    """A pending offer of membership in one tenant, addressed to an email.

    Records the business fact "this organisation asked this address to join,
    with this role, and the offer is still open". It is *not* the membership:
    :class:`TenantMembership` is created immediately (inactive) so the role
    grant is auditable from the moment it is decided, and this row is the
    thing that expires, is resent and is revoked.

    Deliberately **not** a :class:`~apps.core.models.TenantScopedModel`
    despite carrying ``tenant``: the accept path is anonymous — the invitee
    holds a token and no session — so the row must be readable before any
    tenant is bound, exactly like :class:`TenantMembership` at login. The
    table is still covered by the ``tenant_isolation`` RLS policy (see
    ``0003_invitation``) and every authenticated read goes through the
    tenant-filtered queryset in ``apps.iam.viewsets_team``; the one anonymous
    read runs under ``cross_tenant_lookup()`` filtered to a single id.

    Only ``sha256(secret)`` is stored, never the token — same reasoning as
    :class:`ApiKey`. A leaked database backup must not yield a working
    invitation, because accepting one mints a session in someone's books.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        ACCEPTED = "accepted", "Accepted"
        REVOKED = "revoked", "Revoked"
        EXPIRED = "expired", "Expired"

    #: A pending invitation is the only state anything may move out of.
    ALLOWED_TRANSITIONS: dict[str, set[str]] = {
        Status.PENDING: {Status.ACCEPTED, Status.REVOKED, Status.EXPIRED},
        Status.ACCEPTED: set(),
        Status.REVOKED: set(),
        Status.EXPIRED: set(),
    }

    tenant = models.ForeignKey(
        "tenancy.Tenant", on_delete=models.CASCADE, related_name="invitations"
    )
    #: Stored lower-cased. The natural key together with ``tenant``.
    email = models.EmailField()
    role = models.ForeignKey(Role, on_delete=models.PROTECT, related_name="invitations")
    #: Optional ABAC narrowing, mirrored onto the RoleAssignment that is
    #: created with the (inactive) membership.
    department = models.ForeignKey(
        "hr.Department",
        null=True, blank=True,
        on_delete=models.PROTECT,
        related_name="invitations",
    )
    #: SET_NULL, like ``RoleAssignment.granted_by``: who invited whom is
    #: denormalised into ``TenantAuditLog`` and must survive the inviter
    #: leaving, but it must not block their user row from ever being removed.
    invited_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    token_hash = models.CharField(max_length=128)
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.PENDING, db_index=True
    )
    expires_at = models.DateTimeField()
    accepted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "iam_invitation"
        ordering = ["-created_at"]
        constraints = [
            # One open offer per address per organisation. Re-inviting someone
            # must reuse or replace the existing invitation rather than
            # leaving two live tokens, only one of which can be revoked from
            # the UI.
            models.UniqueConstraint(
                fields=["tenant", "email"],
                condition=models.Q(status="pending"),
                name="uq_invitation_one_pending_per_email",
            ),
            # ``accepted_at`` is the timestamp of the accept transition, so it
            # is set exactly when the status says it is.
            models.CheckConstraint(
                condition=models.Q(status="accepted", accepted_at__isnull=False)
                | (~models.Q(status="accepted") & models.Q(accepted_at__isnull=True)),
                name="ck_invitation_accepted_at_matches_status",
            ),
        ]
        indexes = [
            models.Index(fields=["tenant", "-created_at"], name="ix_invite_tenant_time"),
            models.Index(fields=["tenant", "status"], name="ix_invite_tenant_status"),
            models.Index(fields=["email", "status"], name="ix_invite_email_status"),
        ]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.email} -> {self.tenant_id} ({self.status})"

    @property
    def is_open(self) -> bool:
        return (
            self.status == self.Status.PENDING
            and self.expires_at > dj_timezone.now()
        )

    def transition(self, new_status: str, *, when=None) -> None:
        """Move the invitation's status, refusing anything not in the map.

        Assigning ``.status`` from a view is what turns a revoked invitation
        back into a live one; the map makes that impossible to write by
        accident.
        """
        allowed = self.ALLOWED_TRANSITIONS.get(self.status, set())
        if new_status not in allowed:
            from apps.core.exceptions import IllegalTransitionError

            raise IllegalTransitionError(
                f"An invitation cannot go from '{self.status}' to '{new_status}'."
            )
        self.status = new_status
        fields = ["status", "updated_at"]
        if new_status == self.Status.ACCEPTED:
            self.accepted_at = when or dj_timezone.now()
            fields.append("accepted_at")
        self.save(update_fields=fields)
