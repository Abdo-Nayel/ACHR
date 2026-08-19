"""Project configuration package.

Importing the Celery app here is what makes ``@shared_task`` bind to it: the
app must exist before any ``apps.*.tasks`` module is imported by Django's app
registry. Without this, tasks register against a default app with no broker
and silently never run.
"""

from __future__ import annotations

from config.celery import app as celery_app

__all__ = ("celery_app",)
