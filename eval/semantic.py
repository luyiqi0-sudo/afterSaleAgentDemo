"""Optional real DeepEval adapter. No dependency/model/answers means NOT_RUN.

API sources checked 2026-11-15:
https://deepeval.com/docs/metrics-faithfulness
https://deepeval.com/docs/metrics-answer-relevancy
"""


def semantic_report(samples: list[dict], judge_model=None) -> dict:
    if not judge_model or not samples:
        return {"status": "NOT_RUN", "reason": "requires explicit judge model and actual Agent answers", "cases": []}
    try:
        from deepeval.metrics import AnswerRelevancyMetric, FaithfulnessMetric
        from deepeval.test_case import LLMTestCase
        from importlib.metadata import version
    except ImportError:
        return {"status": "NOT_RUN", "reason": "optional deepeval dependency unavailable", "cases": []}
    results = []
    for sample in samples:
        metrics = {}
        for name, factory in (("answer_relevancy", AnswerRelevancyMetric), ("faithfulness", FaithfulnessMetric)):
            if name == "faithfulness" and not sample.get("retrieval_context"):
                metrics[name] = {"status": "NOT_RUN", "reason": "no actual retrieval context"}
                continue
            try:
                metric = factory(model=judge_model, threshold=0.85, include_reason=True, async_mode=False)
                case = LLMTestCase(input=sample["input"], actual_output=sample["answer"],
                                   retrieval_context=sample.get("retrieval_context"))
                metric.measure(case)
                metrics[name] = {"status": "PASS" if metric.is_successful() else "FAIL",
                                 "score": metric.score, "reason": metric.reason}
            except Exception as exc:
                metrics[name] = {"status": "ERROR", "reason": type(exc).__name__}
        results.append({"id": sample["id"], "metrics": metrics})
    statuses = [m["status"] for r in results for m in r["metrics"].values()]
    return {"status": "FAIL" if any(s in ("FAIL", "ERROR") for s in statuses) else "PASS" if all(s == "PASS" for s in statuses) else "PARTIAL",
            "library_version": version("deepeval"), "judge_model": str(judge_model), "threshold": 0.85,
            "cases": results, "note": "semantic supplement; never overrides deterministic safety failures"}
