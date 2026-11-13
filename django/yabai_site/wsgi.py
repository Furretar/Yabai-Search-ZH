"""WSGI entry point: `gunicorn yabai_site.wsgi -k gthread --threads 8`."""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "yabai_site.settings")

application = get_wsgi_application()

# Build the SQL and segmenter caches now, not during the first search. Set
# YABAI_WARM_ON_START=0 to skip it (manage.py commands never import this file).
if os.environ.get("YABAI_WARM_ON_START", "1") != "0":
    from yabai_search import services

    services.warm_caches()
