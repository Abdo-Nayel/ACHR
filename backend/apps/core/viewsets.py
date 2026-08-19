"""
Viewset bases: tenant scoping, RBAC wiring, and state transitions.

Why state transitions are POST sub-resources
--------------------------------------------
``POST /invoices/{id}/issue`` — never ``PATCH /invoices/{id} {"status": "sent"}``.

A writable ``status`` field means any client that can update the object can
move money past every guard: issuing allocates a gap-free invoice number,
posts a balanced journal entry and releases stock, and a PATCH that only
writes the column does none of it. The ledger then disagrees with the
document, and it disagrees *silently* — the API returned 200.

The sub-resource form also gives us, for free:

* one permission per transition (``sales.invoice.issue`` is not
  ``sales.invoice.update``), which is what makes segregation of duties
  expressible at all;
* a place to require re-authentication for sensitive actions;
* an obvious idempotency boundary (``Idempotency-Key``), because "issue this
  invoice" is a thing that must happen exactly once, while "set this field" is
  naturally repeatable;
* an audit record whose *verb* is the business action, not "row updated".

Every ``status``-like column is therefore read-only on the serializer (see
:class:`apps.core.serializers.TenantScopedSerializer.server_owned_fields`) and
the only way to change it is a service call.

Why ``destroy`` is disabled by default
--------------------------------------
CONVENTIONS §5: business documents are archived, voided or reversed, never
hard-deleted. A DELETE that succeeds destroys the audit trail and silently
changes reports that have already been filed. The route stays mounted and
answers 405 with a pointer to the correct action, because a 404 would look
like a routing bug and a silent success would look like a feature.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any, Callable, Iterable, Optional, Sequence

from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import APIException, MethodNotAllowed
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.core.exceptions import DomainError, DuplicateIdempotencyKey
from apps.core.pagination import TenantCursorPagination
from apps.core.serializers import TransitionSerializer
from apps.core.tenancy_context import get_current_tenant_id, get_current_user_id
from apps.iam.permissions import HasPermission, ScopedQuerysetMixin

logger = logging.getLogger(__name__)

IDEMPOTENCY_HEADER = "HTTP_IDEMPOTENCY_KEY"

#: HTTP method -> the action verb in a ``<domain>.<resource>.<action>``
#: codename. ``OPTIONS``/``HEAD`` inherit ``read``; a bare OPTIONS probe of a
#: resource still discloses that the resource exists.
_METHOD_ACTIONS: dict[str, str] = {
    "GET": "read",
    "HEAD": "read",
    "OPTIONS": "read",
    "POST": "create",
    "PUT": "update",
    "PATCH": "update",
    "DELETE": "delete",
}


class HardDeleteDisabled(APIException):
    """405 for DELETE on a document that must be archived or voided."""

    status_code = status.HTTP_405_METHOD_NOT_ALLOWED
    default_code = "hard_delete_disabled"
    default_detail = (
        "Records in this system are archived, voided or reversed — never "
        "deleted. Use the archive or void action on this resource."
    )


# ---------------------------------------------------------------------------
# Shared behaviour
# ---------------------------------------------------------------------------

class TenantViewSetMixin(ScopedQuerysetMixin):
    """Permission wiring, query shaping and pagination for tenant resources.

    Declare two class attributes and the RBAC table builds itself::

        class InvoiceViewSet(TenantModelViewSet):
            permission_domain = "sales"
            resource = "invoice"
            queryset = Invoice.objects.all()
            serializer_class = InvoiceSerializer
            select_related = ("customer", "journal_entry")
            extra_permissions = {"issue": ["sales.invoice.issue"]}

    ``resource`` does double duty: it is the ``Permission.resource`` half of
    the codename *and* the ``ScopeRule.resource`` the ABAC layer compiles, so
    the two can never drift apart for the same endpoint.
    """

    #: ``<domain>`` half of the permission codename (``Permission.Domain``).
    permission_domain: Optional[str] = None
    #: ``<resource>`` half of the codename, and the ABAC scope resource.
    resource: Optional[str] = None
    #: DRF ``@action`` name -> required codenames. Merged over the defaults.
    extra_permissions: dict[str, Sequence[str]] = {}

    select_related: tuple[str, ...] = ()
    prefetch_related: tuple[str, ...] = ()

    permission_classes = [IsAuthenticated, HasPermission]
    pagination_class = TenantCursorPagination

    # -- ABAC ---------------------------------------------------------------

    def get_scope_resource(self) -> str:
        # ScopedQuerysetMixin reads ``scope_resource``; default it to
        # ``resource`` so a viewset declares the name once.
        return self.scope_resource or self.resource or super().get_scope_resource()

    # -- RBAC ---------------------------------------------------------------

    @property
    def required_permissions(self) -> Optional[dict[str, Sequence[str]]]:
        """Table consumed by :class:`apps.iam.permissions.HasPermission`.

        Returning ``None`` when the viewset declares no domain/resource is
        deliberate: ``HasPermission`` denies on ``None``. A viewset that
        forgets to say what it guards is closed, not open.

        A subclass that needs something exotic can still assign a plain dict
        class attribute — it shadows this property in the MRO.
        """
        if not (self.permission_domain and self.resource):
            return None
        prefix = f"{self.permission_domain}.{self.resource}"
        table: dict[str, Sequence[str]] = {
            method: [f"{prefix}.{verb}"] for method, verb in _METHOD_ACTIONS.items()
        }
        table.update(self.extra_permissions or {})
        return table

    # -- queryset -----------------------------------------------------------

    def get_queryset(self):
        # Rebuild from the model's manager on every request. This is not
        # ceremony — it is load-bearing.
        #
        # `queryset = Invoice.objects.all()` as a class attribute is evaluated
        # once, at import time, when no tenant is bound. `TenantManager`
        # fails closed in that situation and returns `.none()`, and DRF then
        # reuses that frozen empty queryset for the life of the process. The
        # symptom is the worst kind: HTTP 200, a well-formed envelope, and an
        # empty `results` array on every list endpoint in the product, with no
        # error anywhere to explain it. Re-deriving here means the manager
        # runs inside the request, with the tenant actually bound.
        declared = getattr(self, "queryset", None)
        if declared is not None:
            self.queryset = declared.model._default_manager.all()

        # super() is ScopedQuerysetMixin: GenericAPIView's queryset (now
        # tenant-filtered by TenantManager) narrowed by the actor's scope Q.
        queryset = super().get_queryset()
        if self.select_related:
            queryset = queryset.select_related(*self.select_related)
        if self.prefetch_related:
            queryset = queryset.prefetch_related(*self.prefetch_related)
        return queryset

    # -- context ------------------------------------------------------------

    def get_serializer_context(self) -> dict[str, Any]:
        context = super().get_serializer_context()
        context["tenant_id"] = getattr(self.request, "tenant_id", None) or get_current_tenant_id()
        context["membership"] = getattr(self.request, "membership", None)
        return context


class TenantModelViewSet(TenantViewSetMixin, viewsets.ModelViewSet):
    """Full CRUD over a tenant-scoped model, minus the D."""

    def destroy(self, request, *args, **kwargs):
        raise HardDeleteDisabled(
            f"{self.get_queryset().model._meta.verbose_name} records are not "
            f"deleted. Archive the record, or void/reverse the document if it "
            f"has been posted — the audit trail must survive the correction."
        )


class AllowDestroyMixin:
    """Re-enables DELETE for the few models where a hard delete is legitimate.

    Legitimate cases, and only these:

    * child lines of an **unposted** parent (``InvoiceLine`` on a DRAFT
      invoice) — nothing downstream references them and no ledger row exists;
    * pure join rows (``ProjectMember``, ``RolePermission``) — the fact they
      record is "this link exists now", which has no history to preserve.

    Mount it to the left of the viewset base::

        class InvoiceLineViewSet(AllowDestroyMixin, TenantModelViewSet):
            ...

    The model's own ``delete()`` is still the final word: an
    ``ImmutableFinancialModel`` raises regardless of what the viewset allows.
    """

    def destroy(self, request, *args, **kwargs):
        return mixins.DestroyModelMixin.destroy(self, request, *args, **kwargs)


class ReadOnlyTenantViewSet(TenantViewSetMixin, viewsets.ReadOnlyModelViewSet):
    """List/retrieve only: reporting projections and derived resources.

    Derived resources (an AR aging bucket, a trial-balance row) have no write
    semantics at all — the way to change one is to post a journal entry. A
    ``ModelViewSet`` here would advertise POST/PATCH routes that could only
    ever return an error.
    """


# ---------------------------------------------------------------------------
# Transitions
# ---------------------------------------------------------------------------

def _resolve(service: Any) -> Callable:
    """Import a service by dotted path, lazily.

    Transitions are declared at class-definition time, but the service modules
    import models, serializers and other services. Resolving the dotted path on
    first call keeps ``apps.core.viewsets`` importable from anywhere without
    dragging half the project into the import graph.
    """
    if callable(service):
        return service
    module_path, _, attr = str(service).rpartition(".")
    if not module_path:
        raise ImproperlyConfiguredTransition(f"{service!r} is not a dotted service path.")
    from importlib import import_module

    return getattr(import_module(module_path), attr)


class ImproperlyConfiguredTransition(APIException):
    status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
    default_code = "transition_misconfigured"
    default_detail = "This transition endpoint is not correctly configured."


def raise_as_api_error(exc: BaseException) -> None:
    """Translate a service-layer exception into the API error vocabulary.

    The service layer raises framework-agnostic exceptions on purpose — it is
    called from Celery tasks and management commands where HTTP status codes
    are meaningless. This is the one place that maps them, reusing the same
    table :mod:`apps.core.exceptions` uses so a domain error reported through a
    transition endpoint and one reported through a plain save produce the same
    ``code``.
    """
    from apps.core import exceptions as core_exceptions

    mapped = core_exceptions._SERVICE_ERROR_MAP.get(type(exc).__name__)
    if mapped is not None and not isinstance(exc, APIException):
        raise mapped(detail=str(getattr(exc, "message", None) or exc)) from exc

    if isinstance(exc, DjangoValidationError):
        detail = getattr(exc, "message_dict", None) or getattr(exc, "messages", [str(exc)])
        error = DomainError(detail=detail)
        error.default_code = "validation_error"
        raise error from exc

    if isinstance(exc, DjangoPermissionDenied):
        from rest_framework.exceptions import PermissionDenied as DRFPermissionDenied

        raise DRFPermissionDenied(str(exc) or None) from exc

    # APIException subclasses and anything unrecognised keep travelling: the
    # DRF exception handler renders the former and logs the latter as a bug.
    raise exc


def read_idempotency_key(request, body: Optional[dict] = None) -> str:
    """``Idempotency-Key`` header, falling back to the request body.

    Header first: it survives a client retrying with the same serialized body
    through a proxy that rewrites payloads, and it is what the mobile outbox
    sets.
    """
    header = (request.META.get(IDEMPOTENCY_HEADER) or "").strip()
    if header:
        return header
    return ((body or {}).get("idempotency_key") or "").strip()


def _idempotency_cache_key(resource: str, transition: str, key: str) -> str:
    # The default cache's key function already namespaces by the bound tenant
    # (apps.core.cache.tenant_key_func), so two tenants replaying the same
    # client-generated UUID cannot collide.
    return f"idem:{resource}:{transition}:{key}"


def _remember_idempotent_result(cache_key: str, object_id: Any) -> None:
    from django.conf import settings
    from django.core.cache import cache

    try:
        cache.set(cache_key, str(object_id),
                  getattr(settings, "IDEMPOTENCY_KEY_TTL_SECONDS", 86400))
    except Exception:  # pragma: no cover - cache outage must not fail the write
        logger.warning("idempotency cache write failed for %s", cache_key, exc_info=True)


def _recall_idempotent_result(cache_key: str) -> Optional[str]:
    from django.core.cache import cache

    try:
        return cache.get(cache_key)
    except Exception:  # pragma: no cover
        logger.warning("idempotency cache read failed for %s", cache_key, exc_info=True)
        return None


def _transition_name(service: Any, url_path: Optional[str]) -> str:
    """The action name: explicit ``url_path`` wins, else the service's name."""
    if url_path:
        return url_path.replace("-", "_")
    name = getattr(service, "__name__", None) or str(service).rpartition(".")[2]
    return name or "transition"


