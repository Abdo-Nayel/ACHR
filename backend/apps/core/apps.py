"""AppConfig for ``apps.core``.

Declared explicitly rather than left to Django's auto-discovery. Auto-discovery
infers the app *label* from the last component of the module path, so a package
rename silently changes ``app_label``, which changes every migration's
dependency graph and every ``"core.Model"`` string reference in the project.
An explicit ``name`` makes that a deliberate edit instead of an accident.
"""

from __future__ import annotations

from django.apps import AppConfig


class CoreConfig(AppConfig):
    """Abstract bases, money fields and the ambient tenant context.

    Contains no concrete models of its own. It is listed in ``INSTALLED_APPS``
    first, and declared explicitly here, because every other app imports its
    abstract bases at class-definition time: the registry must know about it
    before the apps that subclass ``TenantScopedModel`` are loaded.
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
    name = "apps.core"
    label = "core"
    verbose_name = "Core"
