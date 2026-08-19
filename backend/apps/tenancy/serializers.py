"""
Tenancy serializers: the organisation, its domains, its plan, its audit trail.

``Tenant`` is not a tenant-scoped row — it *is* the scope. Nothing in this
module may therefore rely on ``TenantManager`` to hide anything: every
queryset in ``apps.tenancy.viewsets`` filters explicitly, and every serializer
here treats identity fields (``slug``, ``status``, ``base_currency``) as
server-owned.
"""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from apps.core.serializers import MoneyField, ReadOnlyModelSerializer
from apps.tenancy.models import Subscription, Tenant, TenantAuditLog, TenantDomain

#: Settings keys a tenant administrator may write through the API. Anything
#: else in ``Tenant.settings`` is platform-owned (billing counters, migration
#: flags, feature entitlements bought through the sales process) and a customer
#: flipping one of those is a licensing bypass, not a preference change.
WRITABLE_SETTING_KEYS: frozenset[str] = frozenset(
    {
        "branding",            # logo url, primary colour, invoice footer
        "locale",              # default language, date format, number format
        "invoice",             # default payment terms, due-date offset, notes
        "notifications",       # digest frequency, recipients
        "week_start",
        "working_days",
        "decimal_display_places",
    }
)


class TenantSerializer(serializers.ModelSerializer):
    """The customer organisation.

    Almost everything is read-only. ``slug`` is a routing identifier baked into
    sub-domains, bookmarked URLs and stored OAuth redirect URIs; ``status`` is
    driven by billing, not by the customer; ``base_currency`` is immutable once
    a journal entry has been posted because changing it would silently
    reinterpret every historical amount.

    ``settings`` is writable but filtered: see :data:`WRITABLE_SETTING_KEYS`.
    Only an Owner (or a member holding ``settings.organisation.update``) gets
    that far — the check lives in :meth:`validate` rather than in the viewset so
    it cannot be skipped by another caller reusing this serializer.
    """

    feature_flags = serializers.SerializerMethodField()
    seats_in_use = serializers.SerializerMethodField()

    class Meta:
        model = Tenant
        fields = (
            "id", "name", "legal_name", "slug", "status", "country", "timezone",
            "base_currency", "tax_registration_number", "fiscal_year_start_month",
            "settings", "feature_flags", "seats_in_use", "trial_ends_at",
            "suspended_at", "created_at", "updated_at",
        )
        read_only_fields = (
            "id", "slug", "status", "base_currency", "trial_ends_at",
            "suspended_at", "created_at", "updated_at",
        )

    def get_feature_flags(self, obj: Tenant) -> dict[str, Any]:
        """Entitlements, surfaced as a flat map for the client's feature gates.

        Read from ``settings["features"]`` — which is *not* in
        ``WRITABLE_SETTING_KEYS``: a customer who can write their own feature
        flags has bought the Enterprise plan with a PATCH.
        """
        return dict((obj.settings or {}).get("features") or {})

    def get_seats_in_use(self, obj: Tenant) -> int:
        from apps.iam.models import TenantMembership

        return TenantMembership.objects.filter(tenant_id=obj.id, is_active=True).count()

    def validate(self, attrs: dict) -> dict:
        request = self.context.get("request")
        if not attrs:
            return attrs
        self._assert_may_write(request)
        return attrs

    def validate_settings(self, value: dict) -> dict:
        if not isinstance(value, dict):
            raise serializers.ValidationError("Settings must be an object.")
        rejected = sorted(set(value) - WRITABLE_SETTING_KEYS)
        if rejected:
            raise serializers.ValidationError(
                f"These settings are managed by the platform and cannot be "
                f"changed here: {rejected}."
            )
        # Merge rather than replace: a client that PATCHes {"branding": {...}}
        # must not silently wipe the notification preferences it did not send.
        merged = dict(getattr(self.instance, "settings", None) or {})
        merged.update(value)
        return merged

    def validate_fiscal_year_start_month(self, value: int) -> int:
        if self.instance is not None and value != self.instance.fiscal_year_start_month:
            from apps.accounting.models import FiscalYear

            if FiscalYear.all_tenants.filter(tenant_id=self.instance.id).exists():
                raise serializers.ValidationError(
                    "The fiscal year cannot be moved once a fiscal year exists: "
                    "every period boundary, comparative report and closing "
                    "entry is anchored to it."
                )
        return value

    @staticmethod
    def _assert_may_write(request) -> None:
        """Owner, or ``settings.organisation.update``. Nobody else.

        The membership check is not redundant with the permission check: the
        Owner is the account that must always be able to fix a
        misconfiguration, including one that revoked their own role.
        """
        membership = getattr(request, "membership", None)
        if membership is not None and membership.is_owner:
            return

        from apps.iam.services.permissions import has_permission

        user = getattr(request, "user", None)
        if not has_permission(user, "settings.organisation.update"):
            raise serializers.ValidationError(
                "Only an organisation owner or a member holding "
                "'settings.organisation.update' may change these settings."
            )


