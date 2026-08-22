"""
Ambient tenant + actor context.

The tenant is resolved once per request (or once per Celery task) and stored
in a ``ContextVar``. Two consumers read it:

* :class:`apps.core.models.TenantManager` -- ORM-level filtering.
* :func:`bind_database_session` -- sets the PostgreSQL session variable
  ``app.current_tenant`` that every Row-Level Security policy reads.

``ContextVar`` rather than thread-local: it is the only primitive that
survives ``async def`` views and ``asgiref.sync_to_async`` correctly. A
thread-local silently leaks between coroutines sharing a worker thread,
which in a multi-tenant financial system means cross-tenant data exposure.
"""

from __future__ import annotations

import contextlib
import uuid
from contextvars import ContextVar
from typing import Iterator, Optional

from django.db import connection, transaction

_current_tenant_id: ContextVar[Optional[uuid.UUID]] = ContextVar(
    "current_tenant_id", default=None
)
_current_user_id: ContextVar[Optional[uuid.UUID]] = ContextVar(
    "current_user_id", default=None
)
#: Set only by trusted platform-admin code paths.
_rls_bypass: ContextVar[bool] = ContextVar("rls_bypass", default=False)


def get_current_tenant_id() -> Optional[uuid.UUID]:
    return _current_tenant_id.get()


def get_current_user_id() -> Optional[uuid.UUID]:
    return _current_user_id.get()


def bind_database_session(tenant_id: Optional[uuid.UUID], *, bypass: bool = False) -> None:
    """Push the tenant onto the live PostgreSQL session.

    ``SET LOCAL`` scopes the setting to the surrounding transaction, so a
    pooled connection handed to the next request cannot inherit a stale
    tenant. Callers must therefore be inside ``transaction.atomic()``.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('app.current_tenant', %s, true), "
            "       set_config('app.rls_bypass', %s, true)",
            [str(tenant_id) if tenant_id else "", "on" if bypass else "off"],
        )


@contextlib.contextmanager
def tenant_context(tenant_id, user_id=None) -> Iterator[None]:
    """Bind a tenant for a block, on **both** layers, for non-request code
    (Celery tasks, management commands, tests).

    Two guards read two different things: the ORM ``TenantManager`` reads the
    ``ContextVar`` set here, and Row-Level Security reads the PostgreSQL session
    variable ``app.current_tenant``. Binding only the first was a real bug — a
    Celery task that did so read *nothing* under the non-owner role, because RLS
    saw no tenant and filtered every row, silently. So this now sets the
    ContextVar *and* calls :func:`bind_database_session`.

    ``bind_database_session`` issues ``SET LOCAL``, which lives only inside a
    transaction, so this opens one for the block. A web request does not use
    this helper — its middleware already owns the transaction and binds inside
    it — so nothing on the request hot path changes. The previous value is
    always restored, including on error, so a task that raises cannot leak its
    tenant onto the next job on a pooled worker connection.
    """
    tid = uuid.UUID(str(tenant_id)) if tenant_id else None
    uid = uuid.UUID(str(user_id)) if user_id else None
    tenant_token = _current_tenant_id.set(tid)
    user_token = _current_user_id.set(uid)
    try:
        if connection.vendor == "postgresql":
            with transaction.atomic():
                bind_database_session(tid)
                yield
        else:
            # No RLS to bind (SQLite in some unit tests); the ORM ContextVar
            # guard above is the whole story there.
            yield
    finally:
        _current_tenant_id.reset(tenant_token)
        _current_user_id.reset(user_token)


@contextlib.contextmanager
def platform_admin_context() -> Iterator[None]:
    """Temporarily disable tenant filtering at the database layer. Audit-logged
    by the caller.

    Sets ``app.rls_bypass = on`` on the session, which the RLS policy reads as
    its escape hatch. This previously set only a ``ContextVar`` that nothing
    ever read, so the "platform admin bypass" widened nothing — a
    security-relevant no-op. ``SET LOCAL`` needs a transaction, hence the
    ``atomic`` block; the bypass dies with it and cannot survive onto a pooled
    connection.
    """
    token = _rls_bypass.set(True)
    try:
        if connection.vendor == "postgresql":
            with transaction.atomic():
                bind_database_session(get_current_tenant_id(), bypass=True)
                try:
                    yield
                finally:
                    bind_database_session(get_current_tenant_id(), bypass=False)
        else:
            yield
    finally:
        _rls_bypass.reset(token)


@contextlib.contextmanager
def cross_tenant_lookup() -> Iterator[None]:
    """Permit a deliberately cross-tenant read for the duration of a block.

    There is exactly one question that cannot be answered inside a tenant
    scope: *"which tenants may this user access?"* Membership rows carry a
    ``tenant_id`` and are therefore RLS-protected, but the login and
    tenant-resolution paths must read them **before** any tenant is known.
    Without an escape hatch that is a deadlock — you cannot bind the tenant
    until you have read the row, and you cannot read the row until you have
    bound the tenant — and it presents as "this account is not a member of
    any organisation" for every user in the system.

    The escape hatch is narrow by construction:

    * It is a context manager, so the bypass is bounded by a block rather
      than left on for the rest of the connection's life.
    * ``SET LOCAL`` scopes it to the surrounding transaction, so it cannot
      survive onto the next request through a pooled connection.
    * Callers must still filter by ``user_id``. This function grants
      *visibility*, not authorisation — it is not a substitute for the
      membership check that follows it.

    Do not reach for this anywhere else. Any other cross-tenant read is
    either a reporting job (which should use ``tenant_context`` per tenant)
    or a bug.
    """
    from django.db import transaction

    with transaction.atomic():
        bind_database_session(get_current_tenant_id(), bypass=True)
        try:
            yield
        finally:
            # Restore the session to the caller's real scope. `SET LOCAL`
            # would expire at commit anyway, but an outer transaction may
            # continue for the rest of the request and must not inherit the
            # bypass.
            bind_database_session(get_current_tenant_id(), bypass=False)
