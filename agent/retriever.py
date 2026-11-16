"""Source-preserving policy parsing, version adjudication and evidence retrieval."""

import hashlib
import json
import math
import re
from collections import defaultdict
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

from agent.provider import BudgetCallback, ProviderError
from agent.vector_store import CollectionSpec, VectorStore, VectorStoreError


TimeBasis = Literal["query_date", "order_create_date"]
Rules = dict[str, str | int | bool]
ConflictReason = Literal["NO_AUTHORITATIVE_REPLACEMENT", "BODY_RULE_MISMATCH", "EQUIVALENCE_UNVERIFIED"]
_CONDITION_KEYS = {"channel", "region", "tier"}  # tier only if a trusted source supplies it


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    doc_id: str
    file_name: str
    title_path: tuple[str, ...]
    paragraph: int
    char_start: int
    char_end: int
    text: str
    content_hash: str
    document_hash: str
    policy_key: str
    version: str
    effective_date: date
    expires_at: date | None
    conditions: dict[str, str]
    source_type: str
    rules: Rules = field(default_factory=dict)
    time_basis: TimeBasis = "query_date"
    supersedes: tuple[str, ...] = ()
    anchor: str | None = None


def _date(value) -> date:
    return value if type(value) is date else date.fromisoformat(str(value))


def parse_documents(directory: Path, chunk_size: int = 800, overlap: int = 80) -> list[Chunk]:
    if not 0 <= overlap < chunk_size or chunk_size < 100:
        raise ValueError("非法 chunk 长度或 overlap")
    directory = Path(directory)
    if not directory.is_dir():
        raise ValueError("知识目录不存在")
    chunks, identities = [], set()
    for file in sorted(directory.glob("*.md")):
        if file.name.lower() == "readme.md":
            continue
        raw = file.read_text(encoding="utf-8")
        if not raw.startswith("---\n") or "\n---\n" not in raw[4:]:
            raise ValueError(f"{file.name}: 缺少 YAML metadata")
        front, body = raw[4:].split("\n---\n", 1)
        try:
            metadata = yaml.safe_load(front)
        except yaml.YAMLError:
            raise ValueError(f"{file.name}: 无效 YAML metadata") from None
        if (
            not isinstance(metadata, dict)
            or not metadata.get("policy_key")
            or not metadata.get("effective_date")
        ):
            raise ValueError(f"{file.name}: 缺少 policy_key 或 effective_date")
        conditions = metadata.get("conditions", {})
        if not isinstance(conditions, dict) or not all(
            k in _CONDITION_KEYS and isinstance(v, str) and v.strip() for k, v in conditions.items()
        ):
            raise ValueError(f"{file.name}: conditions 必须为受控的字符串键值对")
        conditions = {key: value.strip() for key, value in conditions.items()}
        rules = metadata.get("rules", {})
        if not isinstance(rules, dict) or not all(
            isinstance(k, str) and k and type(v) in (str, int, bool) for k, v in rules.items()
        ):
            raise ValueError(f"{file.name}: rules 必须为键及可比较的简单值")
        time_basis = metadata.get("time_basis", "query_date")
        if time_basis not in ("query_date", "order_create_date"):
            raise ValueError(f"{file.name}: 不支持的 time_basis")
        supersedes = metadata.get("supersedes", [])
        if (not isinstance(supersedes, list) or not all(isinstance(v, str) and v for v in supersedes)
                or len(set(supersedes)) != len(supersedes)):
            raise ValueError(f"{file.name}: 无效 supersedes 列表")
        doc_id = str(metadata.get("doc_id", file.stem))
        if doc_id in identities:
            raise ValueError(f"文档 ID 重复：{doc_id}")
        identities.add(doc_id)
        effective = _date(metadata["effective_date"])
        expires = _date(metadata["expires_at"]) if metadata.get("expires_at") else None
        if expires is not None and expires <= effective:
            raise ValueError(f"{file.name}: 过期时间必须晚于生效时间")
        document_hash = hashlib.sha256(body.strip().encode()).hexdigest()
        headings, paragraph, count, anchor = [], 0, 0, None
        for match in re.finditer(r"\S[\s\S]*?(?=\n\s*\n|\Z)", body):
            block = match.group().strip()
            heading = re.fullmatch(r"(#{1,6})\s+([^\n]+)", block)
            if heading:
                level = len(heading[1])
                headings = [(n, text) for n, text in headings if n < level]
                html_anchor = re.search(r'\s*<a id="([\w-]+)"></a>\s*$', heading[2])
                title = heading[2][:html_anchor.start()] if html_anchor else heading[2]
                headings.append((level, title))
                anchor = html_anchor[1] if html_anchor else None
                continue
            paragraph += 1
            for offset in range(0, len(block), chunk_size - overlap):
                text = block[offset : offset + chunk_size]
                content_hash = hashlib.sha256(text.encode()).hexdigest()
                identity = f"{doc_id}:{document_hash}:{paragraph}:{offset}"
                chunks.append(
                    Chunk(
                        chunk_id=hashlib.sha256(identity.encode()).hexdigest(),
                        doc_id=doc_id,
                        file_name=file.name,
                        title_path=tuple(t for _, t in headings),
                        paragraph=paragraph,
                        char_start=match.start() + offset,
                        char_end=match.start() + offset + len(text),
                        text=text,
                        content_hash=content_hash,
                        document_hash=document_hash,
                        policy_key=str(metadata["policy_key"]),
                        version=str(metadata.get("version", "unspecified")),
                        effective_date=effective,
                        expires_at=expires,
                        conditions=conditions,
                        source_type=str(metadata.get("source_type", "unspecified")),
                        rules=rules,
                        time_basis=time_basis,
                        supersedes=tuple(supersedes),
                        anchor=anchor,
                    )
                )
                count += 1
                if offset + chunk_size >= len(block):
                    break
        if not count:
            raise ValueError(f"{file.name}: 没有政策正文")
    validate_replacements(chunks)
    return chunks


