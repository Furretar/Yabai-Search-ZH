"""Django settings for Yabai Search.

Everything that differs between the laptop, the tablet and a dev checkout is
read from environment variables (deploy/env.local feeds them in, see
deploy/run.sh). Defaults are the safe choice: DEBUG off, no built-in secret key.
"""

from __future__ import annotations

import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_list(name: str, default: list[str]) -> list[str]:
    value = os.environ.get(name)
    if value is None:
        return default
    return [item.strip() for item in value.split(",") if item.strip()]


DEBUG = _env_bool("DJANGO_DEBUG", False)

# The key signs sessions and CSRF tokens. Both machines must use the SAME key,
# or a login made on the laptop is rejected by the tablet. Generate one with:
#   python -c "from django.core.management.utils import get_random_secret_key as g; print(g())"
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "")
if not SECRET_KEY:
    if DEBUG:
        SECRET_KEY = "insecure-dev-only-key"
    else:
        raise ImproperlyConfigured(
            "Set DJANGO_SECRET_KEY (the same value on the laptop and the tablet)."
        )

# Host names the Cloudflare Worker and the tunnels use (see deploy/cloudflared).
ALLOWED_HOSTS = _env_list(
    "DJANGO_ALLOWED_HOSTS",
    [
        "yabaisearch.org",
        "www.yabaisearch.org",
        "laptop.yabaisearch.org",
        "tablet.yabaisearch.org",
        "pc.yabaisearch.org",
        ".yabaisearch.org",  # any <host>.yabaisearch.org, so a new host needs no code change
        "dev.yabaisearch.org",
        "new.yabaisearch.org",
        "localhost",
        "127.0.0.1",
        "[::1]",
    ],
)
# The Worker forwards the browser's Origin (https://yabaisearch.org) to an origin
# whose Host is laptop.* or tablet.*; Django would reject every POST without this.
CSRF_TRUSTED_ORIGINS = _env_list(
    "DJANGO_CSRF_TRUSTED_ORIGINS",
    ["https://yabaisearch.org", "https://www.yabaisearch.org"],
)

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "accounts",
    "dictionary",
]

# Order matters: a middleware listed earlier wraps the ones after it, so
# CacheHeaders sees responses that WhiteNoise answers directly.
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "yabai_site.middleware.OriginSecretMiddleware",
    "yabai_site.middleware.WriterGateMiddleware",
    "yabai_site.middleware.RequestStampMiddleware",
    "yabai_site.middleware.CacheHeadersMiddleware",
    "django.middleware.gzip.GZipMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "yabai_site.urls"
WSGI_APPLICATION = "yabai_site.wsgi.application"
APPEND_SLASH = False  # API paths such as /api/search must not redirect

TEMPLATES = [
    {
        # Admin and the accounts pages use Django's own template language.
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
    {
        # The dictionary pages keep their original Jinja2 templates unchanged.
        "BACKEND": "django.template.backends.jinja2.Jinja2",
        "DIRS": [BASE_DIR / "yabai_search" / "templates"],
        "APP_DIRS": False,
        "OPTIONS": {"autoescape": True},
    },
]

# --- accounts database -------------------------------------------------------
# The dictionary data stays in its own read-only SQLite files (yabai_search/db.py).
# This database only holds users, sessions and per-user settings. SQLite is fine
# for one machine. With several hosts, only the global writer changes the file and
# accounts/replication.py copies it to the others (deploy/GLOBAL-WRITER.md); set
# YABAI_REPL_NODE and friends in deploy/env.local. A shared hosted Postgres
# (DJANGO_DB_ENGINE=postgres) is the alternative.
if os.environ.get("DJANGO_DB_ENGINE", "sqlite") == "postgres":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ["DJANGO_DB_NAME"],
            "USER": os.environ["DJANGO_DB_USER"],
            "PASSWORD": os.environ["DJANGO_DB_PASSWORD"],
            "HOST": os.environ["DJANGO_DB_HOST"],
            "PORT": os.environ.get("DJANGO_DB_PORT", "5432"),
            "CONN_MAX_AGE": 60,
            "OPTIONS": {"sslmode": os.environ.get("DJANGO_DB_SSLMODE", "require")},
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": os.environ.get("DJANGO_DB_PATH", str(BASE_DIR / "data" / "accounts.sqlite3")),
            # Wait (seconds) for the lock instead of failing: a replication install
            # takes an exclusive lock for a moment.
            "OPTIONS": {"timeout": 20},
        }
    }
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- authentication ----------------------------------------------------------
# Argon2 is used when argon2-cffi is installed (it is in pyproject.toml); the
# PBKDF2 entry keeps older hashes working. Termux may not have a prebuilt
# argon2 wheel, so fall back to PBKDF2 there instead of failing at login.
try:
    import argon2  # noqa: F401

    PASSWORD_HASHERS = [
        "django.contrib.auth.hashers.Argon2PasswordHasher",
        "django.contrib.auth.hashers.PBKDF2PasswordHasher",
    ]
except ImportError:
    PASSWORD_HASHERS = [
        "django.contrib.auth.hashers.PBKDF2PasswordHasher",
    ]
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
# On a multi-host install the first backend checks passwords without ever writing
# (a replica must not save an upgraded hash); ModelBackend stays listed because
# accounts/views.py names it when it logs someone in directly.
if os.environ.get("YABAI_REPL_NODE"):
    AUTHENTICATION_BACKENDS = [
        "accounts.backends.ReadOnlyModelBackend",
        "django.contrib.auth.backends.ModelBackend",
    ]
LOGIN_URL = "/accounts/login/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/"
# Set YABAI_SIGNUPS_OPEN=0 to close registration without removing the page.
SIGNUPS_OPEN = _env_bool("YABAI_SIGNUPS_OPEN", True)

# Sessions live in the signed cookie, so a login works on EVERY host and logging in
# never writes to the database (only the global writer may write). The cookie is
# signed, not encrypted: never put secrets in the session. This is the default when
# YABAI_REPL_NODE is set; DJANGO_SESSIONS=db or =cookies overrides it.
if os.environ.get(
    "DJANGO_SESSIONS", "cookies" if os.environ.get("YABAI_REPL_NODE") else "db"
) == "cookies":
    SESSION_ENGINE = "django.contrib.sessions.backends.signed_cookies"
SESSION_COOKIE_AGE = 60 * 60 * 24 * 30
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"

# --- transport / headers -----------------------------------------------------
# Cloudflare terminates TLS and tells the origin through this header. Redirecting
# to HTTPS is left to Cloudflare ("Always Use HTTPS"), so no SSL redirect here.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_HSTS_SECONDS = int(os.environ.get("DJANGO_HSTS_SECONDS", "0"))
SECURE_HSTS_INCLUDE_SUBDOMAINS = SECURE_HSTS_SECONDS > 0
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"

# --- static files ------------------------------------------------------------
STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "yabai_search" / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"
# Serve straight from yabai_search/static without a collectstatic step, like the
# old FastAPI mount did. Set YABAI_STATIC_AUTOREFRESH=1 to see edits without a
# restart (slower; meant for development).
WHITENOISE_USE_FINDERS = True
WHITENOISE_AUTOREFRESH = _env_bool("YABAI_STATIC_AUTOREFRESH", DEBUG)

LANGUAGE_CODE = "en"
TIME_ZONE = "UTC"
USE_I18N = False
USE_TZ = True
ADMIN_URL_PATH = os.environ.get("DJANGO_ADMIN_PATH", "admin/")