class TenantDomainSerializer(serializers.ModelSerializer):
    """A custom domain pointing at this tenant.

    ``verified_at`` is server-owned. It is what the host-header resolver checks
    before it will map a request to this tenant, so a client that could write
    it could claim any hostname — including one belonging to another customer —
    and receive their sub-domain traffic.
    """

    class Meta:
        model = TenantDomain
        fields = ("id", "tenant", "domain", "is_primary", "verified_at",
                  "created_at", "updated_at")
        read_only_fields = ("id", "tenant", "verified_at", "created_at", "updated_at")

    def validate_domain(self, value: str) -> str:
        value = (value or "").strip().lower().rstrip(".")
        if not value or " " in value or "/" in value:
            raise serializers.ValidationError("That is not a valid hostname.")
        return value

    def create(self, validated_data: dict) -> TenantDomain:
        request = self.context.get("request")
        validated_data["tenant_id"] = getattr(request, "tenant_id", None)
        return super().create(validated_data)


class SubscriptionSerializer(serializers.ModelSerializer):
    """The billing plan. Append-only history, so plan changes stay reportable.

    ``monthly_amount`` uses the core :class:`~apps.core.serializers.MoneyField`
    like every other amount in the API: it is rendered as a JSON string so a
    JavaScript client cannot turn 1234.56 into 1234.5599999999999 on its way
    into an invoice total.
    """

    monthly_amount = MoneyField()

    class Meta:
        model = Subscription
        fields = ("id", "tenant", "plan", "seats", "monthly_amount", "currency",
                  "started_on", "ended_on", "created_at", "updated_at")
        read_only_fields = ("id", "tenant", "created_at", "updated_at")

    def validate(self, attrs: dict) -> dict:
        started = attrs.get("started_on") or getattr(self.instance, "started_on", None)
        ended = attrs.get("ended_on")
        if started and ended and ended < started:
            raise serializers.ValidationError(
                {"ended_on": "A subscription cannot end before it starts."}
            )
        return attrs


class TenantAuditLogSerializer(ReadOnlyModelSerializer):
    """Append-only security record. Read-only by construction, not by policy.

    An audit log a tenant administrator can edit is not an audit log. The
    write path is the services that record the events; there is no API that
    creates, changes or deletes a row here.
    """

    action_display = serializers.CharField(source="get_action_display", read_only=True)

    class Meta:
        model = TenantAuditLog
        fields = (
            "id", "tenant", "actor_id", "actor_email", "action", "action_display",
            "object_type", "object_id", "payload", "ip_address", "user_agent",
            "occurred_at",
        )


class TenantSettingsSerializer(serializers.Serializer):
    """Payload of ``GET /tenancy/current/``.

    One call that answers "who am I working for, what is switched on, and what
    am I allowed to do here" — the three questions the client asks before it can
    render its first screen.
    """

    def to_representation(self, tenant: Tenant) -> dict[str, Any]:
        request = self.context.get("request")
        settings_blob = dict(tenant.settings or {})
        subscription = (
            Subscription.objects.filter(tenant_id=tenant.id, ended_on__isnull=True)
            .order_by("-started_on")
            .first()
        )
        membership = getattr(request, "membership", None)

        return {
            "tenant": TenantSerializer(tenant, context=self.context).data,
            "settings": {k: v for k, v in settings_blob.items() if k in WRITABLE_SETTING_KEYS},
            "feature_flags": dict(settings_blob.get("features") or {}),
            "subscription": (
                SubscriptionSerializer(subscription, context=self.context).data
                if subscription is not None else None
            ),
            "domains": TenantDomainSerializer(
                tenant.domains.all(), many=True, context=self.context
            ).data,
            "membership": {
                "id": str(membership.id),
                "is_owner": membership.is_owner,
                "employee_id": str(membership.employee_id) if membership.employee_id else None,
            } if membership is not None else None,
            # Read-only tenants (past due, suspended) must still be able to
            # export their books; the client greys out write actions from this.
            "is_operational": tenant.is_operational,
        }


