"""Real TCP, frozen Mock fault scripts, real graph, explicit controlled model."""
import pytest

from dev.fault_probe import CASES, configuration, probe_case
from eval.controlled import ControlledModel
from knowledge.ingest import ingest_knowledge


@pytest.mark.parametrize("name", tuple(CASES))
async def test_bounded_retry_confirmation_and_single_write_over_http(tmp_path, name):
    settings = configuration("controlled")
    model = ControlledModel({"kind": "retry_500" if CASES[name].get("logistics") else "retry_429"})
    retriever = await ingest_knowledge(settings, model)
    result = await probe_case(name, tmp_path / name, settings, model, retriever)
    assert result["passed"], {key: value for key, value in result["checks"].items() if not value}
