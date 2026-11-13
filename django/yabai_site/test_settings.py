"""Settings for the test suite: the real settings plus a throwaway secret key.

The real settings refuse to start without DJANGO_SECRET_KEY outside debug mode, which is
right for the laptop and tablet but means a plain `uv run pytest` failed before any test
ran. The key below only signs test sessions; a real key in the environment still wins.
"""

import os

os.environ.setdefault("DJANGO_SECRET_KEY", "test-only-key-not-for-production")

from .settings import *  # noqa: E402,F401,F403  (re-export every real setting)
