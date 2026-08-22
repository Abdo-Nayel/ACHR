"""The one status-transition state machine, and the bug it closes.

Before ``apps.core.models.StatusTransitionMixin`` existed, eighteen models
hand-wrote ``assert_can_transition`` and disagreed on what to raise: some a
bare ``ValueError``, some a Django ``ValidationError``. The API only mapped the
latter to a 4xx, so the *same* user mistake — "you cannot void a paid invoice"
— surfaced as a 409 from one endpoint and a **500 Internal Server Error** from
another. These tests pin the fix: one exception, ``IllegalTransitionError``,
that is simultaneously a ``ValueError`` (so pre-existing ``except ValueError``
guards still catch it) and an HTTP 409 (so it never reaches the 500 handler).
"""

from __future__ import annotations

import pytest
from rest_framework import status
from rest_framework.exceptions import APIException

from apps.core.exceptions import IllegalTransitionError, api_exception_handler
from apps.core.models import StatusTransitionMixin


class _Doc(StatusTransitionMixin):
    """A throwaway document with a two-state machine, no database involved."""

    ALLOWED_TRANSITIONS = {"draft": {"sent"}, "sent": set()}

    def __init__(self, status_value: str) -> None:
        self.status = status_value


def test_legal_transition_is_allowed():
    _Doc("draft").assert_can_transition("sent")  # must not raise


def test_illegal_transition_raises_illegal_transition_error():
    with pytest.raises(IllegalTransitionError):
        _Doc("sent").assert_can_transition("draft")


def test_illegal_transition_is_still_a_value_error():
    """Back-compat: services that guard with ``except ValueError`` keep working."""
    with pytest.raises(ValueError):
        _Doc("sent").assert_can_transition("draft")


def test_illegal_transition_is_a_409_not_a_500():
    """The bug: this must be a client error (409), never a server error (500)."""
    exc = IllegalTransitionError()
    assert isinstance(exc, APIException)
    assert exc.status_code == status.HTTP_409_CONFLICT


def test_exception_handler_renders_409_envelope():
    """End to end through DRF's handler: the response is a 409 with our code,
    not the generic 500 an unmapped ``ValueError`` would have produced."""
    try:
        _Doc("sent").assert_can_transition("draft")
    except IllegalTransitionError as exc:
        response = api_exception_handler(exc, {"view": None})
    assert response is not None
    assert response.status_code == status.HTTP_409_CONFLICT
    assert response.data["error"]["code"] == "illegal_transition"


def test_message_names_the_document_and_the_move():
    exc = pytest.raises(IllegalTransitionError,
                        _Doc("sent").assert_can_transition, "draft")
    assert "sent -> draft" in str(exc.value)
