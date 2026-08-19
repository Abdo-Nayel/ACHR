"""
API-key authentication for server-to-server integrations.

Referenced by ``settings.REST_FRAMEWORK["DEFAULT_AUTHENTICATION_CLASSES"]``,
which DRF resolves while importing ``rest_framework.views`` — so this module
must import cleanly with no model access at import time.

Key format
----------
``<prefix>.<secret>`` — e.g. ``ak_7f3c9a21.<43 url-safe chars>``.

The prefix is stored in the clear and indexed, so a lookup is one indexed
equality match; the secret is never stored at all. Only ``sha256(secret)`` is
kept.

Why sha256 and not Argon2, when passwords use Argon2: a password is
low-entropy and human-chosen, so it needs a deliberately slow KDF to survive an
offline attack on a stolen hash. This secret is 32 bytes from ``secrets``,
which is not brute-forceable at any hash speed, and it is verified on *every*
API request — a 100 ms KDF there is a self-inflicted denial of service.

Authority
---------
A key carries ``ApiKey.role``, and ``ApiKeySerializer`` refuses to issue one
whose role outranks its creator. Note the current limitation, stated plainly
because it is a security property: the effective-permission resolver
(:func:`apps.iam.permissions.effective_permissions`) is keyed on
``(tenant, user)`` and knows nothing about keys, so a request authenticated by
a key acts with the permissions of the user who created it, not with the
narrower set implied by ``ApiKey.role``. Until that resolver takes a key into
account, issue integration keys from a dedicated service-account user whose own
roles are the intended ceiling.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from typing import Optional

from django.utils import timezone
from rest_framework import authentication, exceptions

logger = logging.getLogger("erp.security")

API_KEY_HEADER = "HTTP_X_API_KEY"
AUTH_SCHEME = "apikey"
PREFIX_BYTES = 4
SECRET_BYTES = 32


def hash_api_key(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def generate_api_key() -> tuple[str, str, str]:
    """Return ``(prefix, secret, key_hash)``. The secret is never stored."""
    prefix = f"ak_{secrets.token_hex(PREFIX_BYTES)}"  # 11 chars, fits max_length=12
    secret = secrets.token_urlsafe(SECRET_BYTES)
    return prefix, secret, hash_api_key(secret)


def split_key(raw: str) -> tuple[Optional[str], Optional[str]]:
    prefix, separator, secret = (raw or "").strip().partition(".")
    if not separator or not prefix or not secret:
        return None, None
    return prefix, secret


class ApiKeyAuthentication(authentication.BaseAuthentication):
    """``Authorization: ApiKey <prefix>.<secret>`` or ``X-Api-Key: ...``.

    Returns ``(user, payload)`` where ``payload`` is a plain dict carrying the
    tenant claim. That shape is deliberate: ``TenantMiddleware`` reads
    ``request.auth.get("tenant")``, so a key-authenticated request resolves its
    tenant from the key itself and never needs an ``X-Tenant-ID`` header.
    """

    keyword = "ApiKey"

    def authenticate(self, request):
        raw = self._raw_key(request)
        if not raw:
            return None  # fall through to JWT

        prefix, secret = split_key(raw)
        if prefix is None:
            raise exceptions.AuthenticationFailed("Malformed API key.")

        from apps.iam.models import ApiKey

        api_key = (
            ApiKey.objects.select_related("tenant", "created_by", "role")
            .filter(prefix=prefix)
            .first()
        )
        # Constant-time comparison even on the miss path, so response timing
        # does not distinguish "no such prefix" from "wrong secret".
        expected = api_key.key_hash if api_key is not None else ""
        if not hmac.compare_digest(expected, hash_api_key(secret)) or api_key is None:
            logger.info("api key auth failed prefix=%s", prefix)
            raise exceptions.AuthenticationFailed("Invalid API key.")

        now = timezone.now()
        if api_key.revoked_at is not None:
            raise exceptions.AuthenticationFailed("This API key has been revoked.")
        if api_key.expires_at is not None and api_key.expires_at <= now:
            raise exceptions.AuthenticationFailed("This API key has expired.")

        user = api_key.created_by
        if not user.is_active:
            raise exceptions.AuthenticationFailed("The owner of this key is deactivated.")

        # Written unconditionally but cheaply (one indexed UPDATE): "when was
        # this key last used" is the question asked before every key rotation.
        ApiKey.objects.filter(pk=api_key.pk).update(last_used_at=now)

        request.api_key = api_key
        return user, {
            "tenant": str(api_key.tenant_id),
            "tid": str(api_key.tenant_id),
            "api_key_id": str(api_key.id),
            "role": api_key.role.code,
            "token_type": "api_key",
        }

    def authenticate_header(self, request) -> str:
        return self.keyword

    def _raw_key(self, request) -> str:
        header = request.META.get(API_KEY_HEADER)
        if header:
            return header.strip()
        auth = authentication.get_authorization_header(request).split()
        if len(auth) == 2 and auth[0].lower() == AUTH_SCHEME.encode():
            return auth[1].decode("latin-1")
        return ""
