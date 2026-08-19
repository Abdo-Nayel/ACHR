"""Backend test suite.

A package (rather than a bare directory) so that ``tests.factories`` and
future sub-packages import by a stable dotted path under ``pytest-xdist``,
which imports test modules by their package name and would otherwise put two
same-named modules in different directories into conflict.
"""

from __future__ import annotations
