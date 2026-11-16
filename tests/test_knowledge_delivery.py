"""The delivered top-level corpus is runnable and stays bound to the frozen dataset."""
import argparse
from pathlib import Path
import shutil

import httpx
import pytest

from agent.config import Settings
from api.main import assemble
from eval.controlled import ControlledModel
from eval.runner import demo_cases, run_case
from knowledge.ingest import ingest_knowledge


PROJECT = Path(__file__).resolve().parents[1]
DATASET = PROJECT / "data/c004-r01/baseline"
CORPUS = PROJECT / "knowledge"


def delivery_settings(corpus, runtime):
    return Settings(
        _env_file=None, dataset_root=DATASET,
        database_path=DATASET / "database/ecommerce.db",
        dictionary_path=DATASET / "database/dictionary.json",
        knowledge_path=corpus, runtime_dir=runtime,
        business_service_token="synthetic-delivery-service-token",
        vector_backend="memory", embedding_model="controlled-topics-v1", embedding_dimension=16,
    )


async def test_delivered_root_corpus_answers_policy_through_real_http(tmp_path):
    case = next(case for case in demo_cases(DATASET) if case["id"] == "D04")
    settings = delivery_settings(CORPUS, tmp_path / "runtime")
    model = ControlledModel(case)
    retriever = await ingest_knowledge(settings, model)
    assert len(list(CORPUS.glob("*.md"))) == 30
    result = await run_case(DATASET, case, "controlled", settings, model, retriever, tmp_path / "case")
    assert result["turns"][-1]["done"]["status"] == "COMPLETED"
    assert "invoice-v2.md" in result["answer"]


@pytest.mark.parametrize("corruption", ["edit", "missing", "extra", "no_directory"])
async def test_assembly_rejects_changed_delivery_corpus_before_provider_use(tmp_path, corruption):
    corpus = tmp_path / "knowledge"
    if corruption != "no_directory":
        shutil.copytree(DATASET / "knowledge", corpus)
        if corruption == "edit":
            with (corpus / "invoice-v2.md").open("ab") as output:
                output.write(b"\nUnreviewed policy change.\n")
        elif corruption == "missing":
            (corpus / "invoice-v2.md").unlink()
        else:
            shutil.copy2(corpus / "invoice-v2.md", corpus / "extra.md")

    def unexpected_request(request):
        pytest.fail("A changed corpus must not reach a business or model request")

    async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected_request)) as business:
        with pytest.raises(ValueError, match="^KNOWLEDGE_SNAPSHOT_MISMATCH$"):
            await assemble(delivery_settings(corpus, tmp_path / "runtime"), business,
                           lambda *_: None, model=object())
    assert not (tmp_path / "runtime").exists()


async def test_ingest_cli_rejects_changed_corpus_before_constructing_provider(tmp_path, monkeypatch, capsys):
    import knowledge.ingest as ingestion

    corpus = tmp_path / "knowledge"
    shutil.copytree(DATASET / "knowledge", corpus)
    (corpus / "invoice-v2.md").unlink()
    monkeypatch.setattr(ingestion, "Settings", lambda **_: delivery_settings(corpus, tmp_path / "runtime"))

    def unexpected_provider(*args, **kwargs):
        pytest.fail("Invalid delivery corpus must be rejected before creating the model provider")

    monkeypatch.setattr(ingestion, "HTTPModelProvider", unexpected_provider)
    assert await ingestion._run(argparse.Namespace(backend=None, query=None, demo_scope=False)) == 1
    assert "KNOWLEDGE_SNAPSHOT_MISMATCH" in capsys.readouterr().out
