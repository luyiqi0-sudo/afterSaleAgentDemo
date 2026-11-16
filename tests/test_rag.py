from datetime import date

import pytest


def doc(tmp_path, name, effective, text, **extra):
    import yaml
    path = tmp_path / name
    metadata = {"policy_key": "returns", "effective_date": effective, "conditions": {}, **extra}
    path.write_text("---\n" + yaml.safe_dump(metadata) + "---\n# 退货\n\n" + text, encoding="utf-8")
    return path


def test_latest_policy_selected_before_ranking(tmp_path):
    from agent.retriever import parse_documents, select_versions
    doc(tmp_path, "old.md", "2025-01-01", "旧政策：七天退货。")
    # Policy authority now comes from explicit replacement, not a later date alone.
    doc(tmp_path, "new.md", "2026-01-01", "新政策：十四天退货。", supersedes=["old"])
    doc(tmp_path, "future.md", "2027-01-01", "未来政策：三十天退货。", supersedes=["new"])
    chunks = parse_documents(tmp_path)
    current, notes = select_versions(chunks, date(2026, 11, 15), {})
    assert {c.file_name for c in current} == {"new.md"}
    assert notes
    assert current[0].paragraph == 1


def test_expired_latest_does_not_resurrect_old(tmp_path):
    from agent.retriever import parse_documents, select_versions
    doc(tmp_path, "old.md", "2025-01-01", "七天。")
    doc(tmp_path, "new.md", "2026-01-01", "十四天。", expires_at="2026-06-01", supersedes=["old"])
    current, notes = select_versions(parse_documents(tmp_path), date(2026, 11, 15), {})
    assert current == []
    assert any("过期" in n for n in notes)


def test_same_date_conflict_is_not_silently_resolved(tmp_path):
    from agent.retriever import parse_documents, select_versions
    doc(tmp_path, "one.md", "2026-01-01", "七天。")
    doc(tmp_path, "two.md", "2026-01-01", "十四天。")
    current, notes = select_versions(parse_documents(tmp_path), date(2026, 11, 15), {})
    assert current == []
    assert any("冲突" in n for n in notes)


def test_conditions_cannot_be_guessed(tmp_path):
    from agent.retriever import parse_documents, select_versions
    doc(tmp_path, "vip.md", "2026-01-01", "三十天。", conditions={"tier": "vip"})
    current, notes = select_versions(parse_documents(tmp_path), date(2026, 11, 15), {})
    assert current == []
    assert notes


def test_missing_metadata_is_rejected(tmp_path):
    from agent.retriever import parse_documents
    (tmp_path / "bad.md").write_text("# 政策\n七天", encoding="utf-8")
    with pytest.raises(ValueError):
        parse_documents(tmp_path)
