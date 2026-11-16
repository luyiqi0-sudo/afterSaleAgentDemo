from datetime import date

import pytest
import yaml

from agent.retriever import parse_documents, select_versions


AS_OF = date(2026, 11, 15)


def write_doc(root, name, start, days, *, supersedes=(), conditions=None,
              basis="query_date", until=None, body=None):
    meta = {"policy_key": "returns", "doc_id": name, "version": name,
            "effective_date": start, "expires_at": until, "time_basis": basis,
            "supersedes": list(supersedes), "rules": {"return_days": days},
            "conditions": conditions or {}}
    path = root / (name + ".md")
    path.write_text("---\n" + yaml.safe_dump(meta) + "---\n# 退货\n\n" +
                    (body or f"可在 {days} 天内申请退货。"), encoding="utf-8")
    return path


def ids(rows):
    return {c.doc_id for c in rows}


def test_p01_same_rule_paraphrase_preserves_both_sources(tmp_path):
    write_doc(tmp_path, "a", "2025-01-01", 7)
    write_doc(tmp_path, "b", "2026-01-01", 7, body="七日内可退货。")
    rows, notes = select_versions(parse_documents(tmp_path), AS_OF, {})
    assert ids(rows) == {"a", "b"} and not notes


@pytest.mark.parametrize("body", ["十四日内可退货。", "七个工作日内可退货。"])
def test_same_rule_metadata_cannot_overrule_contradictory_text(tmp_path, body):
    write_doc(tmp_path, "a", "2025-01-01", 7)
    write_doc(tmp_path, "b", "2026-01-01", 7, body=body)
    rows, notes = select_versions(parse_documents(tmp_path), AS_OF, {})
    assert not rows and any("正文" in note for note in notes)


def test_equal_quantities_do_not_establish_unknown_equivalence(tmp_path):
    write_doc(tmp_path, "a", "2025-01-01", 7, body="七天内可以退货，但须全新未拆封。")
    write_doc(tmp_path, "b", "2026-01-01", 7, body="七天内可以退货，拆过包装也不影响。")
    rows, notes = select_versions(parse_documents(tmp_path), AS_OF, {})
    assert not rows and any("等价" in note for note in notes)


def test_different_rule_names_do_not_prove_mutual_exclusion(tmp_path):
    write_doc(tmp_path, "a", "2025-01-01", 7)
    other = write_doc(tmp_path, "b", "2026-01-01", 7, body="请保留完整包装。")
    other.write_text(other.read_text(encoding="utf-8").replace("return_days: 7", "packaging_required: true"),
                     encoding="utf-8")
    rows, notes = select_versions(parse_documents(tmp_path), AS_OF, {})
    assert not rows and any("等价" in note for note in notes)


def test_newly_effective_version_is_not_evidence_if_its_body_contradicts_metadata(tmp_path):
    write_doc(tmp_path, "a", "2025-01-01", 7)
    write_doc(tmp_path, "b", "2026-01-01", 14, supersedes=("a",), body="三十天内可退货。")
    rows, notes = select_versions(parse_documents(tmp_path), AS_OF, {})
    assert not rows and any("正文" in note for note in notes)
    assert ids(select_versions(parse_documents(tmp_path), date(2025, 12, 31), {})[0]) == {"a"}


def test_p02_disjoint_scopes_selected_without_guessing(tmp_path):
    write_doc(tmp_path, "a", "2025-01-01", 7, conditions={"region": "CN"})
    write_doc(tmp_path, "b", "2026-01-01", 14, conditions={"region": "SG"})
    chunks = parse_documents(tmp_path)
    assert ids(select_versions(chunks, AS_OF, {"region": "CN"})[0]) == {"a"}
    assert ids(select_versions(chunks, AS_OF, {"region": "SG"})[0]) == {"b"}
    assert not select_versions(chunks, AS_OF, {})[0]


def test_p03_declared_replacement_preserves_history(tmp_path):
    write_doc(tmp_path, "a", "2025-01-01", 7)
    write_doc(tmp_path, "b", "2026-01-01", 14, supersedes=("a",))
    chunks = parse_documents(tmp_path)
    assert ids(select_versions(chunks, date(2025, 11, 15), {})[0]) == {"a"}
    rows, notes = select_versions(chunks, AS_OF, {})
    assert ids(rows) == {"b"} and notes


def test_p04_overlapping_mutually_exclusive_values_require_authority(tmp_path):
    write_doc(tmp_path, "a", "2025-01-01", 7)
    write_doc(tmp_path, "b", "2026-01-01", 14)
    rows, notes = select_versions(parse_documents(tmp_path), AS_OF, {})
    assert not rows and any("冲突" in note for note in notes)


def test_p05_future_does_not_cut_current_and_expired_does_not_revive(tmp_path):
    write_doc(tmp_path, "a", "2025-01-01", 7)
    write_doc(tmp_path, "b", "2026-01-01", 14, supersedes=("a",), until="2026-08-01")
    write_doc(tmp_path, "c", "2027-01-01", 30, supersedes=("b",))
    chunks = parse_documents(tmp_path)
    assert ids(select_versions(chunks, date(2025, 12, 31), {})[0]) == {"a"}
    assert ids(select_versions(chunks, date(2026, 2, 1), {})[0]) == {"b"}
    assert not select_versions(chunks, date(2026, 8, 1), {})[0]
    assert ids(select_versions(chunks, date(2027, 1, 1), {})[0]) == {"c"}


def test_p06_order_date_is_separate_and_mandatory(tmp_path):
    write_doc(tmp_path, "a", "2025-01-01", 7, basis="order_create_date")
    write_doc(tmp_path, "b", "2026-01-01", 14, basis="order_create_date", supersedes=("a",))
    chunks = parse_documents(tmp_path)
    rows, notes = select_versions(chunks, AS_OF, {})
    assert not rows and any("下单日期" in note for note in notes)
    assert ids(select_versions(chunks, AS_OF, {}, order_create_date=date(2025, 7, 1))[0]) == {"a"}


@pytest.mark.parametrize("case", ["cycle", "self", "dangling", "scope", "time", "basis"])
def test_p07_rejects_invalid_replacement_graph(tmp_path, case):
    write_doc(tmp_path, "a", "2026-01-01", 7, supersedes=("a",) if case == "self" else
              (("b",) if case == "cycle" else ()))
    write_doc(tmp_path, "b", "2025-01-01" if case == "time" else "2026-01-01", 14,
              supersedes=("missing" if case == "dangling" else "a",),
              conditions={"region": "CN"} if case == "scope" else None,
              basis="order_create_date" if case == "basis" else "query_date")
    with pytest.raises(ValueError, match="supersedes"):
        parse_documents(tmp_path)


def test_chunk_offsets_and_hash_reference_unchanged_source(tmp_path):
    from hashlib import sha256
    path = write_doc(tmp_path, "a", "2026-01-01", 7, body="可在七天以内申请。  \n\n第二段。")
    raw = path.read_bytes().decode().replace("\r\n", "\n")
    body = raw[4:].split("\n---\n", 1)[1]
    chunks = parse_documents(tmp_path)
    assert [c.text for c in chunks] == ["可在七天以内申请。", "第二段。"]
    for c in chunks:
        assert body[c.char_start:c.char_end] == c.text
        assert c.content_hash == sha256(c.text.encode()).hexdigest()