def validate_replacements(chunks: list[Chunk]) -> None:
    docs = {c.doc_id: c for c in chunks}
    seen: set[str] = set()
    active: set[str] = set()

    def visit(doc: Chunk):
        if doc.doc_id in active:
            raise ValueError(f"{doc.doc_id}: supersedes 存在自引用或循环")
        if doc.doc_id in seen:
            return
        active.add(doc.doc_id)
        for previous in doc.supersedes:
            target = docs.get(previous)
            if target is None:
                raise ValueError(f"{doc.doc_id}: supersedes 目标不存在")
            if (target.policy_key != doc.policy_key or target.conditions != doc.conditions
                    or target.time_basis != doc.time_basis or target.effective_date > doc.effective_date):
                raise ValueError(f"{doc.doc_id}: supersedes 主题、范围、日期或时间依据不合法")
            visit(target)
        active.remove(doc.doc_id)
        seen.add(doc.doc_id)

    for doc in docs.values():
        visit(doc)


class PolicyConflictSource(BaseModel):
    doc_id: str
    version: str
    file_name: str
    anchor: str | None
    paragraph: int
    chunk_id: str
    char_start: int
    char_end: int
    content_hash: str
    document_hash: str
    source_type: str
    rules: Rules
    observed_values: dict[str, list[int]]
    observed_units: dict[str, list[str]]
    unbound_duration: bool = False


class PolicyConflictDiagnostic(BaseModel):
    policy_key: str
    reason: ConflictReason
    sources: list[PolicyConflictSource]


