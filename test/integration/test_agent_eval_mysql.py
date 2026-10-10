"""Real MySQL gates for immutable Agent evaluation definitions and bindings."""

from __future__ import annotations

import os
import json

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
async def test_mysql_agent_eval_definitions_and_run_bindings_are_immutable():
    engine = create_async_engine(_database_url(), isolation_level="READ COMMITTED")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    suite_id, version_id, case_id = uuid7(), uuid7(), uuid7()
    eval_run_id, case_run_id = uuid7(), uuid7()

    async with factory.begin() as db:
        tables = set((await db.execute(text(
            "SELECT TABLE_NAME FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME IN ("
            "'agent_eval_suite','agent_eval_suite_version','agent_eval_case',"
            "'agent_eval_run','agent_eval_case_run')"
        ))).scalars())
        assert tables == {
            "agent_eval_suite", "agent_eval_suite_version", "agent_eval_case",
            "agent_eval_run", "agent_eval_case_run",
        }
        triggers = set((await db.execute(text(
            "SELECT TRIGGER_NAME FROM information_schema.TRIGGERS "
            "WHERE TRIGGER_SCHEMA=DATABASE() AND TRIGGER_NAME LIKE 'trg_agent_eval_%'"
        ))).scalars())
        assert triggers == {
            "trg_agent_eval_suite_version_immutable_update",
            "trg_agent_eval_suite_version_immutable_delete",
            "trg_agent_eval_case_immutable_update",
            "trg_agent_eval_case_immutable_delete",
            "trg_agent_eval_run_binding_immutable",
            "trg_agent_eval_case_run_binding_immutable",
        }
        await db.execute(text("SET FOREIGN_KEY_CHECKS=0"))
        await db.execute(text(
            "INSERT INTO agent_eval_suite (id,agent_id,name,status,current_version_id,"
            "next_version_no,created_by_account_id,created_at,updated_at,version) VALUES "
            "(:id,:agent,'gate','active',NULL,2,:account,NOW(),NOW(),1)"
        ), {"id": suite_id, "agent": uuid7(), "account": uuid7()})
        await db.execute(text(
            "INSERT INTO agent_eval_suite_version (id,suite_id,version_no,name,description,"
            "gate_policy,content_hash,created_by_account_id,published_at) VALUES "
            "(:id,:suite,1,'gate','',JSON_OBJECT('min_pass_rate','1'),:hash,:account,NOW())"
        ), {
            "id": version_id, "suite": suite_id, "hash": "a" * 64,
            "account": uuid7(),
        })
        await db.execute(text(
            "INSERT INTO agent_eval_case (id,suite_version_id,case_key,name,input_text,"
            "assertions,weight,sort_order,created_at) VALUES "
            "(:id,:version,'basic','Basic','work',JSON_OBJECT('min_chars',1),1,0,NOW())"
        ), {"id": case_id, "version": version_id})
        await db.execute(text(
            "INSERT INTO agent_eval_run (id,agent_id,suite_version_id,"
            "candidate_agent_version_id,candidate_content_hash,status,case_count,"
            "passed_count,failed_count,error_count,score,gate_policy_snapshot,started_at,"
            "ended_at,created_by_account_id,created_at,updated_at) VALUES "
            "(:id,:agent,:suite_version,:candidate,:hash,'queued',1,0,0,0,NULL,"
            "JSON_OBJECT('min_pass_rate','1'),NULL,NULL,:account,NOW(),NOW())"
        ), {
            "id": eval_run_id, "agent": uuid7(), "suite_version": version_id,
            "candidate": uuid7(), "hash": "b" * 64, "account": uuid7(),
        })
        await db.execute(text(
            "INSERT INTO agent_eval_case_run (id,eval_run_id,eval_case_id,session_id,run_id,"
            "status,score,response_message_id,assertion_results,failure_code,started_at,"
            "ended_at,created_at,updated_at) VALUES "
            "(:id,:eval_run,:case,:session,:run,'queued',NULL,NULL,JSON_ARRAY(),NULL,NULL,NULL,NOW(),NOW())"
        ), {
            "id": case_run_id, "eval_run": eval_run_id, "case": case_id,
            "session": uuid7(), "run": uuid7(),
        })
        await db.execute(text("SET FOREIGN_KEY_CHECKS=1"))

    async with factory() as db:
        with pytest.raises(DBAPIError, match="agent eval definition is immutable"):
            await db.execute(text(
                "UPDATE agent_eval_suite_version SET name='mutated' WHERE id=:id"
            ), {"id": version_id})
        await db.rollback()
    async with factory() as db:
        with pytest.raises(DBAPIError, match="agent eval definition is immutable"):
            await db.execute(text("DELETE FROM agent_eval_case WHERE id=:id"), {"id": case_id})
        await db.rollback()
    async with factory() as db:
        with pytest.raises(DBAPIError, match="agent eval run binding is immutable"):
            await db.execute(text(
                "UPDATE agent_eval_run SET candidate_content_hash=:hash WHERE id=:id"
            ), {"id": eval_run_id, "hash": "c" * 64})
        await db.rollback()
    async with factory.begin() as db:
        await db.execute(text("SET FOREIGN_KEY_CHECKS=0"))
        await db.execute(text(
            "UPDATE agent_eval_run SET status='running',started_at=NOW() WHERE id=:id"
        ), {"id": eval_run_id})
        await db.execute(text("SET FOREIGN_KEY_CHECKS=1"))
    async with factory() as db:
        with pytest.raises(DBAPIError, match="agent eval case run binding is immutable"):
            await db.execute(text(
                "UPDATE agent_eval_case_run SET run_id=:run WHERE id=:id"
            ), {"id": case_run_id, "run": uuid7()})
        await db.rollback()
    async with factory.begin() as db:
        await db.execute(text("SET FOREIGN_KEY_CHECKS=0"))
        await db.execute(text(
            "UPDATE agent_eval_case_run SET status='running',started_at=NOW() WHERE id=:id"
        ), {"id": case_run_id})
        await db.execute(text("SET FOREIGN_KEY_CHECKS=1"))

    await engine.dispose()


@pytest.mark.asyncio
async def test_mysql_eval_case_metadata_is_frozen_with_the_definition():
    engine = create_async_engine(_database_url(), isolation_level="READ COMMITTED")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    case_id = uuid7()
    metadata = {"tags": ["搜索"], "source_run_id": uuid7(), "source_snapshot": {
        "content_hash": "a" * 64, "response_text": "historical reference, not gold",
    }}
    async with factory.begin() as db:
        await db.execute(text("SET FOREIGN_KEY_CHECKS=0"))
        try:
            await db.execute(text(
                "INSERT INTO agent_eval_case (id,suite_version_id,case_key,name,input_text,"
                "assertions,weight,sort_order,created_at,case_metadata) VALUES "
                "(:id,:version,'source','Source','question',JSON_OBJECT('min_chars',1),"
                "1,0,NOW(),:metadata)"
            ), {"id": case_id, "version": uuid7(), "metadata": json.dumps(metadata)})
        finally:
            await db.execute(text("SET FOREIGN_KEY_CHECKS=1"))
    async with factory() as db:
        stored = await db.scalar(text("SELECT case_metadata FROM agent_eval_case WHERE id=:id"), {"id": case_id})
        assert json.loads(stored) == metadata
        with pytest.raises(DBAPIError, match="agent eval definition is immutable"):
            await db.execute(text("UPDATE agent_eval_case SET case_metadata=JSON_OBJECT() WHERE id=:id"), {"id": case_id})
        await db.rollback()
    await engine.dispose()
