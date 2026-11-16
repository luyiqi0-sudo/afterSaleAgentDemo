"""Isolated HTTP fault demos. Never changes the running Demo or frozen Mock."""
import argparse
import asyncio
import hashlib
import json
import secrets
import sqlite3
import time
from pathlib import Path
from uuid import uuid4

import httpx
from pydantic import SecretStr

from agent.config import Settings
from agent.provider import HTTPModelProvider
from api.main import create_app
from eval.controlled import ControlledModel
from eval.runner import _read_traces, _ticket_count, _turn, serve
from knowledge.ingest import ingest_knowledge
from mock.verify_freeze import verify_freeze
from mock_service.app import create_app as mock_app


ROOT = Path("data/c004-r01/baseline").resolve()
ORDER = "EVAL-002"
CASES = {
    "429-recover": {"faults": [{"action": "429", "retry_after": 1}] * 2, "codes": [429, 429, 200]},
    "500-recover": {"logistics": True, "faults": ["500"], "codes": [500, 200]},
    "500-exhausted": {"faults": ["500"] * 3, "codes": [500] * 3, "offer": True},
    "429-exhausted": {"faults": [{"action": "429", "retry_after": 1}] * 3, "codes": [429] * 3, "offer": True},
    "logistics-exhausted": {"logistics": True, "faults": ["500"] * 3, "codes": [500] * 3, "offer": True},
    "retry-after-budget": {"faults": [{"action": "429", "retry_after": 60}], "codes": [429], "offer": True},
    "read-only": {"faults": ["500"] * 3, "codes": [500] * 3, "readonly": True},
    "write-500": {"faults": ["500"] * 3, "codes": [500] * 3, "offer": True, "write": "500"},
    "write-429": {"faults": ["500"] * 3, "codes": [500] * 3, "offer": True, "write": "429"},
    "write-500-after": {"faults": ["500"] * 3, "codes": [500] * 3, "offer": True, "write": "500_after"},
}


def configuration(profile):
    return Settings(_env_file=None if profile == "controlled" else ".env").model_copy(update={
        "dataset_root": ROOT, "database_path": Path("database/ecommerce.db").resolve(),
        "dictionary_path": Path("database/dictionary.json").resolve(), "knowledge_path": Path("knowledge").resolve(),
        "vector_backend": "memory", "model_timeout_seconds": 25,
        **({"embedding_dimension": 16, "embedding_model": "controlled-topics-v1"} if profile == "controlled" else {}),
    })


