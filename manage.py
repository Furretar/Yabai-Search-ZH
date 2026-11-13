#!/usr/bin/env python
"""Django's command-line entry point (migrate, createsuperuser, test, ...)."""

import os
import sys
from pathlib import Path


def main() -> None:
    # The Django project and app live in ./django (a plain folder, deliberately without __init__.py so it can
    # never shadow the real django package); put it on the import path so `yabai_site` and `dictionary` import.
    sys.path.insert(0, str(Path(__file__).resolve().parent / "django"))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "yabai_site.settings")
    from django.core.management import execute_from_command_line

    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