class TenantSettingsUpdateSerializer(TenantSerializer):
    """Write half of ``/api/v1/tenancy/current/`` (``PATCH``).

    A subclass rather than a second, parallel serializer: everything that
    makes :class:`TenantSerializer` safe — the ``WRITABLE_SETTING_KEYS``
    allow-list, the merge-don't-replace behaviour of ``settings``, the
    fiscal-year guard, and ``_assert_may_write`` — applies here unchanged and
    must not be re-implemented, because a re-implementation is a place for one
    of those four to be forgotten.

    Exactly one field is relaxed: ``base_currency`` becomes writable, and is
    then guarded by
    :func:`apps.tenancy.services.settings.assert_base_currency_changeable`.
    ``slug``, ``status`` and the trial/suspension timestamps stay read-only —
    slug is a routing identifier baked into sub-domains and stored OAuth
    redirect URIs, and status is driven by billing, not by the customer.
    """

    class Meta(TenantSerializer.Meta):
        read_only_fields = (
            "id", "slug", "status", "trial_ends_at", "suspended_at",
            "created_at", "updated_at",
        )

    def validate_country(self, value: str) -> str:
        from apps.iam.reference_data import country_map

        code = (value or "").strip().upper()
        if code not in country_map():
            raise serializers.ValidationError(
                "Unknown country code. Use one of the codes from "
                "GET /api/v1/auth/reference/."
            )
        return code

    def validate_timezone(self, value: str) -> str:
        from apps.iam.reference_data import timezones

        zone = (value or "").strip()
        if zone not in set(timezones()):
            raise serializers.ValidationError(
                "Unknown timezone. Use one of the IANA zones from "
                "GET /api/v1/auth/reference/."
            )
        return zone

    def validate_base_currency(self, value: str) -> str:
        """Refuse the change once the ledger has history, and say why.

        The check runs in ``validate_*`` rather than in ``update()`` so that a
        rejected PATCH reports the reason as a field error alongside any other
        problem in the same request, instead of failing on the first write and
        leaving the caller to discover the rest one round trip at a time.
        """
        from apps.tenancy.services.settings import assert_base_currency_changeable

        code = (value or "").strip().upper()
        tenant = self.instance
        if tenant is None or code == tenant.base_currency:
            return code

        from apps.core.models import Currency

        if code not in set(Currency.values):
            raise serializers.ValidationError(
                f"The ledger cannot be kept in {code} on this deployment. "
                f"Supported base currencies: {', '.join(sorted(Currency.values))}."
            )
        assert_base_currency_changeable(tenant)
        return code

    def update(self, instance: Tenant, validated_data: dict) -> Tenant:
        """Apply the change and write the before/after snapshot.

        The currency goes through
        :func:`apps.tenancy.services.settings.change_base_currency` rather
        than being assigned here, so the invariant holds for the management
        command and the support tool too — a rule enforced only in a
        serializer is a rule with exactly one caller.
        """
        from apps.core.middleware import get_client_ip, get_user_agent
        from apps.tenancy.services.settings import (
            change_base_currency,
            record_setting_change,
        )

        request = self.context.get("request")
        actor = getattr(request, "user", None)
        new_currency = validated_data.pop("base_currency", None)

        tracked = (
            "name", "legal_name", "country", "timezone",
            "fiscal_year_start_month", "tax_registration_number", "settings",
        )
        changes = {
            field: {"from": getattr(instance, field), "to": validated_data[field]}
            for field in tracked
            if field in validated_data and validated_data[field] != getattr(instance, field)
        }

        tenant = super().update(instance, validated_data)

        if new_currency and new_currency != tenant.base_currency:
            change_base_currency(
                tenant,
                new_currency,
                actor=actor,
                ip_address=get_client_ip(),
                user_agent=(get_user_agent() or "")[:512],
            )

        record_setting_change(
            tenant,
            actor=actor,
            changes=changes,
            ip_address=get_client_ip(),
            user_agent=(get_user_agent() or "")[:512],
        )
        return tenant