# Closed checks for the time quantities in the synthetic policy corpus. They flag
# clear contradictions; numeric agreement alone is not evidence of equivalence.
_QUANTITY_RULES = {
    "returns": (("return_days", "days"), "DAY", r"退货|退回"),
    "exchange": (("exchange_days",), "DAY", r"换货"),
    "warranty": (("warranty_days",), "DAY", r"保修|质保"),
    "invoice": (("issue_business_days",), "BUSINESS_DAY", r"开票|发票|开具"),
    "ticket_response": (("response_hours",), "HOUR", r"响应|回复"),
    "delivery_delay": (("escalate_after_days",), "DAY", r"物流|人工核查"),
}
_WITHIN = r"(?:以内|内)\s*[，,]?\s*"
_MAY = r"(?:(?:可以|可|能|允许)\s*)?"
_DEADLINE_LABEL = r"(?:期限|窗口|时限)\s*(?:(?:为|是)\s*)?[：:，,]?\s*$"
# Each pair describes the words immediately before and after the quantity. An
# empty side places no constraint there. Commas may separate the window from
# its action, but punctuation/topic proximity cannot assign a value by itself.
_DURATION_RELATIONS = {
    "returns": (
        (r"(?:退货|退回)(?:申请)?" + _DEADLINE_LABEL, ""),
        ("", _WITHIN + _MAY + r"(?:(?:申请)?(?:无理由)?退货|提出退货申请|(?:申请)?退回(?:普通)?商品)"),
    ),
    "exchange": (
        (r"换货(?:申请)?(?:期限|窗口|时限)(?:为|是)\s*$", ""),
        ("", _WITHIN + _MAY + r"(?:申请)?(?:规格)?换货"),
    ),
    "warranty": (
        (r"(?:保修|质保)(?:期|期限)(?:为|是)\s*$", ""),
        ("", r"\s*(?:保修|质保)"),
    ),
    "invoice": (
        (r"(?:开票|发票开具)(?:期限|时限)(?:为|是)\s*$", ""),
        ("", _WITHIN + r"(?:可)?开具(?:电子发票)?"),
    ),
    "ticket_response": (
        (r"(?:首次)?(?:响应|回复)(?:期限|时限)(?:为|是)\s*$", ""),
        ("", _WITHIN + r"(?:首次)?(?:响应|回复)"),
        (r"^\s*工单首次响应\s*$", r"\s*$"),
    ),
    "delivery_delay": (
        (r"物流连续\s*$", r"\s*未更新"),
    ),
}
_REVIEW_DURATION_RELATIONS = (
    (r"(?:审核|复核)(?:将|通常|预计)?(?:在|需(?:要)?|用时(?:为)?|时长(?:为|是))\s*$", ""),
    ("", _WITHIN + r"(?:完成|通过)(?:审核|复核)"),
)
_RETURN_PROCESSING_DURATION_RELATIONS = ((r"(?:退货|退回)(?:申请)?处理" + _DEADLINE_LABEL, ""),)
_NUMERALS = "零〇一二两三四五六七八九十百千"
_NUMBER = rf"(?P<number>[0-9]{{1,4}}|[{_NUMERALS}]{{1,8}})"
_UNIT = r"(?P<unit>自然日|工作日|小时|天|日)"
_QUANTITY = re.compile(rf"(?<![0-9.{_NUMERALS}]){_NUMBER}\s*个?\s*{_UNIT}")
_UNIT_KEY = {"自然日": "DAY", "天": "DAY", "日": "DAY", "工作日": "BUSINESS_DAY", "小时": "HOUR"}
_DIGITS = dict(zip("零〇一二两三四五六七八九", (0, 0, 1, 2, 2, 3, 4, 5, 6, 7, 8, 9)))
_MULTIPLIERS = {"十": 10, "百": 100, "千": 1_000}


def _quantity_number(text: str) -> int:
    if text.isascii():
        return int(text)
    if all(char in _DIGITS for char in text):
        return int("".join(str(_DIGITS[char]) for char in text))
    total, digit = 0, None
    for char in text:
        if char in _MULTIPLIERS:
            total += (digit if digit is not None else 1) * _MULTIPLIERS[char]
            digit = None
        else:
            digit = _DIGITS[char]
    return total + (digit or 0)


def _matches_duration_role(relations, before: str, after: str) -> bool:
    return any(re.search(left, before) and re.match(right, after) for left, right in relations)


