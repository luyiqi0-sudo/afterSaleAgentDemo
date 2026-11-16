"""Exercise local fault scripts against real tool guards/ledger and generated SQLite.

The HTTP transport is a candidate-authored substitute, NOT the required formal Mock.
"""

import httpx
import pytest

from agent.contracts import Principal
from agent.guards import SafetyError, check_input
from agent.operations import OperationStore
from agent.sql import SQLService
from agent.tools import BusinessTools
from database.contracts import jsonl_rows
from database.seed import generate_dataset


@pytest.fixture
def seeded(tmp_path):
    root = tmp_path / "dataset"
    generate_dataset(root)
    return root


@pytest.mark.parametrize("fixture_id", ["GET-429", "GET-500", "GET-TIMEOUT", "POST-LOST"])
async def test_fault_script_with_generated_business_data(seeded, tmp_path, fixture_id):
    script = next(r for r in jsonl_rows(seeded / "fixtures/faults.jsonl") if r["id"] == fixture_id)
    orders = {o["order_id"]: o for o in jsonl_rows(seeded / "orders.jsonl")}
    attempts = []

    def server(request):
        # Fail loudly if the implementation retries beyond the scenario's allowed sequence.
        response = script["responses"][len(attempts)]
        attempts.append(request.method)
        if response.get("exception") == "ReadTimeout":
            raise httpx.ReadTimeout("scripted response loss", request=request)
        return httpx.Response(
            response["status"],
            headers={"Retry-After": response.get("retry_after", "0")},
            json=orders.get(response.get("body_ref"), {}),
        )

    sql = SQLService(seeded / "database/ecommerce.db")
    ledger = tmp_path / "operations.db"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(server), base_url="http://local-test"
    ) as client:
        tools = BusinessTools(sql, OperationStore(ledger), client, backoff=0)
        principal = Principal(user_id="acc-alice")
        if script["method"] == "POST":
            result = await tools.create_ticket(
                "ACC-007", "退款失败核查", "confirmed-operation", principal
            )
            # Simulate restart: a fresh store instance sees the prior durable UNKNOWN.
            tools = BusinessTools(sql, OperationStore(ledger), client, backoff=0)
            duplicate = await tools.create_ticket(
                "ACC-007", "退款失败核查", "confirmed-operation", principal
            )
            assert duplicate.status == "UNKNOWN"
            assert duplicate.operation_id == "confirmed-operation"
        else:
            result = await tools.query_order("ACC-002", principal)
        assert result.status == script["expected"]["status"]
        assert len(attempts) == script["expected"]["max_attempts"]
        if result.status == "SUCCESS":
            assert result.data == {
                "order_id": "ACC-002",
                "amount": 30100,
                "status": 2,
                "create_time": "2026-10-31 23:59:59",
            }


async def test_seeded_cross_user_case_is_blocked_before_http(seeded, tmp_path):
    case = next(r for r in jsonl_rows(seeded / "cases/acceptance.jsonl") if r["id"] == "A05")

    def server(request):
        pytest.fail("foreign order must not reach the HTTP service")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(server), base_url="http://local-test"
    ) as client:
        tools = BusinessTools(
            SQLService(seeded / "database/ecommerce.db"),
            OperationStore(tmp_path / "ops.db"),
            client,
        )
        result = await tools.query_order(
            case["data_refs"]["order_ids"][0], Principal(user_id=case["principal_id"])
        )
    assert result.error_code == "order_unavailable"
    assert result.data is None
    assert result.attempts == 0


def test_seeded_dangerous_user_input_is_rejected(seeded):
    case = next(r for r in jsonl_rows(seeded / "cases/acceptance.jsonl") if r["id"] == "A09")
    with pytest.raises(SafetyError):
        check_input(case["question"])
