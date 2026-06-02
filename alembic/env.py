from logging.config import fileConfig
from sqlalchemy import engine_from_config, pool
from alembic import context
import os
import sys
from pathlib import Path

# Add the backend directory to sys.path so alembic can import app modules
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.core.config import get_settings
from app.core.database import Base
# Import every module's models so autogenerate sees all tables.
from app.modules.users import models as _u    # noqa
from app.modules.clients import models as _c   # noqa
from app.modules.employees import models as _e  # noqa
from app.modules.production import models as _p  # noqa
from app.modules.wages import models as _w       # noqa

config = context.config
db_url = get_settings().database_url
# Escape % characters in the URL for ConfigParser (it treats % as interpolation)
db_url = db_url.replace("%", "%%")
config.set_main_option("sqlalchemy.url", db_url)
if config.config_file_name:
    fileConfig(config.config_file_name)
target_metadata = Base.metadata


def run_migrations_offline():
    context.configure(url=get_settings().database_url, target_metadata=target_metadata,
                      literal_binds=True, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