_DOCUMENT_OVERRIDE = re.compile(
    r"(?:忽略|无视|覆盖|忘记)(?:之前|此前|以上|前面|先前|系统|开发者|用户)(?:的)?"
    r"(?:所有|一切)?(?:指令|命令|提示词|系统消息)", re.I,
)
_QUOTED_SPEECH = re.compile(r'“[^”]*”|「[^」]*」|『[^』]*』|"[^"\n]*"')


def document_override(text: str) -> bool:
    """Recognize direct override wording for display, without deciding rule integrity."""
    unquoted = _QUOTED_SPEECH.sub("〔引文〕", text)
    return any(_DOCUMENT_OVERRIDE.match(sentence.lstrip()) for sentence in re.split(r"[。！？；!?;\n]", unquoted))


def _policy_quantities(chunk: Chunk) -> tuple[dict[str, set[tuple[int, str]]], bool]:
    spec = _QUANTITY_RULES.get(chunk.policy_key)
    if not spec or chunk.anchor == "simulation-scope":
        return {}, False
    names, _, topic = spec
    keys = [name for name in names if type(chunk.rules.get(name)) is int]
    if not keys:
        return {}, False
    measurements = set()
    unbound = False
    for sentence in re.split(r"[。；;！？!?\n]", chunk.text):
        if not re.search(topic, sentence):
            continue
        for m in _QUANTITY.finditer(sentence):
            before, after = sentence[:m.start()], sentence[m.end():].lstrip()
            target = _matches_duration_role(_DURATION_RELATIONS[chunk.policy_key], before, after)
            downstream = _matches_duration_role(_REVIEW_DURATION_RELATIONS, before, after) or (
                chunk.policy_key == "returns"
                and _matches_duration_role(_RETURN_PROCESSING_DURATION_RELATIONS, before, after)
            )
            if target and not downstream:
                measurements.add((_quantity_number(m["number"]), _UNIT_KEY[m["unit"]]))
            elif not downstream or target:
                unbound = True
    return {name: measurements for name in keys if measurements}, unbound


def _contradicts_rules(chunk: Chunk, observed: dict[str, set[tuple[int, str]]]) -> bool:
    if not observed:
        return False
    expected_unit = _QUANTITY_RULES[chunk.policy_key][1]
    return any(values != {(chunk.rules[name], expected_unit)} for name, values in observed.items())


def _checked_paraphrase(doc_chunks: list[Chunk]) -> tuple | None:
    """Recognize only reviewed return-window sentences with no extra qualifications."""
    if len(doc_chunks) != 1 or doc_chunks[0].policy_key != "returns":
        return None
    chunk = doc_chunks[0]
    text = re.sub(r"\s+", "", chunk.text).removesuffix("。")
    matches = list(_QUANTITY.finditer(text))
    if len(matches) != 1 or not _policy_quantities(chunk)[0]:
        return None
    value = matches[0]
    marker = text[:value.start()] + "{quantity}" + text[value.end():]
    if marker not in ("可在{quantity}内申请退货", "{quantity}内可退货"):
        return None
    return chunk.title_path, _quantity_number(value["number"]), _UNIT_KEY[value["unit"]]


