"""Sentry event scrubbing.

This is an accounting/HR system: request bodies, local variables and query
strings routinely contain salaries, bank account numbers, tax IDs and auth
tokens. ``settings/prod.py`` wires ``before_send=scrub_event`` so none of that
leaves for a third party — but the module it pointed at did not exist, so **every
Sentry event in production raised ``ImportError``** and was dropped. This is that
module.

Belt-and-braces on top of ``send_default_pii=False`` and
``max_request_body_size="never"``: those stop Sentry *collecting* most PII, this
redacts anything sensitive that still slips through in a stack frame's locals or
an explicitly-attached extra.
"""

from __future__ import annotations

from typing import Any, Optional

#: Substrings (case-insensitive) that mark a key as too sensitive to ship.
_SENSITIVE_KEY_PARTS = (
    "password", "secret", "token", "authorization", "api_key", "apikey",
    "salary", "iban", "bank_account", "account_number", "tax_number",
    "tax_id", "ssn", "national_id", "card", "cvv", "otp", "session",
)
_REDACTED = "[redacted]"


def _is_sensitive(key: Any) -> bool:
    return isinstance(key, str) and any(part in key.lower() for part in _SENSITIVE_KEY_PARTS)


def _scrub(value: Any, _depth: int = 0) -> Any:
    """Recursively redact values under sensitive keys. Bounded depth so a
    self-referential structure cannot spin."""
    if _depth > 12:
        return value
    if isinstance(value, dict):
        return {
            k: (_REDACTED if _is_sensitive(k) else _scrub(v, _depth + 1))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return type(value)(_scrub(v, _depth + 1) for v in value)
    return value


def scrub_event(event: dict, hint: Optional[dict] = None) -> dict:
    """Sentry ``before_send`` hook. Redact sensitive keys anywhere in the event.

    Returns the mutated event (Sentry sends it) — never ``None``, which would
    silently drop the error and hide the very incident we are trying to see.
    """
    try:
        return _scrub(event)
    except Exception:  # pragma: no cover - scrubbing must never lose an error
        # If scrubbing itself fails, drop the request payload wholesale rather
        # than risk shipping unscrubbed PII, but keep the error.
        event.pop("request", None)
        return event
