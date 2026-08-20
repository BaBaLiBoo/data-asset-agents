"""Alembic运行环境，从demo/.env读取真实数据库连接。"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from asset_supervisor.config import Settings
from asset_supervisor.db import metadata


config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

settings = Settings.from_env()
if not settings.database_url:
    raise RuntimeError("执行数据库迁移前必须配置 DATABASE_URL")
config.set_main_option(
    "sqlalchemy.url",
    settings.database_url.replace("%", "%%"),
)
target_metadata = metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
