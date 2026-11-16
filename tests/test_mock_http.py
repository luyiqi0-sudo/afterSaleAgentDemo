import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from datetime import date
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "data/c004-r01/baseline"
DATASET_ID = "8eb383698f2ac6a5baed126126e40c8a41e176d2a2e177beb4969b1a3955c26b"
TEST_TOKEN = "local-task1-test-token"
BASE_META = {
    "api_version": "after-sales-mock-v1",
    "dataset_id": DATASET_ID,
    "data_contract_version": "C-004-v1",
    "snapshot_as_of": "2026-11-15T23:59:59+08:00",
}
ABSENT = "not-a-fixture-order"


def query(sql, values=()):
    with closing(sqlite3.connect((DATASET/"database/ecommerce.db").as_uri() + "?mode=ro", uri=True)) as con:
        con.row_factory = sqlite3.Row
        return [dict(row) for row in con.execute(sql, values)]


@pytest.fixture
def order():
    return query("SELECT * FROM orders WHERE order_id='ACC-011'")[0]


@pytest.fixture(scope="module")
def formal_freeze():
    from mock.verify_freeze import verify_freeze

    return verify_freeze(ROOT / "mock/freeze.json")


def headers(actor):
    return {"Authorization": f"Bearer {TEST_TOKEN}", "X-Actor-ID": actor}


@pytest.fixture
def launch(tmp_path, formal_freeze):
    processes = []

    def start(*, faults=None, dataset=DATASET, token=TEST_TOKEN):
        with closing(socket.socket()) as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        env = dict(os.environ, DATASET_ROOT=str(dataset), MOCK_TICKET_DB=str(tmp_path / "tickets.sqlite"))
        env.pop("MOCK_FAULTS", None)
        env.pop("BUSINESS_API_TOKEN", None)
        if token is not None:
            env["BUSINESS_API_TOKEN"] = token
        if faults:
            env["MOCK_FAULTS"] = str(ROOT / "mock/faults" / faults)
        name = f"server-{len(processes)}.log"
        with (tmp_path / name).open("w+", encoding="utf-8") as log:
            proc = subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "mock_server:app", "--host", "127.0.0.1", "--port", str(port)],
                cwd=ROOT, env=env, stdout=log, stderr=log,
            )
        processes.append(proc)
        return proc, f"http://127.0.0.1:{port}", tmp_path / name

    yield start
    for proc in processes:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


def ready(launch, expected_dataset_id=DATASET_ID, **kwargs):
    proc, url, log = launch(**kwargs)
    deadline = time.monotonic() + 10
    with httpx.Client(base_url=url, trust_env=False, timeout=1) as client:
        while time.monotonic() < deadline and proc.poll() is None:
            try:
                response = client.get("/health")
                if response.status_code == 200:
                    assert response.json() == {
                        "status": "ok", "api_version": "after-sales-mock-v1", "dataset_id": expected_dataset_id,
                    }
                    return url
            except httpx.TransportError:
                pass
            time.sleep(0.05)
    pytest.fail("uvicorn did not become ready:\n" + log.read_text(encoding="utf-8", errors="replace"))


def test_snapshot_rejects_inconsistent_remote_facts_as_independent_component(order):
    from agent.snapshot import SnapshotMismatch, SnapshotVerifier

    verifier = SnapshotVerifier(DATASET)
    assert verifier.metadata == BASE_META
    assert verifier.dataset_id == DATASET_ID
    body = dict(order, _meta=dict(BASE_META, observed_at="2026-11-16T10:00:00+08:00"))
    assert verifier.validate_order(body, order["order_id"], order["user_id"]) == body
    for corrupt in [
        dict(body, amount=False), dict(body, status=0), dict(body, amount=1), dict(body, user_id="other"),
        dict(body, order_id="other"), dict(body, _meta=dict(body["_meta"], unexpected=1)),
        dict(body, _meta=dict(body["_meta"], dataset_id="another")), dict(body, _meta=BASE_META),
        dict(body, _meta=dict(body["_meta"], observed_at="2026-11-16")),
    ]:
        with pytest.raises(SnapshotMismatch, match="RESPONSE_MISMATCH"):
            verifier.validate_order(corrupt, order["order_id"], order["user_id"])
    with pytest.raises(SnapshotMismatch, match="RESPONSE_MISMATCH"):
        verifier.validate_order(body, order["order_id"], "other")


