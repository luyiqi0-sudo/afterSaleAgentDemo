"""The delivered root database is usable and cannot drift from its frozen reference."""
from contextlib import closing
import hashlib
from pathlib import Path
import shutil
import sqlite3

import httpx
import pytest

from agent.config import Settings
from api.main import assemble
from eval.controlled import ControlledModel
from eval.eval import evaluate_cases
from eval.runner import load_cases, run_case
from knowledge.ingest import ingest_knowledge


PROJECT = Path(__file__).resolve().parents[1]
DATASET = PROJECT / "data/c004-r01/baseline"
DATABASE = PROJECT / "database"


def delivery_settings(directory, runtime):
    return Settings(
        _env_file=None, dataset_root=DATASET,
        database_path=directory / "ecommerce.db", dictionary_path=directory / "dictionary.json",
        knowledge_path=PROJECT / "knowledge", runtime_dir=runtime,
        business_service_token="synthetic-database-delivery-token",
        vector_backend="memory", embedding_model="controlled-topics-v1", embedding_dimension=16,
    )


def test_root_database_contains_schema_dictionary_and_populated_seed():
    assert all((DATABASE / name).is_file() for name in ("schema.sql", "dictionary.json", "ecommerce.db"))
    with closing(sqlite3.connect((DATABASE / "ecommerce.db").as_uri() + "?mode=ro", uri=True)) as con:
        for table, expected in (("orders", 200), ("logistics", 125), ("products", 24)):
            assert con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == expected
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            con.execute("DELETE FROM orders")


async def test_root_database_runs_product_sql_via_actual_agent_http(tmp_path):
    case = next(case for case in load_cases(DATASET) if case["id"] == "GOLD-16")
    settings = delivery_settings(DATABASE, tmp_path / "runtime")
    model = ControlledModel(case)
    before = hashlib.sha256(settings.database_path.read_bytes()).hexdigest()
    result = await run_case(DATASET, case, "controlled", settings, model,
                            await ingest_knowledge(settings, model), tmp_path / "case")
    report = evaluate_cases(DATASET, {case["id"]: result}, contract="mvp-agent-v2")
    assert report["summary"] == {
        "passed": 1, "failed": 0, "not_run": 35, "executed": 1,
        "pass_rate": 1.0, "coverage": 1 / 36,
    }
    assert before == hashlib.sha256(settings.database_path.read_bytes()).hexdigest()


@pytest.mark.parametrize("corruption", ["missing_db", "changed_db", "changed_dictionary", "changed_schema", "wal"])
async def test_relocated_database_is_rejected_before_provider_or_runtime_write(tmp_path, corruption):
    directory = tmp_path / "database"
    shutil.copytree(DATASET / "database", directory)
    if corruption == "missing_db":
        (directory / "ecommerce.db").unlink()
    elif corruption == "changed_db":
        with closing(sqlite3.connect(directory / "ecommerce.db")) as con, con:
            con.execute("UPDATE orders SET amount = amount + 1 WHERE order_id='EVAL-002'")
    elif corruption == "wal":
        (directory / "ecommerce.db-wal").write_bytes(b"unexpected SQLite sidecar")
    else:
        name = "dictionary.json" if corruption == "changed_dictionary" else "schema.sql"
        with (directory / name).open("ab") as output:
            output.write(b"\n ")

    def unexpected_request(request):
        pytest.fail("A changed seed must be rejected before any external request")

    runtime = tmp_path / "runtime"
    async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected_request)) as business:
        with pytest.raises(ValueError, match="^DATABASE_SNAPSHOT_MISMATCH$"):
            await assemble(delivery_settings(directory, runtime), business, lambda *_: None, model=object())
    assert not runtime.exists()


async def test_runtime_must_not_write_into_delivered_database_directory(tmp_path):
    directory = tmp_path / "database"
    shutil.copytree(DATASET / "database", directory)
    async with httpx.AsyncClient() as business:
        with pytest.raises(ValueError, match="^RUNTIME_MUST_BE_PRIVATE$"):
            await assemble(delivery_settings(directory, directory / "runtime"), business,
                           lambda *_: None, model=object())
    assert not (directory / "runtime").exists()
