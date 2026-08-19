"""Reporting services: the write side of the reporting module.

Generators are pure and read-only; anything that persists, schedules or
compares lives here, so that re-running a report can never change anything.
"""

from __future__ import annotations
