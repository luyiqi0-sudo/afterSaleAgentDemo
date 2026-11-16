"""Ordinary current-time wording must not erase real unknown qualifiers."""
import pytest

from agent.answer_support import policy_for_goal
from test_answer_support import baseline_evidence


@pytest.mark.parametrize("question,covered", [
    ("目前电子发票需要几个工作日开具？请引用文档名和段落。", True),
    ("目前电子发票多久开具？", True),
    ("目前电子发票涉及海外退税时，需要几个工作日开具？", False),
    ("目前电子发票的手续费是多少钱？", False),
])
def test_current_time_word_does_not_change_business_coverage(question, covered):
    evidence = baseline_evidence("invoice")
    data, unmet, coverage = policy_for_goal({"description": question}, {"evidences": [evidence]})
    if covered:
        assert unmet is None and data["evidences"] == [evidence]
    else:
        assert unmet == "NO_DATA" and coverage["policy_uncovered"]