@pytest.mark.parametrize("target", ["manifest.json", "database/ecommerce.db"])
def test_snapshot_rejects_malformed_or_modified_files(tmp_path, target):
    from agent.snapshot import SnapshotValidationError, SnapshotVerifier

    copy = tmp_path / "dataset"
    shutil.copytree(DATASET, copy)
    (copy / target).write_bytes(b"invalid")
    with pytest.raises(SnapshotValidationError, match="SNAPSHOT_INVALID"):
        SnapshotVerifier(copy)


@pytest.mark.parametrize("reason", ["token", "manifest"])
def test_invalid_configuration_prevents_uvicorn_readiness(launch, tmp_path, reason):
    params = {"token": None}
    if reason == "manifest":
        data = tmp_path / "broken"
        data.mkdir()
        (data/"manifest.json").write_text("{}", encoding="utf-8")
        params = {"dataset": data}
    process, _, log = launch(**params)
    assert process.wait(10) != 0
    output = log.read_text(encoding="utf-8", errors="replace")
    assert {"token": "MOCK_SERVICE_TOKEN_REQUIRED", "manifest": "SNAPSHOT_INVALID"}[reason] in output


def test_real_http_actor_auth_field_parity_and_no_logistics(launch, order):
    linked = query(
        "SELECT logistics.*, orders.user_id FROM logistics JOIN orders USING (order_id) ORDER BY logistics_id LIMIT 1"
    )[0]
    unlinked = query("SELECT * FROM orders WHERE order_id='ACC-009'")[0]
    url = ready(launch)
    actor = order["user_id"]
    with httpx.Client(base_url=url, trust_env=False) as client:
        path = f"/orders/{order['order_id']}"
        assert client.get(path).json() == {"error": {"code": "unauthorized", "accepted": False}}
        assert client.get(path, headers={"Authorization": "Bearer very-secret-wrong"}).status_code == 401
        assert client.get(path, headers={"Authorization": f"Bearer {TEST_TOKEN}"}).status_code == 400
        assert client.get(path, headers=headers(actor)).status_code == 200
        body = client.get(path, headers=headers(actor)).json()
        meta = body.pop("_meta")
        assert body == order
        assert {k: meta[k] for k in BASE_META} == BASE_META
        assert client.get(path, headers=headers("other")).json() == client.get(
            f"/orders/{ABSENT}", headers=headers(actor)
        ).json() == {"error": {"code": "order_unavailable", "accepted": False}}
        assert client.get(
            f"/logistics/{unlinked['order_id']}", headers=headers(unlinked["user_id"]),
        ).json()["error"]["code"] == "logistics_unavailable"
        null_order = client.get("/orders/ACC-009", headers=headers(unlinked["user_id"])).json()
        assert null_order["status"] is None
        edge = query("SELECT * FROM orders WHERE order_id='ACC-012'")[0]
        edge_reply = client.get("/orders/ACC-012", headers=headers(edge["user_id"])).json()
        assert type(edge_reply["amount"]) is int and edge_reply["amount"] == edge["amount"] == 1_000_000
        logistics = client.get(
            f"/logistics/{linked['order_id']}", headers=headers(linked["user_id"]),
        ).json()
        from agent.snapshot import SnapshotVerifier
        assert SnapshotVerifier(DATASET).validate_logistics(
            logistics, linked["order_id"], linked["user_id"],
        ) == logistics
        logistics.pop("_meta")
        linked.pop("user_id")
        assert logistics == linked


