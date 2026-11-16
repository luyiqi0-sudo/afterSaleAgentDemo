"""Reviewed policy facts for this exercise, not real retailer policies."""

from datetime import date

import yaml

from database.contracts import SOURCE, VERSION, month_bounds

# key, title, rule name, current value, current clause, historical value/clause.
POLICIES = [
    (
        "returns",
        "普通商品退货期限",
        "return_days",
        14,
        "在线购买的普通商品，自签收日起十四个自然日内可申请无理由退货，商品须完好且未影响二次销售。",
        7,
        "在线购买的普通商品，自签收日起七个自然日内可申请无理由退货，商品须完好且未影响二次销售。",
    ),
    (
        "invoice",
        "发票开具时限",
        "issue_business_days",
        5,
        "订单完成且开票资料核验通过后，电子发票在五个工作日内开具。",
        10,
        "订单完成且开票资料核验通过后，电子发票在十个工作日内开具。",
    ),
    (
        "warranty",
        "标准保修期限",
        "warranty_days",
        365,
        "标准商品自签收之日起享有三百六十五个自然日保修；人为损坏不在免费保修范围。",
        180,
        "标准商品自签收之日起享有一百八十个自然日保修；人为损坏不在免费保修范围。",
    ),
    (
        "return_shipping",
        "退货运费承担",
        "quality_return_payer",
        "merchant",
        "经核验属于商品质量问题的退货运费由商家承担；个人原因退货运费由用户承担。",
        "customer",
        "退货运费均由用户承担，包括商品质量问题退货。",
    ),
    (
        "ticket_response",
        "售后工单响应时限",
        "response_hours",
        24,
        "售后工单创建成功后，客服将在二十四小时内首次响应；响应不代表问题已解决。",
        72,
        "售后工单创建成功后，客服将在七十二小时内首次响应；响应不代表问题已解决。",
    ),
    (
        "exchange",
        "换货申请",
        "exchange_days",
        14,
        "签收后十四个自然日内可申请规格换货；实际库存需查库确认。",
        None,
        None,
    ),
    (
        "cancel",
        "取消订单",
        "cancellation_stage",
        "before_shipment",
        "尚未发货的订单可申请取消；发货后需按退货流程办理。",
        None,
        None,
    ),
    (
        "address",
        "收货地址修改",
        "address_stage",
        "before_shipment",
        "未发货订单可申请修改收货地址；已发货订单需联系物流核实，不能保证修改成功。",
        None,
        None,
    ),
    (
        "invoice_title",
        "发票抬头资料",
        "required_fields",
        "title_and_tax_id",
        "企业开票需准确提供企业抬头和纳税人识别号；个人开票需提供个人抬头。",
        None,
        None,
    ),
    (
        "invoice_correction",
        "发票更正",
        "correction_process",
        "void_then_reissue",
        "错误发票核验后先作废或红冲，再按正确资料重新开具，不直接篡改已开票据。",
        None,
        None,
    ),
    (
        "damaged",
        "破损签收处理",
        "photo_required",
        True,
        "发现包装或商品破损时应保留照片及包装，并提供订单号申请售后核验。",
        None,
        None,
    ),
    (
        "missing_item",
        "缺件处理",
        "evidence",
        "packing_photo",
        "收到商品缺件时请提供外包装、内包装和商品照片，客服核验后确定补发方案。",
        None,
        None,
    ),
    (
        "wrong_item",
        "错发处理",
        "verified_shipping_payer",
        "merchant",
        "经核实错发商品的，商家承担换回运费；处理前需核对订单和实收商品。",
        None,
        None,
    ),
    (
        "delivery_delay",
        "配送延误",
        "escalate_after_days",
        3,
        "物流连续三个自然日未更新可申请人工核查；预计送达时间应以实际物流信息为依据。",
        None,
        None,
    ),
    (
        "tracking",
        "物流状态说明",
        "source",
        "latest_snapshot",
        "本系统展示最新物流快照；没有历史事件数据时无法提供完整运输轨迹。",
        None,
        None,
    ),
    (
        "lost_parcel",
        "疑似丢件",
        "resolution",
        "carrier_verification",
        "疑似丢件需由承运商核查，不因单次物流超时自动认定包裹丢失或承诺赔偿。",
        None,
        None,
    ),
    (
        "repair",
        "维修申请",
        "proof",
        "order_and_fault",
        "维修申请需提供订单号和故障描述，是否免费维修以保修范围及检测结果为准。",
        None,
        None,
    ),
    (
        "accessories",
        "配件退回",
        "return_scope",
        "complete_accessories",
        "退货应包含随商品交付的配件与赠品；如有缺失需由售后核实影响。",
        None,
        None,
    ),
    (
        "sealed",
        "特殊商品退货",
        "review_required",
        True,
        "拆封后影响卫生或安全的特殊商品需人工核验，不直接套用普通商品无理由退货规则。",
        None,
        None,
    ),
    (
        "refund_method",
        "退款渠道说明",
        "refund_channel",
        "original_payment",
        "审核通过的退款原则上原路退回；本文仅说明渠道，实际退款金额及进度必须查询退款事实。",
        None,
        None,
    ),
    (
        "refund_failure",
        "退款失败处理",
        "action",
        "suggest_ticket",
        "出现退款失败标记时可建议用户创建工单核查；未取得退款流水不得猜测到账进度。",
        None,
        None,
    ),
    (
        "stock",
        "库存说明",
        "reservation",
        "not_guaranteed_by_query",
        "库存查询只表示查询时刻的可用库存，不构成库存预留或供货承诺。",
        None,
        None,
    ),
    (
        "privacy",
        "用户数据保护",
        "access_scope",
        "own_orders",
        "用户只能查询本人订单；姓名、电话或消息中的用户编号不能替代认证。",
        None,
        None,
    ),
    (
        "confirmation",
        "工单确认规则",
        "write_confirmation",
        True,
        "创建售后工单前应展示订单号和问题描述并取得用户确认；未确认不提交。",
        None,
        None,
    ),
    (
        "service_failure",
        "服务异常说明",
        "unknown_write",
        "manual_reconciliation",
        "建单请求响应超时可能已执行，应说明结果未知并核对，不能盲目重复提交。",
        None,
        None,
    ),
]


