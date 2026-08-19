"""
Authentication backend — ``settings.AUTHENTICATION_BACKENDS``.

Email is the username (``User.USERNAME_FIELD``), and the backend adds the two
things ``ModelBackend`` does not do:

* **Lockout.** ``User.failed_login_count`` / ``locked_until`` bound credential
  stuffing at the account level, which the per-IP rate limit cannot: an
  attacker with a botnet has more IPs than we have rate-limit buckets.
* **Timing equalisation.** A missing user still runs a password hash, so
  response time does not disclose which email addresses have accounts. In a
  multi-tenant product that is a customer list.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend
from django.utils import timezone

logger = logging.getLogger("erp.security")

#: Attempts before the account locks, and for how long. Short enough that a
#: user who fatfingers their password five times is not calling support, long
#: enough that online guessing is hopeless.
MAX_FAILED_LOGINS = 8
LOCKOUT_DURATION = timedelta(minutes=15)


class TenantAwareModelBackend(ModelBackend):
    """Email + password, with account lockout.

    Deliberately *not* tenant-aware in the sense of "authenticate into a
    tenant": a ``User`` is global and may belong to several organisations, so
    which tenant the session is bound to is decided after authentication, by
    ``TenantTokenObtainPairSerializer``. Folding it in here would mean the same
    credentials succeed or fail depending on a tenant hint the caller supplies,
    which turns a login form into a membership oracle.
    """

    def authenticate(self, request, username=None, password=None, **kwargs):
        UserModel = get_user_model()
        email = (username or kwargs.get(UserModel.USERNAME_FIELD) or "").strip().lower()
        if not email or password is None:
            return None

        try:
            user = UserModel.objects.get(email=email)
        except UserModel.DoesNotExist:
            # Run the hasher anyway: an early return here makes "unknown email"
            # measurably faster than "wrong password".
            UserModel().set_password(password)
            return None

        if user.is_locked:
            logger.info("login refused: account locked user=%s", user.id)
            return None

        if not user.check_password(password):
            self._record_failure(user)
            return None

        if not self.user_can_authenticate(user):
            return None

        self._record_success(user, request)
        return user

    @staticmethod
    def _record_failure(user) -> None:
        user.failed_login_count = (user.failed_login_count or 0) + 1
        fields = ["failed_login_count", "updated_at"]
        if user.failed_login_count >= MAX_FAILED_LOGINS:
            user.locked_until = timezone.now() + LOCKOUT_DURATION
            fields.append("locked_until")
            logger.warning(
                "account locked after %s failed logins user=%s",
                user.failed_login_count, user.id,
            )
        user.save(update_fields=fields)

    @staticmethod
    def _record_success(user, request) -> None:
        fields: list[str] = []
        if user.failed_login_count:
            user.failed_login_count = 0
            fields.append("failed_login_count")
        if user.locked_until is not None:
            user.locked_until = None
            fields.append("locked_until")

        ip = None
        if request is not None:
            forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
            ip = (forwarded.split(",")[0].strip() or None) if forwarded else (
                request.META.get("REMOTE_ADDR") or None
            )
        if ip and ip != user.last_login_ip:
            user.last_login_ip = ip
            fields.append("last_login_ip")

        if fields:
            user.save(update_fields=fields + ["updated_at"])