def test_real_http_pagination_is_complete_and_cursor_scope_is_checked(launch, tmp_path):
    busiest = query("SELECT user_id, count(*) AS n FROM orders GROUP BY user_id ORDER BY n DESC LIMIT 1")[0]
    actor = busiest["user_id"]
    expected = query("SELECT * FROM orders WHERE user_id=? ORDER BY order_id", (actor,))
    with httpx.Client(base_url=ready(launch), trust_env=False) as client:
        found, cursor, first_cursor = [], None, None
        for _ in range(30):
            params = {"limit": 5}
            if cursor:
                params["cursor"] = cursor
            response = client.get("/orders", params=params, headers=headers(actor))
            assert response.status_code == 200
            page = response.json()
            assert page["_meta"]["dataset_id"] == DATASET_ID
            assert len(page["items"]) <= 5
            found.extend(page["items"])
            cursor = page["next_cursor"]
            first_cursor = first_cursor or cursor
            if not cursor:
                break
        assert first_cursor and not cursor
        assert found == expected and len(found) == busiest["n"]
        assert client.get("/orders", headers=headers("other"), params={"cursor": first_cursor}).status_code == 400
        for suffix in ("", "x"):
            invalid_cursor = "nonsense" if not suffix else first_cursor + suffix
            assert client.get(
                "/orders", headers=headers(actor), params={"cursor": invalid_cursor},
            ).status_code == 400
        for limit in ("0", "101", "bad"):
            assert client.get("/orders", headers=headers(actor), params={"limit": limit}).status_code == 422
    from database.seed import generate_dataset

    # Cross-dataset cursors need a second snapshot, not a shipped historical fixture.
    anchor = tmp_path / "anchor"
    generate_dataset(anchor, anchor_date=date(2027, 1, 1))
    anchor_id = json.loads((anchor / "snapshots/baseline/snapshot.json").read_text(encoding="utf-8"))["base_dataset_id"]
    assert anchor_id != DATASET_ID
    other = ready(launch, expected_dataset_id=anchor_id, dataset=anchor)
    with httpx.Client(base_url=other, trust_env=False) as client:
        assert client.get("/orders", headers=headers(actor), params={"cursor": first_cursor}).status_code == 400


def ticket_rows(path):
    with closing(sqlite3.connect(path)) as con:
        return con.execute("SELECT ticket_id,order_id,user_id,reason FROM tickets ORDER BY rowid").fetchall()


def test_real_http_ticket_validation_and_no_hidden_idempotency(launch, tmp_path, order):
    db = tmp_path / "tickets.sqlite"
    with httpx.Client(base_url=ready(launch), trust_env=False) as client:
        actor = order["user_id"]
        post_headers = dict(headers(actor), **{"Idempotency-Key": "same-both-times"})
        payload = {"order_id": order["order_id"], "user_id": actor, "reason": "  question  "}
        for invalid in [dict(payload, user_id="other"), dict(payload, reason=" \n "), dict(payload, reason="x"*501)]:
            response = client.post("/tickets", headers=post_headers, json=invalid)
            assert response.status_code in (403, 422)
            assert response.json()["error"]["accepted"] is False
        assert client.post("/tickets", headers=post_headers, json=dict(payload, order_id=ABSENT)).status_code == 404
        assert client.get("/tickets", headers=post_headers).status_code in (404, 405)
        replies = [client.post("/tickets", headers=post_headers, json=payload) for _ in range(2)]
        assert all(response.status_code == 201 for response in replies)
        tickets = [response.json()["ticket_id"] for response in replies]
        assert tickets[0] != tickets[1]
        assert ticket_rows(db) == [(t, payload["order_id"], actor, "question") for t in tickets]


