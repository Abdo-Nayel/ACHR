"""AppConfig for ``apps.accounting``.

Declared explicitly rather than left to Django's auto-discovery. Auto-discovery
infers the app *label* from the last component of the module path, so a package
rename silently changes ``app_label``, which changes every migration's
dependency graph and every ``"accounting.Model"`` string reference in the project.
An explicit ``name`` makes that a deliberate edit instead of an accident.
"""

from __future__ import annotations

from django.apps import AppConfig


class AccountingConfig(AppConfig):
    """The general ledger: chart of accounts, fiscal calendar, journals.

    The single source of financial truth. Every other business app expresses
    its financial effect as a journal entry posted through
    ``apps.accounting.services.posting.post_entry``, so this app must be in
    the registry before any of them resolve their ``"accounting.Account"``
    string references.
    """

    # Every business model in this project declares an explicit UUID primary
    # key (``apps.core.models.UUIDModel``), so this setting is almost never
    # consulted. It is pinned to ``BigAutoField`` anyway, and deliberately not
    # to ``UUIDField``: ``DEFAULT_AUTO_FIELD`` is only used for models that do
    # *not* declare a ``primary_key``, which here means pure join tables and
    # anything a third-party mixin adds. Those want a cheap sequential key,
    # not a random 16-byte one that fragments their index. Leaving it unset
    # would emit a warning per app on every ``manage.py`` invocation and,
    # worse, would silently change the column type of any future implicit PK
    # if Django's default changes in a later release.
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.accounting"
    label = "accounting"
    verbose_name = "Accounting"
