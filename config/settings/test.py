import os

# Test-only defaults for the local compose database, so tests and mypy run without a .env; CI sets real values.
os.environ.setdefault("DJANGO_SECRET_KEY", "test-only")
os.environ.setdefault("DATABASE_URL", "postgres://risk:risk@localhost:5432/risk")

from config.settings.base import *  # noqa: F403
