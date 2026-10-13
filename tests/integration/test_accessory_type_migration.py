import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
import sqlalchemy as sa

from app.modules.materials.accessory_catalog import SEED_TYPES


_MIGRATION_PATH = (Path(__file__).resolve().parents[2] / "alembic" / "versions" /
                   "20260929_acc_type_catalog.py")
_SPEC = importlib.util.spec_from_file_location(
    "accessory_type_catalog_migration", _MIGRATION_PATH)
_MIGRATION = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MIGRATION)


def test_catalogue_migration_creates_and_seeds_idempotently():
    engine = sa.create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            context = MigrationContext.configure(connection)
            with Operations.context(context):
                _MIGRATION.upgrade()

            catalogue = sa.Table("accessory_type", sa.MetaData(), autoload_with=connection)
            expected_codes = {item["code"] for item in SEED_TYPES}
            actual_codes = set(connection.execute(
                sa.select(catalogue.c.code)).scalars())
            assert actual_codes == expected_codes

            with Operations.context(MigrationContext.configure(connection)):
                _MIGRATION.upgrade()

            actual_codes = set(connection.execute(
                sa.select(catalogue.c.code)).scalars())
            assert actual_codes == expected_codes
            assert "ix_accessory_type_code" in {
                index["name"] for index in sa.inspect(connection).get_indexes(
                    "accessory_type")
            }
    finally:
        engine.dispose()