def transition_action(
    service: Any,
    *,
    url_path: Optional[str] = None,
    methods: Iterable[str] = ("post",),
    body_serializer: type = TransitionSerializer,
    pass_reason: bool = False,
    detail: bool = True,
    build_kwargs: Optional[Callable[[Any, Any, dict], dict]] = None,
    **action_kwargs: Any,
):
    """Build a DRF ``@action`` that runs a service-layer state transition.

    ::

        class InvoiceViewSet(TenantModelViewSet):
            permission_domain = "sales"
            resource = "invoice"
            extra_permissions = {
                "issue": ["sales.invoice.issue"],
                "void":  ["sales.invoice.void"],
            }

            issue = transition_action("apps.sales.services.invoice_workflow.issue_invoice")
            void = transition_action(
                "apps.sales.services.invoice_workflow.void_invoice",
                body_serializer=ReasonRequiredTransitionSerializer,
                pass_reason=True,
            )

    What the generated handler does, in order:

    1. ``get_object()`` — which runs the ABAC scope check, so a transition can
       never act on a row the caller cannot see.
    2. Validates the body with ``body_serializer``.
    3. Reads ``Idempotency-Key``. A replay of a key we have already seen
       returns the *same* object with ``Idempotency-Replayed: true`` instead
       of running the service twice; a key already bound to a different object
       is a client bug and returns 409.
    4. Calls the service inside ``transaction.atomic()``. The service does the
       locking, the guard checks and the GL posting; the view contributes no
       business logic whatsoever, which is what keeps the same transition
       callable from a Celery task.
    5. Maps service exceptions onto the core error classes.
    6. Returns the refreshed object through the viewset's own serializer, so
       the client never has to re-fetch to see the new status and totals.

    Services in this codebase take ``(object_id, *, tenant_id, user_id=None,
    **kwargs)``; only the keyword arguments the target actually accepts are
    passed, so a service without an ``idempotency_key`` parameter does not
    blow up on one.
    """

    def handler(self, request, *args, **kwargs):
        instance = self.get_object()
        transition_name = getattr(self, "action", None) or (url_path or "transition")

        body = body_serializer(data=request.data)
        body.is_valid(raise_exception=True)
        payload = dict(body.validated_data)

        tenant_id = getattr(request, "tenant_id", None) or get_current_tenant_id()
        user_id = getattr(getattr(request, "user", None), "id", None) or get_current_user_id()

        key = read_idempotency_key(request, payload)
        cache_key = None
        if key:
            cache_key = _idempotency_cache_key(
                getattr(self, "resource", None) or "resource", transition_name, key
            )
            previous = _recall_idempotent_result(cache_key)
            if previous is not None:
                if str(previous) != str(instance.pk):
                    raise DuplicateIdempotencyKey(
                        "This Idempotency-Key was already used for a different "
                        "record. Generate a new key per logical operation."
                    )
                response = Response(self.get_serializer(instance).data)
                response["Idempotency-Replayed"] = "true"
                return response

        call_kwargs: dict[str, Any] = {"tenant_id": tenant_id, "user_id": user_id}
        if pass_reason:
            call_kwargs["reason"] = payload.get("reason") or ""
        if key:
            call_kwargs["idempotency_key"] = key
        if build_kwargs is not None:
            call_kwargs.update(build_kwargs(self, instance, payload))

        func = _resolve(service)
        try:
            accepted = inspect.signature(func).parameters
        except (TypeError, ValueError):  # pragma: no cover - builtins
            accepted = {}
        if accepted and not any(
            p.kind is inspect.Parameter.VAR_KEYWORD for p in accepted.values()
        ):
            call_kwargs = {k: v for k, v in call_kwargs.items() if k in accepted}

        try:
            # ATOMIC_REQUESTS already wraps the request; this nested atomic
            # becomes a savepoint, which is what we want — a failed transition
            # must not leave a half-written document behind, and the tenant
            # binding (SET LOCAL) still belongs to the outer transaction.
            with transaction.atomic():
                result = func(instance.pk, **call_kwargs)
        except Exception as exc:  # noqa: BLE001 - re-raised as an API error
            raise_as_api_error(exc)
            raise  # pragma: no cover - raise_as_api_error never returns

        refreshed = result if hasattr(result, "pk") else None
        if refreshed is None:
            refreshed = self.get_queryset().filter(pk=instance.pk).first() or instance

        if cache_key is not None:
            _remember_idempotent_result(cache_key, refreshed.pk)

        return Response(self.get_serializer(refreshed).data, status=status.HTTP_200_OK)

    # DRF reads ``func.__name__`` to derive the route name *and* the value of
    # ``self.action`` at request time — which is the key ``HasPermission``
    # looks up in ``extra_permissions``. Naming the handler after the URL
    # segment keeps those three spellings identical by construction.
    handler.__name__ = _transition_name(service, url_path)
    handler.__doc__ = (
        f"State transition delegated to ``{service}``. "
        f"POST-only sub-resource: see apps.core.viewsets for why a status "
        f"field is never writable."
    )

    return action(
        detail=detail,
        methods=[m.lower() for m in methods],
        url_path=url_path,
        **action_kwargs,
    )(handler)


def deny_method(message: str):
    """Replace an inherited handler with an explicit, explained 405."""

    def handler(self, request, *args, **kwargs):
        raise MethodNotAllowed(request.method, detail=message)

    return handler
