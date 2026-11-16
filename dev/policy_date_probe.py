"""Simulate policy query dates in isolated FastAPI instances, using real models.

This is a test clock, not a date field accepted by /chat. The running service,
its sessions, .env, frozen data and system clock remain unchanged.
"""
import argparse
import asyncio
from datetime import date
import json
from pathlib import Path
import secrets
from uuid import uuid4

import httpx
from pydantic import SecretStr

from agent.config import Settings
from agent.provider import HTTPModelProvider, ProviderError
from api.main import create_app
from dev.demo import DATABASE, KNOWLEDGE, PRIVATE, ROOT
from knowledge.ingest import ingest_knowledge


QUESTION = "电子发票一般多久能开出来？"


async def run(days):
    # Reuse only the existing local business-service credential; each test app
    # gets an independent demo credential and runtime/checkpoint directory.
    business_token = json.loads(PRIVATE.read_text(encoding="utf-8"))["business"]
    output = Path("runtime/policy-date-probe") / uuid4().hex
    output.mkdir(parents=True)
    settings = Settings().model_copy(update={
        "dataset_root": ROOT, "database_path": DATABASE / "ecommerce.db",
        "dictionary_path": DATABASE / "dictionary.json", "knowledge_path": KNOWLEDGE,
        "business_base_url": "http://127.0.0.1:8090",
        "business_service_token": SecretStr(business_token), "vector_backend": "memory",
        "demo_mode": True, "model_timeout_seconds": 25.0,
    })
    observations = []
    print("Building an isolated index with the configured embedding provider...", flush=True)
    async with httpx.AsyncClient() as model_client:
        # The corpus generation is date independent. Applicability is decided
        # anew for each request, so both dates use the same real vectors.
        index = await ingest_knowledge(settings, HTTPModelProvider(settings, model_client))
        for number, day in enumerate(days):
            bearer = secrets.token_urlsafe(32)
            case_settings = settings.model_copy(update={
                "anchor_date": day, "runtime_dir": output / f"{number}-{day}",
                "auth_tokens": {bearer: "eval-u01"},
            })
            app = create_app(case_settings, model=HTTPModelProvider(case_settings, model_client), retriever=index)
            record = {"anchor_date": str(day), "question": QUESTION,
                      "transport": "in_process_asgi", "profile": "real_model_memory_index"}
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
                    health = await client.get("/health?mode=ready")
                    record["readiness_http_status"] = health.status_code
                    if health.status_code != 200:
                        record["readiness"] = health.json()
                    else:
                        reply = await client.post("/chat", json={"message": QUESTION, "client_message_id": uuid4().hex},
                                                  headers={"Authorization": "Bearer " + bearer})
                        record["http_status"] = reply.status_code
                        record["events"] = [json.loads(line[6:]) for line in reply.text.splitlines() if line.startswith("data: ")]
                        record["done"] = next((event["data"] for event in record["events"] if event["type"] == "done"), None)
            observations.append(record)
            (output / "observations.json").write_text(json.dumps(observations, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            done = record.get("done") or {}
            print(f"\n模拟查询日期：{day}；任务状态：{done.get('task_status', 'UNAVAILABLE')}", flush=True)
            print(done.get("answer") or "未取得最终回答，请查看运行记录。", flush=True)
    print(f"\n运行记录：{(output / 'observations.json').resolve()}", flush=True)
    return int(any((row.get("done") or {}).get("task_status") != "COMPLETED" for row in observations))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", action="append", type=date.fromisoformat, dest="dates",
                        help="Simulated query date (YYYY-MM-DD); repeat to compare dates")
    args = parser.parse_args()
    try:
        return asyncio.run(run(args.dates or [date(2026, 9, 30), date(2026, 10, 1)]))
    except (OSError, ValueError, RuntimeError, httpx.HTTPError) as error:
        code = error.code if isinstance(error, ProviderError) else type(error).__name__
        print(f"验证未完成：{code}。需要已有本机 Mock、私有演示凭据及可用模型配置。")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
