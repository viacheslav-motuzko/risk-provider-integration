from django.db import connection
from django.db.migrations.executor import MigrationExecutor


def pending_migrations() -> list[str]:
    """Migrations not yet applied, for the startup check. The release pipeline migrates; this process never does."""
    executor = MigrationExecutor(connection)
    plan = executor.migration_plan(executor.loader.graph.leaf_nodes())
    return [f"{migration.app_label}.{migration.name}" for migration, _ in plan]