def test_real_http_fault_script_counts_and_post_accept_timeout(launch, tmp_path, order):
    db = tmp_path / "tickets.sqlite"
    with httpx.Client(base_url=ready(launch, faults="baseline-http.json"), trust_env=False) as client:
        actor = order["user_id"]
        h = headers(actor)
        payload = {"order_id": order["order_id"], "user_id": actor, "reason": "fault proof"}
        reads = [client.get("/orders", headers=h) for _ in range(4)]
        assert [r.status_code for r in reads] == [429, 500, 200, 200]
        assert reads[0].headers["retry-after"] == "1"
        assert reads[2].json()["items"] == reads[3].json()["items"]
        first = client.post("/tickets", headers=h, json=payload)
        assert first.status_code == 500 and "accepted" not in first.json()["error"]
        assert ticket_rows(db) == []
        with pytest.raises(httpx.ReadTimeout):
            client.post("/tickets", headers=h, json=payload, timeout=0.2)
        assert ticket_rows(db) == []
        with pytest.raises(httpx.ReadTimeout):
            client.post("/tickets", headers=h, json=payload, timeout=0.2)
        assert len(ticket_rows(db)) == 1
        after = client.post("/tickets", headers=h, json=payload)
        assert after.status_code == 500 and "accepted" not in after.json()["error"]
        assert len(ticket_rows(db)) == 2
        assert client.post("/tickets", headers=h, json=payload).status_code == 201
        assert len(ticket_rows(db)) == 3


def test_real_http_prevents_live_snapshot_switch(launch, tmp_path, order):
    copy = tmp_path / "dataset"
    shutil.copytree(DATASET, copy)
    with httpx.Client(base_url=ready(launch, dataset=copy), trust_env=False) as client:
        path = f"/orders/{order['order_id']}"
        h = headers(order["user_id"])
        initial = client.get(path, headers=h).json()
        with closing(sqlite3.connect(copy / "database/ecommerce.db")) as con, con:
            con.execute("UPDATE orders SET amount=5 WHERE order_id=?", (order["order_id"],))
        reply = client.get(path, headers=h).json()
        assert reply["amount"] == initial["amount"] == 0
        assert reply["_meta"]["dataset_id"] == initial["_meta"]["dataset_id"] == DATASET_ID
        process, _, log = launch(dataset=copy)
        assert process.wait(10) != 0
        assert "SNAPSHOT_INVALID" in log.read_text(encoding="utf-8", errors="replace")


def test_real_http_runtime_profile_retries_and_first_post_accepts(launch, tmp_path, order):
    db = tmp_path / "tickets.sqlite"
    with httpx.Client(base_url=ready(launch, faults="runtime-http.json"), trust_env=False) as client:
        actor = order["user_id"]
        path = f"/orders/{order['order_id']}"
        replies = [client.get(path, headers=headers(actor)) for _ in range(3)]
        assert [reply.status_code for reply in replies] == [429, 500, 200]
        assert replies[0].headers["retry-after"] == "0"
        payload = {"order_id": order["order_id"], "user_id": actor, "reason": "runtime profile"}
        with pytest.raises(httpx.ReadTimeout):
            client.post("/tickets", headers=headers(actor), json=payload, timeout=0.2)
        assert len(ticket_rows(db)) == 1


@pytest.mark.parametrize(("profile", "codes"), [
    ("read-429-then-ok.json", [429, 200, 200, 200]),
    ("read-500-three-times.json", [500, 500, 500, 200]),
])
def test_real_http_acc002_acceptance_read_profiles(launch, profile, codes):
    with httpx.Client(base_url=ready(launch, faults=profile), trust_env=False) as client:
        response_codes = [client.get("/orders/ACC-002", headers=headers("acc-alice")).status_code for _ in codes]
        assert response_codes == codes


def test_freeze_verification_is_required_and_covers_contract_source_data_and_faults(tmp_path, formal_freeze):
    from mock.verify_freeze import verify_freeze

    baseline = formal_freeze
    assert baseline["api_version"] == BASE_META["api_version"]
    assert baseline["dataset_id"] == DATASET_ID
    assert "mock/faults/baseline-http.json" in baseline["files"]
    assert "docs/Mock接口契约-v1.md" in baseline["files"]
    assert "mock_service/app.py" in baseline["files"]
    assert "data/c004-r01/baseline/manifest.json" in baseline["files"]
    for name in ["mock/freeze.json", *baseline["files"]]:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, path)
    (tmp_path / "mock_service/app.py").write_text("source changed", encoding="utf-8")
    with pytest.raises(ValueError, match="MOCK_FREEZE_MISMATCH"):
        verify_freeze(tmp_path / "mock/freeze.json")
