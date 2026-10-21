import os
from datetime import timedelta

import dj_database_url
from django.core.exceptions import ImproperlyConfigured


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ImproperlyConfigured(f"environment variable {name} is not set")
    return value


SECRET_KEY = _required("DJANGO_SECRET_KEY")
DEBUG = os.environ.get("DJANGO_DEBUG", "0") == "1"
ALLOWED_HOSTS: list[str] = []

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "apps.wallets",
    "apps.betting",
    "apps.risk_integration",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
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
]

WSGI_APPLICATION = "config.wsgi.application"

# From the environment, per Deployment. The default is Django's own (0), so this shared module leaves the web pods'
# connection behaviour as their Deployment configures it. The integration process sets it (about 60 s): its worker
# threads are fixed, so persistent connections give exactly that many steady connections instead of one per unit
# of work, and 60 s also rotates them after a database failover.
DATABASES = {
    "default": dj_database_url.parse(
        _required("DATABASE_URL"),
        conn_max_age=int(os.environ.get("DATABASE_CONN_MAX_AGE", "0")),
        conn_health_checks=True,
    )
}
# Without it psycopg waits 130 s on an unreachable host while the worker thread holds a run_db permit. 2 s is the
# minimum psycopg honours and stays below the integration's await timeout, so a failed connect surfaces as a
# transient error rather than as an unknown outcome. A value in DATABASE_URL wins.
DATABASES["default"].setdefault("OPTIONS", {}).setdefault(
    "connect_timeout", int(os.environ.get("DATABASE_CONNECT_TIMEOUT", "2"))
)

# A Django setting, not an integration setting, because the monolith's web pods create the tickets.
RISK_TICKET_DEADLINE = timedelta(seconds=float(os.environ.get("RISK_TICKET_DEADLINE_SECONDS", "10")))

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
