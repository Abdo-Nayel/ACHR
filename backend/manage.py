#!/usr/bin/env python
"""Django's command-line utility.

Defaults to the development settings. Production deployments set
``DJANGO_SETTINGS_MODULE=config.settings.prod`` in the environment — the
default here is deliberately the *safe* one, so that forgetting the variable
gives you a local sandbox rather than a process pointed at production
credentials.
"""

from __future__ import annotations

import os
import sys


def main() -> None:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "Django could not be imported. Is the virtual environment active "
            "and are the requirements installed? (`make install`)"
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
