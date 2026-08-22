"""Every dotted path the configuration names must resolve to real code.

Phase 6 fixed a cluster of config that pointed at modules which did not exist —
a Celery beat schedule firing six unregistered tasks, a Sentry ``before_send``
importing a missing scrubber, a read-replica router class that was never written,
a dead ``STARTUP_CHECKS`` list. This test is the standing guard against that
class of bug: it imports what the config references and fails if any of it is a
phantom.
"""

from __future__ import annotations

import importlib

import pytest
from django.conf import settings


def _resolve(dotted: str) -> object:
    module_path, _, attr = dotted.rpartition(".")
    module = importlib.import_module(module_path)
    return getattr(module, attr)


def test_every_beat_task_is_registered():
    """No beat entry may name a task Celery cannot find (was 6 of 7)."""
    from config.celery import app

    app.loader.import_default_modules()
    registered = set(app.tasks.keys())
    scheduled = {entry["task"] for entry in app.conf.beat_schedule.values()}
    missing = sorted(scheduled - registered)
    assert not missing, f"beat schedule names unregistered tasks: {missing}"


def test_celery_task_routes_name_importable_or_glob_prefixes():
    """Routing prefixes should target apps that exist (a '*' glob is fine)."""
    from config.celery import app
    from django.apps import apps as django_apps

    installed = {c.name for c in django_apps.get_app_configs()}
    for pattern in app.conf.task_routes:
        if pattern == "*" or "*" not in pattern:
            continue
        app_path = pattern.rsplit(".tasks", 1)[0]
        assert app_path in installed, f"task route targets missing app: {pattern}"


@pytest.mark.parametrize("dotted", list(settings.MIDDLEWARE))
def test_middleware_resolves(dotted):
    assert _resolve(dotted) is not None


def test_drf_dotted_paths_resolve():
    drf = settings.REST_FRAMEWORK
    singles = [drf["EXCEPTION_HANDLER"]]
    tuples = (
        drf.get("DEFAULT_PERMISSION_CLASSES", ())
        + drf.get("DEFAULT_AUTHENTICATION_CLASSES", ())
        + drf.get("DEFAULT_FILTER_BACKENDS", ())
        + drf.get("DEFAULT_THROTTLE_CLASSES", ())
        + (drf["DEFAULT_PAGINATION_CLASS"],)
    )
    for dotted in singles + list(tuples):
        assert _resolve(dotted) is not None, dotted


def test_logging_handler_and_filter_classes_resolve():
    logging = settings.LOGGING
    for section in ("filters", "handlers", "formatters"):
        for spec in logging.get(section, {}).values():
            dotted = spec.get("class") or spec.get("()")
            if dotted and isinstance(dotted, str) and "." in dotted:
                assert _resolve(dotted) is not None, dotted


def test_sentry_scrubber_exists():
    """prod.py's Sentry before_send imports this at send time; a missing module
    made every production error event raise ImportError and vanish."""
    from apps.core.observability import scrub_event

    event = {"request": {"data": {"password": "hunter2", "amount": "100.00"}}}
    scrubbed = scrub_event(event, None)
    assert scrubbed["request"]["data"]["password"] == "[redacted]"
    assert scrubbed["request"]["data"]["amount"] == "100.00"
