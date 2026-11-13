"""ASGI entry point, kept for completeness.

Prefer gunicorn + WSGI (see wsgi.py). Under an ASGI server Django runs these
synchronous views one at a time in a single worker thread, which would serialize
every search.
"""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "yabai_site.settings")

application = get_asgi_application()
