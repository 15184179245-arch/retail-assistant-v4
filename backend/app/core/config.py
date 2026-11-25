import os


def database_url() -> str:
    """Require an explicit database target; never silently create a local DB."""
    value = os.environ.get("DATABASE_URL")
    if not value:
        raise RuntimeError("DATABASE_URL must be explicitly configured before database access or migration")
    return value
