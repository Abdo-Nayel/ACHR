"""
Shared serializer primitives.

Three rules are enforced here once, so that no downstream app has to remember
them:

1. **Money crosses the wire as a string.** See :class:`MoneyField`.
2. **The client never names its own tenant.** ``tenant`` is injected from the
   request context, never read from the request body — see
   :class:`TenantScopedSerializer`.
3. **State changes are not fields.** A status is read-only on every
   serializer; moving a document from one state to another is a POST to a
   sub-resource that runs the service layer. See
   :mod:`apps.core.viewsets`.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Iterable, Optional

from rest_framework import serializers

from apps.core.fields import (
    MONEY_DECIMAL_PLACES,
    MONEY_MAX_DIGITS,
    RATE_DECIMAL_PLACES,
    RATE_MAX_DIGITS,
)
from apps.core.tenancy_context import get_current_tenant_id, get_current_user_id

#: Fields that describe *who touched a row and when*. They are facts the
#: server establishes; a client that could write them could forge an audit
#: trail, so they are read-only on every tenant-scoped serializer.
AUDIT_FIELDS: tuple[str, ...] = (
    "id",
    "tenant",
    "created_by",
    "updated_by",
    "created_at",
    "updated_at",
)


def replace_draft_lines(model, *, tenant_id, **filters) -> None:
    """Drop a draft document's child lines so they can be rewritten wholesale.

    Editing a draft replaces its lines rather than diffing them — a diff is how
    a "removed" line survives and quietly inflates a total. But
    ``TenantQuerySet.delete()`` refuses bulk delete, so ``obj.lines.all()
    .delete()`` raises *403 "Bulk delete is disabled on tenant-scoped models"*
    on an entirely legitimate draft edit. Callers must have already refused
    anything past DRAFT, so these rows have no number, no journal entry and were
    never sent — intermediate input to a document still being typed, not
    financial records.

    The bypass goes through a plain (non-tenant) ``QuerySet`` filtered
    explicitly by ``tenant_id`` so it cannot widen its own scope. This was
    copied three ways — sales' ``_replace_draft_lines``, and inline in the
    expenses Bill and VendorCredit serializers (the Bill copy used the disabled
    manager and so 403'd on every bill edit) — now one function.
    """
    from django.db.models.query import QuerySet  # local: avoids a manager import cycle

    QuerySet(model=model).filter(tenant_id=tenant_id, **filters).delete()


class MoneyField(serializers.DecimalField):
    """A monetary amount, always serialised as a JSON **string**.

    JSON numbers go through IEEE-754 double in every JS client;
    9007199254740993 and 0.1 do not survive the round trip, so a totals row
    computed client-side disagrees with the ledger. Emitting
    ``"1234.560000"`` forces the client to parse it with a decimal library
    (the web and mobile clients use ``decimal.js``), which is the only way the
    number the user sees can equal the number PostgreSQL stores.

    The same argument applies in the other direction, so float *input* is
    rejected rather than coerced: a float in the request body means the
    caller already lost precision before we saw the value, and silently
    accepting it hides the bug in a place where it becomes a rounding
    discrepancy in a posted journal entry weeks later.
    """

    default_error_messages = {
        "float_forbidden": (
            "Send monetary amounts as JSON strings (e.g. \"1234.56\"), not as "
            "JSON numbers: a JSON number is parsed as a float and loses "
            "precision before the server ever sees it."
        ),
    }

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("max_digits", MONEY_MAX_DIGITS)
        kwargs.setdefault("decimal_places", MONEY_DECIMAL_PLACES)
        # Not a default: a caller passing coerce_to_string=False would defeat
        # the entire point of this class, so the value is pinned.
        kwargs["coerce_to_string"] = True
        super().__init__(**kwargs)

    def to_internal_value(self, data: Any) -> Decimal:
        # bool is a subclass of int, and `True` would quietly become 1.
        if isinstance(data, bool) or isinstance(data, float):
            self.fail("float_forbidden")
        return super().to_internal_value(data)


class QuantityField(MoneyField):
    """numeric(19,6) quantities (stock, billable hours). Same string rule.

    A quantity multiplied by a unit price becomes money, so a float here is a
    float in the ledger one multiplication later.
    """


class RateField(MoneyField):
    """numeric(9,6) tax/FX/percentage rates. Same string rule."""

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("max_digits", RATE_MAX_DIGITS)
        kwargs.setdefault("decimal_places", RATE_DECIMAL_PLACES)
        super().__init__(**kwargs)


# ---------------------------------------------------------------------------
# Base serializers
# ---------------------------------------------------------------------------

class ContextActorMixin:
    """Resolve the acting tenant/user from the serializer context.

    The tenant is read from the request that ``TenantMiddleware`` already
    resolved and validated against an active membership. The ambient
    ``ContextVar`` is the fallback so the same serializer works inside a
    Celery task or a management command running in ``tenant_context()``.
    """

    def _request(self):
        return self.context.get("request")

    def get_tenant_id(self):
        request = self._request()
        tenant_id = getattr(request, "tenant_id", None)
        if tenant_id is None:
            tenant_id = getattr(getattr(request, "tenant", None), "id", None)
        return tenant_id or get_current_tenant_id()

    def get_actor_id(self):
        request = self._request()
        user = getattr(request, "user", None)
        if user is not None and getattr(user, "is_authenticated", False):
            return user.id
        return get_current_user_id()


class TenantScopedSerializer(ContextActorMixin, serializers.ModelSerializer):
    """Base for every serializer over a :class:`~apps.core.models.TenantScopedModel`.

    ``tenant`` is **never** read from the request body. A writable tenant field
    is a one-line cross-tenant write: the caller posts an invoice with
    ``{"tenant": "<other company's uuid>"}`` and, because the row is then saved
    with that tenant id, it lands in someone else's books. The value is taken
    from the middleware-resolved request context instead, which has already
    been checked against an active ``TenantMembership``.

    ``created_by``/``updated_by`` are stamped here rather than in each viewset,
    because "the audit trail is only as good as the endpoint that remembered
    to fill it in" is not an audit trail.
    """

    #: Extra fields that must never be writable by a client (typically
    #: ``status``, ``number``, ``posted_at`` — anything a service owns).
    server_owned_fields: tuple[str, ...] = ()

    def get_fields(self) -> dict[str, Any]:
        fields = super().get_fields()
        for name in AUDIT_FIELDS + tuple(self.server_owned_fields):
            field = fields.get(name)
            if field is None:
                continue
            # Mutating after construction rather than passing read_only=True at
            # declaration: DRF asserts that read_only and required are not both
            # set in ``Field.__init__``, and required=True may come from the
            # model introspection we are overriding.
            field.read_only = True
            field.required = False

        # `currency` becomes optional so `create()` can inherit the
        # organisation's base currency. Left required, that default could
        # never run: DRF rejects the payload during validation, before any
        # write path is reached.
        #
        # Not made read-only — a genuinely foreign-currency document must
        # still be able to say so, and `fx.resolve_rate` then demands a rate
        # for it. This only removes the obligation to restate a fact the
        # server already owns.
        currency = fields.get("currency")
        if currency is not None and not currency.read_only:
            currency.required = False
        return fields

    # -- write paths --------------------------------------------------------

    def _strip_tenant(self, validated_data: dict) -> None:
        validated_data.pop("tenant", None)
        validated_data.pop("tenant_id", None)

    def _model_field_names(self) -> set[str]:
        return {f.name for f in self.Meta.model._meta.get_fields() if hasattr(f, "attname")}

    def create(self, validated_data: dict):
        self._strip_tenant(validated_data)

        names = self._model_field_names()
        if "tenant" in names:
            tenant_id = self.get_tenant_id()
            if tenant_id is None:
                # Fail closed and loudly. Letting the model's save() pick up an
                # ambient tenant that is also unset raises PermissionDenied
                # deep in the ORM, which surfaces as a 403 with no useful text.
                raise serializers.ValidationError(
                    "No organisation is bound to this request; the record "
                    "cannot be created."
                )
            validated_data["tenant_id"] = tenant_id

        # Currency inherits from the organisation when the caller omits it.
        #
        # Every document model declares `currency` as required with no
        # default, so an invoice, bill, expense or payroll run could only be
        # created by naming the currency explicitly. The tenant already has a
        # `base_currency` — chosen at signup and immutable once anything has
        # posted — so requiring it again on every call is asking the client to
        # repeat a fact the server owns, and the failure mode when they get it
        # wrong is an unintended FX exposure rather than an error.
        #
        # Only filled when absent: a genuinely foreign-currency document still
        # says so, and `fx.resolve_rate` will demand a rate for it.
        if "currency" in names and not validated_data.get("currency"):
            base = self._tenant_base_currency()
            if base:
                validated_data["currency"] = base

        actor_id = self.get_actor_id()
        if actor_id is not None:
            if "created_by" in names:
                validated_data.setdefault("created_by_id", actor_id)
            if "updated_by" in names:
                validated_data.setdefault("updated_by_id", actor_id)
        return super().create(validated_data)

    def _tenant_base_currency(self):
        """The organisation's reporting currency, or None if unresolvable.

        Read through `all_tenants` because `Tenant` is the scope row rather
        than a scoped one, and this runs on paths (signup, management
        commands) where no tenant is bound yet.
        """
        from apps.tenancy.models import Tenant  # noqa: PLC0415

        tenant_id = self.get_tenant_id()
        if tenant_id is None:
            return None
        return (
            Tenant.objects.filter(id=tenant_id)
            .values_list("base_currency", flat=True)
            .first()
        )

    def update(self, instance, validated_data: dict):
        # Re-parenting a row to another tenant is never an update; it is a
        # migration, and it does not happen through the API.
        self._strip_tenant(validated_data)
        actor_id = self.get_actor_id()
        if actor_id is not None and "updated_by" in self._model_field_names():
            validated_data["updated_by_id"] = actor_id
        return super().update(instance, validated_data)


class ReadOnlyModelSerializer(serializers.ModelSerializer):
    """Every field read-only; ``create``/``update`` refuse.

    Used for derived and reporting resources (trial balance rows, computed
    balances, audit log entries). Making the read-only-ness structural means a
    future ``ModelViewSet`` mounted over one of these cannot accidentally
    expose a writable projection of a table that has no write semantics.
    """

    def get_fields(self) -> dict[str, Any]:
        fields = super().get_fields()
        for field in fields.values():
            field.read_only = True
            field.required = False
        return fields

    def create(self, validated_data):  # pragma: no cover - guard rail
        raise NotImplementedError(f"{type(self).__name__} is read-only.")

    def update(self, instance, validated_data):  # pragma: no cover - guard rail
        raise NotImplementedError(f"{type(self).__name__} is read-only.")

    @classmethod
    def for_model(cls, model, fields="__all__"):
        """Build a read-only serializer class for ``model`` on the fly.

        Used by the placeholder URL modules so every table gets a listable
        endpoint without hand-writing a serializer per model first. Real
        serializers replace these as each module's write path is implemented —
        the generated class is a scaffold, not a destination, and it is
        read-only precisely so that nothing can start depending on it to
        write.
        """
        meta = type("Meta", (), {"model": model, "fields": fields})
        return type(f"{model.__name__}ReadSerializer", (cls,), {"Meta": meta})


class TransitionSerializer(serializers.Serializer):
    """Request body for a state-change sub-resource (``POST .../issue``).

    Every transition takes the same two optional inputs, so the client (and
    the generated TypeScript type) sees one shape everywhere:

    ``reason``
        Free text written to the audit trail. Individual transitions that
        legally require it — voiding a document, reopening a period — mark it
        required by subclassing and re-declaring the field, rather than
        checking for it in the view.

    ``idempotency_key``
        Client-generated UUID. The mobile client keeps an offline outbox and
        replays it when connectivity returns; without a key, a flaky network
        turns one "approve payroll" tap into two payroll runs. The
        ``Idempotency-Key`` header takes precedence over this field; the field
        exists for clients that cannot set headers (form posts, some webhook
        relays).
    """

    reason = serializers.CharField(
        required=False, allow_blank=True, max_length=1000, trim_whitespace=True
    )
    idempotency_key = serializers.CharField(required=False, allow_blank=True, max_length=200)

    def validated_reason(self) -> str:
        return (self.validated_data.get("reason") or "").strip()


class ReasonRequiredTransitionSerializer(TransitionSerializer):
    """For transitions where "why" is part of the record: void, reverse, reject.

    A voided invoice with no reason is a hole in the audit trail that nobody
    can explain a year later to a tax auditor.
    """

    reason = serializers.CharField(
        required=True, allow_blank=False, max_length=1000, trim_whitespace=True
    )


class IdempotencyKeySerializer(serializers.Serializer):
    """Body-only idempotency key, for creates that are not transitions."""

    idempotency_key = serializers.CharField(required=False, allow_blank=True, max_length=200)


def pick_error_fields(detail: Any) -> Optional[dict]:
    """Normalise a DRF detail into ``{field: [messages]}`` (test helper)."""
    if isinstance(detail, dict):
        return {k: v if isinstance(v, list) else [v] for k, v in detail.items()}
    return None


def enum_choices(choices: Iterable) -> list[dict[str, str]]:
    """``TextChoices`` -> ``[{"value": ..., "label": ...}]`` for the client.

    The client renders status filters and badges from this rather than
    hard-coding a list that drifts from the model's ``TextChoices``.
    """
    return [{"value": value, "label": str(label)} for value, label in choices]
