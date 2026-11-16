"""The delivered evaluation corpus is executable and bound to its frozen facts."""
import argparse
from pathlib import Path

import pytest

from eval import runner
from eval.eval import evaluate_cases, read_cases


PROJECT = Path(__file__).resolve().parents[1]
ROOT = PROJECT / "data/c004-r01/baseline"
CASES = PROJECT / "eval/golden_dataset.jsonl"


def test_root_eval_dataset_is_the_default_scored_corpus():
    assert CASES.is_file()
    cases = read_cases(ROOT)
    assert len(cases) == 36
    assert cases == runner.load_cases(ROOT)
    assert evaluate_cases(ROOT, contract="mvp-agent-v2")["summary"]["not_run"] == 36


@pytest.mark.parametrize("corruption", ["missing", "changed"])
def test_scoring_rejects_missing_or_changed_delivery_cases(tmp_path, corruption):
    path = tmp_path / "golden_dataset.jsonl"
    if corruption == "changed":
        path.write_bytes((ROOT / "eval/golden_dataset.jsonl").read_bytes() + b"\n")
    with pytest.raises(ValueError, match="^EVAL_SNAPSHOT_MISMATCH$"):
        evaluate_cases(ROOT, cases_path=path)


@pytest.mark.parametrize("corruption", ["missing", "changed"])
async def test_runner_rejects_invalid_cases_before_provider_and_output(tmp_path, monkeypatch, corruption):
    path = tmp_path / "golden_dataset.jsonl"
    if corruption == "changed":
        path.write_bytes((ROOT / "eval/golden_dataset.jsonl").read_bytes() + b"\n")

    def unexpected_provider(*args, **kwargs):
        pytest.fail("Invalid cases must be rejected before model setup")

    monkeypatch.setattr(runner, "HTTPModelProvider", unexpected_provider)
    output = tmp_path / "evidence"
    args = argparse.Namespace(root=ROOT, cases_file=path, output=output, profile="real", demo=False,
                              ids=None, ingest_timeout=None, knowledge_path=None, database_dir=None)
    with pytest.raises(ValueError, match="^EVAL_SNAPSHOT_MISMATCH$"):
        await runner.run(args)
    assert not output.exists()