async def probe_case(name, output, settings, model, retriever):
    """Checks use wire observations and independent Mock DB reads, not Agent claims."""
    case = CASES[name]
    output.mkdir(parents=True, exist_ok=False)
    token, credential = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    path = f"/{'logistics' if case.get('logistics') else 'orders'}/{ORDER}"
    faults = {"GET " + path: case["faults"]}
    if case.get("write"):
        faults["POST /tickets"] = [case["write"]]
    ticket_db = output / "mock.sqlite"
    frozen = mock_app(ROOT, ticket_db, token, faults)
    wire = []

    @frozen.middleware("http")
    async def observe(request, call_next):
        started = time.monotonic()
        response = await call_next(request)
        if request.url.path in {path, "/tickets"}:
            wire.append({"method": request.method, "path": request.url.path,
                         "status": response.status_code, "start": started, "end": time.monotonic()})
        return response

    question = (f"帮我看看订单 {ORDER} 的包裹现在到哪里了，快递单号和最近更新时间是什么？"
                if case.get("logistics") else f"帮我查一下订单 {ORDER} 现在的状态和金额。")
    if case.get("readonly"):
        question += "只查询，不用创建售后工单。"
    before = hashlib.sha256(settings.database_path.read_bytes()).hexdigest()
    settings = settings.model_copy(update={
        "runtime_dir": output / "api", "demo_mode": True, "auth_tokens": {credential: "eval-u01"},
        "business_service_token": SecretStr(token),
    })
    turns, counts, operations = [], [], []
    async with serve(frozen) as business:
        app = create_app(settings.model_copy(update={"business_base_url": business}), model=model, retriever=retriever)
        async with serve(app) as base, httpx.AsyncClient(base_url=base, timeout=35, trust_env=False) as client:
            headers = {"Authorization": "Bearer " + credential}
            first = await _turn(client, headers, {"message": question, "client_message_id": "query"})
            turns.append(first)
            counts.append(_ticket_count(ticket_db))
            waiting = first.get("done", {}).get("interrupt")
            if case.get("offer") and waiting and waiting["kind"] == "CONFIRM":
                body = {"message": "确认", "client_message_id": "approve",
                        "session_id": first["done"]["session_id"],
                        "interrupt_id": waiting["interrupt_id"], "expected_revision": waiting["expected_revision"]}
                for payload in (body, body, {**body, "client_message_id": "duplicate"}):
                    turns.append(await _turn(client, headers, payload))
                    counts.append(_ticket_count(ticket_db))
            controller = app.state.controller
            await controller.tools.drain_sends(timeout=2)
            operations = [{k: op[k] for k in ("operation_id", "order_id", "state", "attempt_count")}
                          for run in controller.store.list_runs() for op in controller.store.list_operations(run["run_id"])]
            health = (await client.get("/health?mode=live")).status_code
    trace = _read_traces(settings.runtime_dir / "private-trace.jsonl")
    reads = [r for r in wire if r["method"] == "GET"]
    writes = [r for r in wire if r["method"] == "POST"]
    retries = [r for r in trace if r["event"] == "tool_retry"]
    gaps = [reads[i + 1]["start"] - reads[i]["end"] for i in range(len(reads) - 1)]
    with sqlite3.connect(ticket_db.resolve().as_uri() + "?mode=ro", uri=True) as con:
        tickets = [dict(zip(("ticket_id", "order_id", "reason"), row))
                   for row in con.execute("SELECT ticket_id,order_id,reason FROM tickets")]
    done = first.get("done", {})
    checks = {
        "expected_http_sequence": [r["status"] for r in reads] == case["codes"],
        "backoff_observed": len(retries) == len(gaps) and all(
            .2 * 2**i <= r["delay_seconds"] and gap + .03 >= r["delay_seconds"]
            for i, (r, gap) in enumerate(zip(retries, gaps))),
        "retry_after_respected": all(gap >= .97 for gap in gaps) if name.startswith("429-") else True,
        "bounded_terminal_delivery": first["http_status"] == 200 and done.get("persisted") is True
        and first["events"][-1]["at_ms"] < 32000 and not any(e["event"] == "error" for e in first["events"]),
        "health_after_fault": health == 200,
        "no_write_before_confirmation": counts[0] == 0,
        "business_data_unchanged": before == hashlib.sha256(settings.database_path.read_bytes()).hexdigest(),
    }
    if case.get("offer"):
        expected_count = int(case.get("write") in (None, "500_after"))
        expected_state = "UNKNOWN" if case.get("write") else "SUCCESS"
        checks.update({
            "failure_explained_and_confirmed": done.get("status") == "WAITING_CONFIRM"
            and "读取服务暂不可用" in done.get("answer", "") and "尚未提交" in done.get("answer", ""),
            "one_confirmed_write": len(writes) == 1 and writes[0]["status"] == (
                201 if not case.get("write") else 429 if case["write"] == "429" else 500),
            "ledger_and_remote_count": len(operations) == 1 and operations[0]["state"] == expected_state
            and operations[0]["attempt_count"] == 1 and len(tickets) == expected_count,
            "replay_and_stale_guard": len(turns) == 4 and turns[1].get("done") == turns[2].get("done")
            and turns[3].get("http_status") == 409 and turns[3].get("error") == {"detail": "STALE_INTERRUPT"}
            and counts == [0, expected_count, expected_count, expected_count],
            "honest_write_answer": len(turns) >= 2 and (
                "待核对" in turns[1].get("done", {}).get("answer", "") if expected_state == "UNKNOWN"
                else "工单已创建" in turns[1].get("done", {}).get("answer", "")),
        })
    elif case.get("readonly"):
        checks["read_only_respected"] = not writes and done.get("interrupt") is None and "读取服务暂不可用" in done.get("answer", "")
    else:
        checks["query_recovered"] = done.get("status") == "COMPLETED" and not writes
    result = {"case": name, "passed": all(checks.values()), "checks": checks, "question": question,
              "faults": faults, "turns": turns, "http": wire, "actual_backoff_seconds": gaps,
              "retry_trace": retries, "ticket_counts": counts, "tickets": tickets, "operations": operations}
    (output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


async def run(args):
    verify_freeze(Path("mock/freeze.json"))
    output = args.output or Path("runtime/fault-probe") / uuid4().hex
    output.mkdir(parents=True, exist_ok=False)
    settings = configuration(args.profile)
    shared = ControlledModel() if args.profile == "controlled" else HTTPModelProvider(settings)
    retriever = await ingest_knowledge(settings, shared)
    results = []
    for name in args.cases or CASES:
        case = CASES[name]
        model = (ControlledModel({"kind": "retry_500" if case.get("logistics") else "retry_429"})
                 if args.profile == "controlled" else shared)
        result = await probe_case(name, output / name, settings, model, retriever)
        summary = {"case": name, "passed": result["passed"], "failed_checks": [k for k, v in result["checks"].items() if not v],
                   "http": [r["status"] for r in result["http"]],
                   "backoff_seconds": [round(t, 3) for t in result["actual_backoff_seconds"]],
                   "first_turn_ms": result["turns"][0]["events"][-1]["at_ms"], "ticket_counts": result["ticket_counts"]}
        results.append(summary)
        print(json.dumps(summary, ensure_ascii=False), flush=True)
    sources = [Path(p) for p in ("agent/graph_nodes.py", "agent/events.py", "agent/tools.py", "dev/fault_probe.py")]
    report = {"profile": args.profile, "model": settings.model_name if args.profile == "real" else "controlled-candidate",
              "results": results, "passed": all(r["passed"] for r in results),
              "sources": {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}}
    (output / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Evidence: {output.resolve()}", flush=True)
    return report["passed"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=["controlled", "real"], default="controlled")
    parser.add_argument("--cases", choices=tuple(CASES), nargs="+")
    parser.add_argument("--output", type=Path)
    raise SystemExit(0 if asyncio.run(run(parser.parse_args())) else 1)


if __name__ == "__main__":
    main()
