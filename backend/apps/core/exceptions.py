"""
One error envelope for the entire API.

Every failure — DRF validation, a domain rule, an unhandled crash — leaves the
API in the same shape:

    {"error": {"code": "...", "detail": "...", "status": 4xx,
               "fields": {...}, "request_id": "..."}}

A machine-readable ``code`` matters more than the prose. A mobile client must
be able to tell "this invoice period is closed, show the reopen hint" from
"the amount you typed is invalid, highlight the field" without regex-matching
an English sentence that a translator will change next sprint.

Domain exceptions are mapped here rather than each service raising an HTTP
error, so that the service layer stays callable from Celery and management
commands where HTTP status codes are meaningless.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError
from django.http import Http404
from rest_framework import status
from rest_framework.exceptions import APIException
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

logger = logging.getLogger(__name__)


class DomainError(APIException):
    """Base for errors that are the caller's problem, not a crash."""

    status_code = status.HTTP_400_BAD_REQUEST
    default_code = "domain_error"
    default_detail = "The request could not be completed."


class UnbalancedEntryError(DomainError):
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    default_code = "unbalanced_entry"
    default_detail = "Debits and credits do not balance."


class PeriodClosedError(DomainError):
    status_code = status.HTTP_409_CONFLICT
    default_code = "period_closed"
    default_detail = "The accounting period is closed."


class InsufficientStockError(DomainError):
    status_code = status.HTTP_409_CONFLICT
    default_code = "insufficient_stock"
    default_detail = "Not enough stock on hand."


class IllegalTransitionError(DomainError, ValueError):
    """A status change the state machine forbids.

    Deliberately a ``ValueError`` as well as a ``DomainError``. An illegal
    transition *is* a value error — a caller asked for a state the object
    cannot be in — and code that guards transitions with ``except ValueError``
    has always relied on that. It is also a client mistake, not a server fault,
    so it must render as an HTTP 409 rather than falling through the exception
    handler to a 500. Inheriting both is what lets one exception satisfy both
    truths, so :class:`~apps.core.models.StatusTransitionMixin` can replace the
    dozen hand-written ``raise ValueError(...)`` guards without changing either
    the HTTP contract or the callers that catch them.
    """

    status_code = status.HTTP_409_CONFLICT
    default_code = "illegal_transition"
    default_detail = "That status change is not allowed."


class DuplicateIdempotencyKey(DomainError):
    status_code = status.HTTP_409_CONFLICT
    default_code = "duplicate_idempotency_key"
    default_detail = "This request has already been processed."


class TenantNotOperational(DomainError):
    status_code = status.HTTP_402_PAYMENT_REQUIRED
    default_code = "tenant_not_operational"
    default_detail = "This organisation cannot perform write operations."


class ConcurrentModification(DomainError):
    status_code = status.HTTP_412_PRECONDITION_FAILED
    default_code = "concurrent_modification"
    default_detail = "The record changed since you loaded it."


class GatewayError(DomainError):
    status_code = status.HTTP_502_BAD_GATEWAY
    default_code = "gateway_error"
    default_detail = "The payment gateway rejected or failed the request."


#: Maps domain exception *class names* from the service layer onto API errors.
#: Matching by name rather than by import keeps this module free of imports
#: from every business app, which would create a cycle at startup.
_SERVICE_ERROR_MAP: dict[str, type[DomainError]] = {
    "UnbalancedEntry": UnbalancedEntryError,
    "PeriodClosed": PeriodClosedError,
    "DuplicatePosting": DuplicateIdempotencyKey,
    "InsufficientStock": InsufficientStockError,
    "NegativeStockNotAllowed": InsufficientStockError,
    "NetIdentityViolation": UnbalancedEntryError,
    "PayrollError": DomainError,
    "NoDefaultWarehouse": DomainError,
}


def _normalise_fields(detail: Any) -> Optional[dict]:
    """Flatten DRF's nested error detail into ``{field: [messages]}``."""
    if isinstance(detail, dict):
        return {
            key: value if isinstance(value, list) else [value]
            for key, value in detail.items()
        }
    return None


def _envelope(code: str, detail: str, status_code: int, fields=None) -> dict:
    from apps.core.middleware import get_request_id

    body: dict[str, Any] = {
        "code": code,
        "detail": detail,
        "status": status_code,
    }
    if fields:
        body["fields"] = fields
    request_id = get_request_id()
    if request_id:
        # Echoed so a user can paste it into a support ticket and we can find
        # the exact request in the logs without asking them what they clicked.
        body["request_id"] = request_id
    return {"error": body}


def api_exception_handler(exc, context):
    """DRF ``EXCEPTION_HANDLER``."""

    # --- translate framework-agnostic exceptions into API errors -----------
    name = type(exc).__name__
    if name in _SERVICE_ERROR_MAP and not isinstance(exc, APIException):
        mapped = _SERVICE_ERROR_MAP[name]
        exc = mapped(detail=str(getattr(exc, "message", None) or exc))

    elif isinstance(exc, DjangoValidationError):
        detail = getattr(exc, "message_dict", None) or getattr(exc, "messages", [str(exc)])
        exc = DomainError(detail=detail)
        exc.default_code = "validation_error"

    elif isinstance(exc, DjangoPermissionDenied):
        from rest_framework.exceptions import PermissionDenied as DRFPermissionDenied

        exc = DRFPermissionDenied(detail=str(exc) or None)

    elif isinstance(exc, Http404):
        from rest_framework.exceptions import NotFound

        exc = NotFound()

    elif isinstance(exc, IntegrityError):
        text = str(exc)
        if "uq_entry_idempotency" in text or "idempotency" in text:
            exc = DuplicateIdempotencyKey()
        elif "ck_entry_balanced" in text or "balance" in text.lower():
            exc = UnbalancedEntryError(
                "The database rejected this entry because it does not balance."
            )
        else:
            # A constraint we did not anticipate is a bug, not user error.
            # Log the real text, return a generic message: constraint names
            # and column names in an API response are reconnaissance.
            logger.exception("Unhandled IntegrityError", extra={"db_error": text})
            return Response(
                _envelope(
                    "constraint_violation",
                    "The request conflicts with existing data.",
                    status.HTTP_409_CONFLICT,
                ),
                status=status.HTTP_409_CONFLICT,
            )

    response = drf_exception_handler(exc, context)

    if response is None:
        # Genuinely unhandled: a bug. Log with the traceback, tell the client
        # nothing about our internals.
        logger.exception("Unhandled exception in %s", context.get("view"))
        return Response(
            _envelope(
                "internal_error",
                "An unexpected error occurred. The incident has been logged.",
                status.HTTP_500_INTERNAL_SERVER_ERROR,
            ),
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    code = getattr(exc, "default_code", "error")
    detail = exc.detail if hasattr(exc, "detail") else str(exc)
    fields = _normalise_fields(detail)
    message = (
        "Validation failed." if fields
        else (detail[0] if isinstance(detail, list) and detail else str(detail))
    )

    response.data = _envelope(str(code), str(message), response.status_code, fields)
    return response