def policy_documents(as_of: date) -> list[tuple[str, dict, str]]:
    current_date = month_bounds(as_of)[0].date()
    historical_date = date(current_date.year - 1, 1, 1)
    docs = []
    for index, (key, title, rule, current, clause, old, old_clause) in enumerate(POLICIES, 1):
        versions = [(2 if old is not None else 1, current_date, current, clause, False)]
        if old is not None:
            versions.append((1, historical_date, old, old_clause, True))
        for version, effective, value, text, historical in versions:
            doc_id = f"{key}-v{version}"
            metadata = {
                "doc_id": doc_id,
                "title": title,
                "policy_key": key,
                "version": version,
                "effective_date": effective.isoformat(),
                "expires_at": None,
                "conditions": {"channel": "online", "region": "CN"},
                "rules": {rule: value},
                "historical": historical,
                "source_type": SOURCE,
                "contract_version": VERSION,
                "catalog_id": f"DOC-{25 + index if historical else index:02}",
                "responsibility": title + ("（历史对照，仅用于版本冲突测试）" if historical else "（当前业务规则）"),
                "time_basis": "query_date",
                "supersedes": [f"{key}-v1"] if old is not None and not historical else [],
                "superseded_by": f"{key}-v2" if historical else None,
                "anchors": {"1": "policy-rule", "2": "simulation-scope"},
            }
            body = (f'# {title}\n\n## 业务规则 <a id="policy-rule"></a>\n\n{text}\n\n'
                    '## 模拟范围 <a id="simulation-scope"></a>\n\n'
                    '适用范围：中国大陆线上渠道。本文是候选人为面试构造的模拟电商平台规则，'
                    '不代表真实法律或真实企业政策。版本按查询日期选择；具体商品条件以规则正文为准。\n')
            docs.append((doc_id + ".md", metadata, body))
    return sorted(docs)


def render_document(metadata: dict, body: str) -> str:
    return "---\n" + yaml.safe_dump(metadata, allow_unicode=True, sort_keys=True) + "---\n" + body


def coverage_manifest(documents: list[tuple[str, dict, str]]) -> dict:
    entries, pairs = [], []
    by_id = {meta["doc_id"]: meta for _, meta, _ in documents}
    for name, meta, _ in documents:
        entries.append({**meta, "file": name})
        for old_id in meta["supersedes"]:
            older = by_id[old_id]
            pairs.append({"policy_key": meta["policy_key"], "old_doc_id": old_id,
                          "new_doc_id": meta["doc_id"], "old_rules": older["rules"],
                          "new_rules": meta["rules"], "conditions": meta["conditions"],
                          "overlap_start": meta["effective_date"], "overlap_end": None,
                          "resolution": "explicit_supersedes_on_overlap"})
    return {"source_type": SOURCE, "documents": sorted(entries, key=lambda r: r["catalog_id"]),
            "conflict_pairs": pairs, "numbering": "repository catalog; not G-002 example numbering"}
