"""Narrow fresh-install compatibility for legacy numeric boolean defaults.

Historical SQLite-oriented revisions are immutable. On PostgreSQL migrations
only, render literal Boolean defaults 0/1 as false/true. Never mutate Columns,
change non-Boolean expressions or rewrite already installed database objects.
"""
from sqlalchemy import Boolean


def configure_migration_dialect(dialect):
    if dialect.name != 'postgresql' or getattr(dialect,'_sx_boolean_defaults',False):
        return
    base=dialect.ddl_compiler
    class BooleanDefaultCompiler(base):
        def get_column_default_string(self,column):
            default=column.server_default
            if isinstance(column.type,Boolean) and default is not None:
                value=str(default.arg).strip()
                if value in ('0','1'):
                    return 'true' if value=='1' else 'false'
            return super().get_column_default_string(column)
    dialect.ddl_compiler=BooleanDefaultCompiler
    dialect._sx_boolean_defaults=True
