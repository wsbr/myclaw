"""Real MySQL gates for the append-only usage and cost ledger."""

from __future__ import annotations

import os

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.common import uuid7


pytestmark = pytest.mark.integration


def _database_url() -> str:
    value = os.getenv("MYCLAW_V3_ACCEPTANCE_DATABASE_URL")
    if not value:
        pytest.skip("MYCLAW_V3_ACCEPTANCE_DATABASE_URL selects the real migrated MySQL database")
    return value


@pytest.mark.asyncio
async def test_mysql_usage_ledger_schema_and_immutability_triggers():
    engine = create_async_engine(_database_url(), isolation_level="READ COMMITTED")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    price_id = uuid7()
    entry_id = uuid7()
    tenant_id = uuid7()

    async with factory.begin() as db:
        tables = set((await db.execute(text(
            "SELECT TABLE_NAME FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME IN "
            "('usage_price_snapshot','usage_ledger_entry')"
        ))).scalars())
        assert tables == {"usage_price_snapshot", "usage_ledger_entry"}
        triggers = set((await db.execute(text(
            "SELECT TRIGGER_NAME FROM information_schema.TRIGGERS "
            "WHERE TRIGGER_SCHEMA=DATABASE() AND TRIGGER_NAME IN ("
            "'trg_usage_price_snapshot_immutable_update',"
            "'trg_usage_price_snapshot_immutable_delete',"
            "'trg_usage_ledger_entry_immutable_update',"
            "'trg_usage_ledger_entry_immutable_delete')"
        ))).scalars())
        assert triggers == {
            "trg_usage_price_snapshot_immutable_update",
            "trg_usage_price_snapshot_immutable_delete",
            "trg_usage_ledger_entry_immutable_update",
            "trg_usage_ledger_entry_immutable_delete",
        }

        # The database is disposable. Disable foreign-key checks only while
        # inserting minimal facts so this gate can focus on MySQL trigger
        # behavior without constructing an entire runtime graph.
        await db.execute(text("SET FOREIGN_KEY_CHECKS=0"))
        await db.execute(text(
            "INSERT INTO usage_price_snapshot ("
            "id,subject_type,provider,subject_key,metric,unit,currency,"
            "price_per_unit,price_status,effective_from,effective_to,"
            "source_label,content_hash,created_by_account_id,created_at"
            ") VALUES ("
            ":id,'model','integration','model','requests','request','USD',"
            "1.000000000000,'contracted',NOW(),NULL,'integration',:hash,:account,NOW())"
        ), {
            "id": price_id,
            "hash": uuid7().replace("-", "").ljust(64, "0"),
            "account": uuid7(),
        })
        await db.execute(text(
            "INSERT INTO usage_ledger_entry ("
            "id,tenant_id,account_id,session_id,run_id,source_type,source_id,"
            "source_operation_key,metric,unit,quantity,measurement_status,"
            "price_snapshot_id,currency,unit_price_snapshot,cost_amount,"
            "cost_status,safe_metadata,created_at"
            ") VALUES ("
            ":id,:tenant,:account,:session,:run,'adjustment',:source,:operation,"
            "'requests','request',-1.000000000,'measured',:price,'USD',"
            "1.000000000000,-1.000000000000,'contracted',JSON_OBJECT(),NOW())"
        ), {
            "id": entry_id,
            "tenant": tenant_id,
            "account": uuid7(),
            "session": uuid7(),
            "run": uuid7(),
            "source": uuid7(),
            "operation": f"integration:{uuid7()}",
            "price": price_id,
        })
        await db.execute(text("SET FOREIGN_KEY_CHECKS=1"))

    for statement in (
        "UPDATE usage_price_snapshot SET source_label='mutated' WHERE id=:id",
        "DELETE FROM usage_price_snapshot WHERE id=:id",
    ):
        async with factory() as db:
            with pytest.raises(DBAPIError, match="usage ledger fact is append-only"):
                await db.execute(text(statement), {"id": price_id})
            await db.rollback()

    for statement in (
        "UPDATE usage_ledger_entry SET quantity=2 WHERE id=:id",
        "DELETE FROM usage_ledger_entry WHERE id=:id",
    ):
        async with factory() as db:
            with pytest.raises(DBAPIError, match="usage ledger fact is append-only"):
                await db.execute(text(statement), {"id": entry_id})
            await db.rollback()

    await engine.dispose()
