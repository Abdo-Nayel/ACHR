"""AppConfig for ``apps.reporting``.

Declared explicitly rather than left to Django's auto-discovery. Auto-discovery
infers the app *label* from the last component of the module path, so a package
rename silently changes ``app_label``, which changes every migration's
dependency graph and every ``"reporting.Model"`` string reference in the project.
An explicit ``name`` makes that a deliberate edit instead of an accident.
"""

from __future__ import annotations

from django.apps import AppConfig


class ReportingConfig(AppConfig):
    """The financial reporting engine.

    Owns saved report definitions, delivery schedules, the data-driven
    statement layout, and — most importantly — ``ReportSnapshot``, the frozen
    evidence of what was reported and when.
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
    name = "apps.reporting"
    label = "reporting"
    verbose_name = "Reporting"

    def ready(self) -> None:
        """Import the generators so ``@register_report`` has run.

        Without this, the registry is populated only if something happened to
        import ``apps.reporting.generators`` first. The symptom is a
        ``ReportError("No report generator registered for 'balance_sheet'")``
        raised inside a Celery task at 03:00, whose traceback contains no hint
        that the cause is a module that was never imported. Doing it in
        ``ready()`` makes the registry complete after ``django.setup()``,
        which is the point at which every entry point — web, worker, shell,
        management command, test — has finished loading.

        Imported inside the method, not at module level: ``ready()`` is the
        first moment the app registry is populated, and importing models any
        earlier raises ``AppRegistryNotReady``.
        """
        from apps.reporting import generators  # noqa: F401  (registers reports)