def _conflict(eligible: list[Chunk], chunks: list[Chunk]) -> PolicyConflictDiagnostic | None:
    by_doc = {doc.doc_id: [] for doc in eligible}
    for c in chunks:
        if c.doc_id in by_doc:
            by_doc[c.doc_id].append(c)
    quantities = {c.chunk_id: _policy_quantities(c) for rows in by_doc.values() for c in rows}
    observed = {key: result[0] for key, result in quantities.items()}
    unbound = {key for key, result in quantities.items() if result[1]}
    contradictory = {c.chunk_id for rows in by_doc.values() for c in rows
                     if _contradicts_rules(c, observed[c.chunk_id])}
    rule_signatures = {json.dumps(c.rules, sort_keys=True) for c in eligible}
    values_by_rule = defaultdict(set)
    for c in eligible:
        for rule, value in c.rules.items():
            # Canonical JSON distinguishes bool from integer, unlike Python equality.
            values_by_rule[rule].add(json.dumps(value))
    if contradictory:
        reason = "BODY_RULE_MISMATCH"
    elif any(len(values) > 1 for values in values_by_rule.values()):
        reason = "NO_AUTHORITATIVE_REPLACEMENT"
    elif unbound:
        reason = "EQUIVALENCE_UNVERIFIED"
    else:
        exact = len({c.document_hash for c in eligible}) == 1
        paraphrases = {_checked_paraphrase(rows) for rows in by_doc.values()}
        reviewed = len(paraphrases) == 1 and None not in paraphrases
        if len(rule_signatures) == 1 and (exact or reviewed):
            return None
        reason = "EQUIVALENCE_UNVERIFIED"

    sources = []
    for _, rows in sorted(by_doc.items()):
        c = min(rows, key=lambda c: (c.chunk_id not in contradictory, c.chunk_id not in unbound,
                                     not observed[c.chunk_id],
                                     c.anchor != "policy-rule", c.paragraph, c.char_start))
        measurements = observed[c.chunk_id]
        sources.append(PolicyConflictSource(
            doc_id=c.doc_id, version=c.version, file_name=c.file_name, anchor=c.anchor,
            paragraph=c.paragraph, chunk_id=c.chunk_id, char_start=c.char_start, char_end=c.char_end,
            content_hash=c.content_hash, document_hash=c.document_hash, source_type=c.source_type, rules=c.rules,
            observed_values={k: sorted({value for value, _ in pairs}) for k, pairs in measurements.items()},
            observed_units={k: sorted({unit for _, unit in pairs}) for k, pairs in measurements.items()},
            unbound_duration=c.chunk_id in unbound,
        ))
    return PolicyConflictDiagnostic(policy_key=eligible[0].policy_key, reason=reason, sources=sources)


@dataclass
class _Decision:
    chunks: list[Chunk] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    missing: set[str] = field(default_factory=set)
    conflict: PolicyConflictDiagnostic | None = None


def _decision(chunks: list[Chunk], query_date: date, conditions: dict[str, str],
              order_create_date: date | None) -> _Decision:
    docs = {c.doc_id: c for c in chunks}
    children = defaultdict(list)
    for doc in docs.values():
        for older in doc.supersedes:
            children[older].append(doc)
    result = _Decision()
    eligible, replaced, expired = [], [], []
    for doc in docs.values():
        as_of = query_date if doc.time_basis == "query_date" else order_create_date
        if any(k in conditions and conditions[k] != v for k, v in doc.conditions.items()):
            continue
        if as_of is None:
            result.missing.add("order_create_date")
            result.missing.update(doc.conditions.keys() - conditions.keys())
            continue
        if as_of < doc.effective_date:
            continue
        if any(as_of >= successor.effective_date for successor in children[doc.doc_id]):
            replaced.append(doc)
            continue
        if doc.expires_at is not None and as_of >= doc.expires_at:
            expired.append(doc)
            continue
        unknown = doc.conditions.keys() - conditions.keys()
        if unknown:
            result.missing.update(unknown)
            continue
        eligible.append(doc)
    if not docs:
        return result
    theme = chunks[0].policy_key
    if result.missing:
        if "order_create_date" in result.missing:
            result.notes.append(f"{theme} 缺少已核验的下单日期，需要澄清。")
        if result.missing - {"order_create_date"}:
            result.notes.append(f"{theme} 缺少适用条件，需要澄清。")
        return result
    if not eligible:
        if expired:
            result.notes.append(f"{theme} 适用版本已过期，不自动恢复被替代政策。")
        return result

    result.conflict = _conflict(eligible, chunks)
    if result.conflict:
        subject = ", ".join(sorted(c.doc_id for c in eligible))
        why = {
            "NO_AUTHORITATIVE_REPLACEMENT": "适用规则互相冲突且无权威替代关系",
            "BODY_RULE_MISMATCH": "政策正文的时长或单位与结构化规则冲突",
            "EQUIVALENCE_UNVERIFIED": "可能存在规则冲突，正文语义等价性未经核实",
        }[result.conflict.reason]
        if result.conflict.reason == "EQUIVALENCE_UNVERIFIED" and any(
                source.unbound_duration for source in result.conflict.sources):
            why = "政策正文中有时长无法确认对应哪项业务规则"
        result.notes.append(f"{theme}（{subject}）：{why}，无法裁决。")
        return result
    eligible_ids = {c.doc_id for c in eligible}
    result.chunks.extend(c for c in chunks if c.doc_id in eligible_ids and not document_override(c.text))
    if replaced:
        older = ", ".join(sorted(c.doc_id for c in replaced))
        current = ", ".join(sorted(eligible_ids))
        result.notes.append(f"{theme} 已声明的替代关系生效；旧版（{older}）不适用，适用版本（{current}）。")
    return result


def select_versions(
    chunks: list[Chunk], as_of: date, conditions: dict[str, str], *, order_create_date: date | None = None,
) -> tuple[list[Chunk], list[str]]:
    groups = defaultdict(list)
    for chunk in chunks:
        groups[chunk.policy_key].append(chunk)
    selected, notes = [], []
    for _, group in sorted(groups.items()):
        decision = _decision(group, as_of, conditions, order_create_date)
        selected.extend(decision.chunks)
        notes.extend(decision.notes)
    return selected, notes


def retrieval_text(chunk: Chunk) -> str:
    return " / ".join(chunk.title_path) + "\n" + chunk.text


def collection_for(chunks: list[Chunk], model: str, dimension: int) -> CollectionSpec:
    payload = json.dumps([asdict(c) for c in sorted(chunks, key=lambda c: c.chunk_id)],
                         default=str, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    # Hash policy metadata and exact chunks; the tag identifies how embeddings were prepared.
    generation = hashlib.sha256(("policy-index-v1\n" + payload).encode("utf-8")).hexdigest()
    return CollectionSpec(model, dimension, generation)


class PolicyEvidence(BaseModel):
    evidence_id: str
    chunk_id: str
    doc_id: str
    file_name: str
    title_path: tuple[str, ...]
    paragraph: int
    char_start: int
    char_end: int
    anchor: str | None
    text: str
    content_hash: str
    document_hash: str
    policy_key: str
    version: str
    effective_date: date
    expires_at: date | None
    applied_on: date
    conditions: dict[str, str]
    source_type: str
    rules: dict[str, str | int | bool]
    time_basis: TimeBasis
    supersedes: tuple[str, ...]
    score: float
    generation: str


class PolicyReplacement(BaseModel):
    """Historical quotations only; these are never current PolicyEvidence."""
    current_doc_id: str
    previous_doc_id: str
    supersedes_path: list[str]
    sources: list[Chunk]


class PolicyRetrievalResult(BaseModel):
    status: Literal["SUCCESS", "NEED_INPUT", "NO_DATA", "CONFLICT", "RETRIEVAL_UNAVAILABLE"]
    evidences: list[PolicyEvidence] = Field(default_factory=list)
    replacements: list[PolicyReplacement] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    error_code: str | None = None
    conflicts: list[PolicyConflictDiagnostic] = Field(default_factory=list)
    quarantined: list[dict[str, str]] = Field(default_factory=list)


class PolicyRetriever:
    def __init__(self, chunks: list[Chunk], spec: CollectionSpec, store: VectorStore,
                 provider, *, min_score: float = 0.35, top_k: int = 3):
        if not chunks or not 1 <= top_k <= 8 or not -1 <= min_score <= 1:
            raise ValueError("invalid retrieval collection or limits")
        if collection_for(chunks, spec.model, spec.dimension) != spec:
            raise VectorStoreError("COLLECTION_MISMATCH")
        validate_replacements(chunks)
        chunks = deepcopy(chunks)
        self.spec = spec
        self.store = store
        self.provider = provider
        self.min_score = min_score
        self.top_k = top_k
        self._chunks = {c.chunk_id: c for c in chunks}
        self._groups = defaultdict(list)
        for chunk in chunks:
            self._groups[chunk.policy_key].append(chunk)

    async def _scores(self, vector) -> dict[str, float]:
        rows = await self.store.search(self.spec, vector)
        if ({key for key, _ in rows} != self._chunks.keys() or len(rows) != len(self._chunks)
                or not all(math.isfinite(score) and -1.001 <= score <= 1.001 for _, score in rows)):
            raise VectorStoreError("COLLECTION_MISMATCH")
        return dict(rows)

    async def check_index(self) -> None:
        """Verify the selected backend/generation without an external model call."""
        await self._scores([1.0] + [0.0] * (self.spec.dimension - 1))

    def _replacements(self, evidences, group):
        docs = {c.doc_id: c for c in group}
        replacements = []
        for current_id in sorted({e.doc_id for e in evidences}):
            pending, seen = [(current_id, [current_id])], set()
            while pending:
                doc_id, path = pending.pop()
                for previous_id in docs[doc_id].supersedes:
                    if previous_id in seen:
                        continue
                    seen.add(previous_id)
                    previous = docs[previous_id]
                    # Check the complete historical body before quoting it, even
                    # though only current policies participate in adjudication.
                    if _conflict([previous], group):
                        continue
                    sources = [c for c in group if c.doc_id == previous_id
                               and c.anchor != "simulation-scope" and not document_override(c.text)]
                    if not sources:
                        continue
                    chain = path + [previous_id]
                    replacements.append(PolicyReplacement(current_doc_id=current_id,
                                                          previous_doc_id=previous_id,
                                                          supersedes_path=chain, sources=sources))
                    pending.append((previous_id, chain))
        return replacements

    async def retrieve(self, question: str, *, query_date: date, conditions: dict[str, str],
                       order_create_date: date | None = None, budget: BudgetCallback | None = None,
                       provider=None) -> PolicyRetrievalResult:
        try:
            (vector,) = await (provider or self.provider).embed([question], budget=budget)
            scores = await self._scores(vector)
        except (ProviderError, VectorStoreError) as error:
            return PolicyRetrievalResult(status="RETRIEVAL_UNAVAILABLE", error_code=str(error))
        # Enumerate full versions for topical discovery. Scope boilerplate is provenance,
        # not topical evidence. Only the strongest theme is picked for one graph goal.
        theme_scores = {}
        for theme, group in self._groups.items():
            usable = [scores[c.chunk_id] for c in group
                      if c.anchor != "simulation-scope" and not document_override(c.text)]
            if usable:
                theme_scores[theme] = max(usable)
        if not theme_scores:
            return PolicyRetrievalResult(status="NO_DATA")
        topic = min(theme_scores, key=lambda theme: (-theme_scores[theme], theme))
        if theme_scores[topic] < self.min_score:
            return PolicyRetrievalResult(status="NO_DATA")
        group = self._groups[topic]
        quarantined = [{"file_name": c.file_name, "chunk_id": c.chunk_id, "content_hash": c.content_hash,
                        "generation": self.spec.generation}
                       for c in group if document_override(c.text)]
        decision = _decision(group, query_date, conditions, order_create_date)
        if decision.missing:
            return PolicyRetrievalResult(status="NEED_INPUT", missing=sorted(decision.missing), notes=decision.notes)
        if decision.conflict:
            return PolicyRetrievalResult(status="CONFLICT", notes=decision.notes,
                                         conflicts=[decision.conflict], quarantined=quarantined)
        eligible = sorted((c for c in decision.chunks if c.anchor != "simulation-scope"),
                          key=lambda c: (-scores[c.chunk_id], c.chunk_id))
        evidences = []
        for c in eligible[:self.top_k]:
            applied = query_date if c.time_basis == "query_date" else order_create_date
            evidences.append(PolicyEvidence(**asdict(c), evidence_id=c.chunk_id, score=scores[c.chunk_id],
                                            applied_on=applied, generation=self.spec.generation))
        return PolicyRetrievalResult(status="SUCCESS" if evidences else "NO_DATA",
                                     evidences=evidences, replacements=self._replacements(evidences, group),
                                     notes=decision.notes, quarantined=quarantined)
