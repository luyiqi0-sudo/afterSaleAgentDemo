"""Domain topic and requested-dimension support; retrieval success is not coverage.

This is not a general semantic verifier. It covers the declared policy families
with composable anchors and vocabulary, preserving unknown dimensions as gaps.
Only literal source excerpts are delivered; model confidence never completes goals.
"""
from copy import deepcopy
import re

from agent.retriever import document_override
from agent.policy_requests import policy_request


def refund_gaps(goal):
    # A ticket for investigation does not promise that this Agent knows the cause.
    if goal["kind"] in {"ticket", "policy"}:
        return []
    text = goal["description"]
    if not re.search(r"退款|退钱|退了|退到|到账|(?:钱|款).{0,12}(?:退|返)", text):
        return []
    if re.search(r"退款(?:情况|详情|详细情况)", text):
        return ["退款阶段或流水", "退款金额", "退款到账时间", "退款失败原因"]
    checks = {
        "退款金额": r"退款金额|退款(?:是|有)?多少|退了?(?:多少|几多)(?:钱|款)?|退多少钱|应退|已退金额",
        "退款到账时间": r"到账|什么时候.*(?:退|返)|何时.*(?:退|返)|多久.*(?:退|返)|钱.*什么时候",
        "退款失败原因": r"原因|为何|为什么",
        "退款阶段或流水": r"进度|阶段|明细|流水|退到哪|哪一步|受理时间",
    }
    return [label for label, pattern in checks.items() if re.search(pattern, text)]


def unavailable_read_capability(goal):
    question = goal["description"]
    if re.search(r"(?:画(?:出)?|绘制).{0,70}趋势|(?:画(?:出)?|绘制|生成).{0,70}(?:图表|曲线|趋势图)", question):
        return "chart_tool"
    if (not goal.get("subject")
            and re.search(r"(?:我买|我购买|我订购|买过).{0,32}(?:对应|哪个订单|物流)", question)):
        return "order_items"
    return None


def sql_for_goal(goal, data):
    subject, question = goal.get("subject"), goal["description"]
    facts = [fact for fact in data.get("bound_facts", []) if subject is None or fact["entity_id"] == subject]
    requested = set()
    for field, pattern in {"amount": r"金额|总额|多少钱", "status": r"状态", "count": r"数量|几单|多少单|订单数|笔数",
                           "create_time": r"下单时间|创建时间", "update_time": r"更新时间", "logistics_id": r"物流(?:编号|单号)",
                           "stock": r"库存", "price": r"价格|单价"}.items():
        if re.search(pattern, question):
            requested.add(field)
    def field_of(fact):
        return "count" if fact.get("aggregate") == "count" else fact["field"]
    if requested:
        facts = [fact for fact in facts if field_of(fact) in requested]
    present = {field_of(fact) for fact in facts if fact["value"] is not None}
    missing = sorted(requested - present)
    result = deepcopy(data)
    # Per-goal evidence never embeds the other subject's raw rows.
    result.pop("rows", None)
    result["bound_facts"] = facts
    gaps = {}
    if missing:
        gaps["missing_fields"] = missing
    if data.get("projection_gaps"):
        gaps["projection_gaps"] = data["projection_gaps"]
    refund = refund_gaps(goal)
    if refund:
        gaps.update(refund_gaps=refund, capability_gap="refund_facts")
    reason = "UNSUPPORTED" if refund or data.get("projection_gaps") else "NO_DATA" if not facts or missing else None
    return result if facts else None, reason, gaps


# Topic anchors identify the existing business domain, not whole question forms.
# The supported dimensions describe what each source family can actually answer.
# These are NOT answers: the answer still quotes the retrieved, version-adjudicated
# body. No duration, fee, condition or action is manufactured from these entries.
_POLICIES = {
    "accessories": ("配件退回", r"配件|赠品", r"配件.*赠品", {"materials", "eligibility", "process"}),
    "address": ("地址修改", r"地址", r"地址.*(?:修改|核实)|修改.*地址", {"eligibility", "process"}),
    "cancel": ("取消订单", r"取消.*订单|订单.*取消", r"取消.*退货|发货.*取消", {"eligibility", "process"}),
    "confirmation": ("工单确认", r"(?:工单|建单).*(?:确认|同意)|确认.*(?:工单|建单)", r"订单号.*问题描述.*确认", {"materials", "eligibility", "process"}),
    "damaged": ("破损处理", r"破损|摔坏|碎了|损坏.*包装", r"破损.*照片.*包装", {"materials", "process"}),
    "delivery_delay": ("物流延误核查", r"(?:物流|快递|配送).*(?:延误|不更新|未更新|没更新|不动|停了)", r"(?:日|天).*未更新.*核查", {"duration", "eligibility", "process"}),
    "exchange": ("换货", r"换货|换.{0,3}规格", r"(?:换货.*(?:日|天)|(?:日|天).*换货)", {"duration", "eligibility", "process"}),
    "invoice_correction": ("发票更正", r"发票.*(?:错|修改|更正|红冲|作废)", r"发票.*(?:作废|红冲).*重新", {"process", "eligibility"}),
    "invoice_title": ("开票资料", r"(?:开票|发票).*(?:资料|材料|抬头|税号|提供|填写)|(?:企业|公司).*开票|纳税人识别号", r"抬头.*纳税人识别号", {"materials"}),
    "invoice": ("开票时限", r"发票|开票|开具", r"(?:发票|开票).*(?:日|天)|(?:日|天).*开具", {"duration", "eligibility"}),
    "lost_parcel": ("疑似丢件", r"丢件|包裹.*丢|快递.*丢", r"丢件.*核查.*赔偿", {"process", "eligibility", "cost"}),
    "missing_item": ("缺件补发", r"缺件|少.{0,3}(?:配件|商品|东西)|漏发|补发", r"缺件.*照片.*补发", {"materials", "process"}),
    "privacy": ("订单查询身份", r"隐私|认证|(?:别人|他人|本人|他的).*订单|(?:电话|姓名|编号).*查", r"本人订单.*认证", {"eligibility", "process"}),
    "refund_failure": ("退款失败处理", r"退款.*失败|退钱.*失败", r"退款失败.*工单.*流水", {"process"}),
    "refund_method": ("退款渠道", r"退款|退钱", r"原路退回|原支付渠道|原支付账户", {"channel"}),
    "repair": ("维修申请", r"维修|送修", r"维修.*订单号.*故障描述.*(?:保修|检测)", {"materials", "process", "eligibility", "cost"}),
    "return_shipping": ("退货运费", r"退货.*(?:运费|邮费)|(?:运费|邮费).*退货", r"(?:运费|邮费).*承担", {"cost", "eligibility"}),
    "returns": ("退货期限", r"退货|退回|商品.*(?:能|可以)退", r"(?:退货|退回).*(?:天|日)|(?:天|日).*退货", {"duration", "eligibility"}),
    "sealed": ("特殊商品退货", r"拆.{0,2}封|特殊商品|卫生|密封", r"特殊商品.*人工核验.*不直接", {"eligibility", "process"}),
    "service_failure": ("建单超时", r"(?:工单|建单|提交).*(?:超时|没.{0,2}响应|没结果)", r"超时.*未知.*重复", {"process", "eligibility"}),
    "stock": ("库存说明", r"库存|有货|预留", r"库存.*(?:预留|承诺)", {"eligibility"}),
    "ticket_response": ("工单响应", r"工单|建单|客服.*(?:回复|响应|联系)", r"(?:小时|天|日).*(?:响应|回复)|(?:响应|回复).*(?:小时|天|日)", {"duration", "eligibility"}),
    "tracking": ("物流轨迹", r"物流|运输|轨迹|快照", r"快照.*历史.*轨迹", {"eligibility"}),
    "warranty": ("保修", r"保修|质保", r"(?:保修|质保).*(?:天|日|年)|(?:天|日|年).*(?:保修|质保)", {"duration", "eligibility", "cost"}),
    "wrong_item": ("错发商品", r"错发|寄错|发错|收到.*不对", r"错发.*运费.*核对", {"process", "cost", "materials"}),
}

# A requested dimension must have both a capable policy family and a literal
# source signal. Generic topic overlap cannot prove a requested fee/cause/date.
_DIMENSIONS = {
    "duration": (r"多久|多长时间|几天|多少天|几小时|多少小时|(?:几|多少)个?(?:工作日|自然日)|期限|时限|时效|最迟|最晚|什么时候", r"[0-9一二三四五六七八九十百两]+(?:个)?(?:自然日|工作日|天|日|小时|年)"),
    "materials": (r"资料|材料|凭证|凭据|哪些.*(?:内容|提供)|准备什么|提供什么|要带什么", r"抬头|照片|订单号|配件|赠品|故障描述"),
    "cost": (r"(?:运费|邮费).*(?:谁|承担)|谁.*(?:运费|邮费)|免费|赔偿", r"承担|免费|赔偿"),
    "amount": (r"多少钱|多少元|费用.*多少|手续费|金额|税率", r"(?!)"),
    "cause": (r"为什么|为何|失败原因|具体原因|什么原因|原因是什么", r"(?!)"),
    # These require transaction facts. No policy family above claims that
    # capability, even when its body mentions the missing facts as a caveat.
    "arrival": (r"到账|受理(?:时间|日期)", r"(?!)"),
    "transaction_details": (r"进度|阶段|流水|明细|哪一步", r"(?!)"),
    "channel": (r"渠道|原路|退到|退至|账户|方式", r"原路退回|原支付渠道|原支付账户"),
    "process": (r"流程|怎么办|如何处理|怎么处理|如何申请|怎么申请|步骤", r"核查|核实|核验|申请|需|应|先|工单"),
}

# Remaining content words signal an unknown subtopic or qualifier. This is a
# domain vocabulary, not a full-sentence grammar: word order and ordinary
# connective phrasing are free. Unknown terms fail closed instead of being
# silently discarded beside a recognized topic (e.g. tax beside refund).
_DOMAIN_WORDS = """普通商品 标准商品 特殊商品 电子发票 发票 开票 开具 抬头 纳税人识别号 企业 个人 资料 内容
退货 退回 退钱 退款 换货 换回 配件 赠品 规格 维修 送修 保修 质保 订单号 订单 商品 东西 收货地址 地址
售后工单 工单 建单 售后 客服 创建 取消 确认 同意 查询 核查 核实 核验 认证 用户编号 姓名 电话 隐私
快递 配送 物流 运输 历史 轨迹 快照 更新 包裹 丢件 疑似 破损 包装 照片 凭证 补发 缺件 错发
原路 付款 支付 账户 渠道 方式 运费 邮费 承担 质量 问题 个人原因 免费 赔偿 库存 有货 预留 供货
卫生 安全 拆封 密封 发货 签收 收到 收货 购买 买 寄 寄回去 换回去 寄回 换回 退 改 修改 更正 错 失败
响应 回复 联系 规则 规定 政策 期限 窗口 时限 时效 流程 申请 原来 最迟 最多 时间 工作日 自然日 小时 天 日 年
纳税人 税号 少 缺 损坏 摔坏 碎 准备 提供 留 保留 留住 信息 退款失败 重复 提交 超时 作废 红冲
中国大陆 线上 在线 实收 故障 描述 检测 进度 阶段 明细 流水 到账 日期 受理 金额 原因 费用 手续费 税率 商家 货 丢
人工 范围 人为损坏 二次销售 完好 公司 填写 填 材料 证明 凭据 拆过封 标准 首次 套用 普通
""".split()
_GLUE = """请问 想问一下 麻烦 请 说明 告诉 介绍 解释 了解 查询 获取 我想 想 能否 是否 是不是 可以 能够 不能 能 需要 需 应该 应 当 得
一般 通常 常规 当前 目前 最新 现在 一下 一起 直接 实际 准确 完整 具体 正常 最多 最迟 最晚 还 才 就 也 都 会 给 为 由 在 于 从 到
的时候 时候 之后 以后 之前 前 后 自 起 了 的 地 得 吗 呢 啊 吧 是 有 没有 没 不 未 尚未 已经 已
我 我们 你 他的 他 别人 他人 本人 这 这里 那 那个 这些 哪些 哪个 什么 怎样 如何 怎么办 怎么 多久 多长 多少 几 谁 为什么 为何
要 多 好 刚 又 再 一次 已 经 等 通过 成功 满足 影响 不代表 不构成 不保证 代表 保证 个 该 知道 先 时 一直
出去 出来 出 去 回来 回 住 上 下 来 处理 办理 看到 查 找 开 收 发 换 补 漏 退 留 含 包括
连续 接下来 平台 系统 显示 发现 对 不对 过 为了 仍 仍然 一 了 及 与 和 或 以及 并且 同时 另外
""".split()
_WORDS = re.compile("|".join(re.escape(word) for word in sorted(set(_DOMAIN_WORDS + _GLUE), key=len, reverse=True)))
_DOCUMENT_ACTION = re.compile(r"(?:文档|材料|证据)(?:里|中|内)?的.{0,30}(?:指令|命令)(?:要|需要|能|应该|会|是否)?(?:执行|照做)吗?")


def _topics(text):
    keys = {key for key, (_, trigger, _, _) in _POLICIES.items() if re.search(trigger, text)}
    duration = re.search(_DIMENSIONS["duration"][0], text)
    if "invoice_correction" in keys or "invoice_title" in keys and not duration:
        keys.discard("invoice")
    if "sealed" in keys or keys & {"accessories", "return_shipping"} and not duration:
        keys.discard("returns")
    if "missing_item" in keys:
        keys.discard("accessories")
    if "wrong_item" in keys:
        keys.discard("return_shipping")
    if "refund_failure" in keys and not re.search(r"渠道|原路|账户", text):
        keys.discard("refund_method")
    if keys & {"confirmation", "service_failure"} and not duration:
        keys.discard("ticket_response")
    if "delivery_delay" in keys and not re.search(r"轨迹|快照", text):
        keys.discard("tracking")
    return keys


def _unknown_terms(text):
    return re.sub(r"[\W\d_]+", "", _WORDS.sub("", text))


def _replacements_for(selected, data):
    current = {e.get("doc_id"): e for e in selected if e.get("doc_id")}
    result = []
    for item in data.get("replacements", []):
        evidence = current.get(item.get("current_doc_id"))
        path, sources = item.get("supersedes_path", []), item.get("sources", [])
        if (not evidence or len(path) < 2 or path[0] != evidence["doc_id"]
                or path[-1] != item.get("previous_doc_id")
                or path[1] not in evidence.get("supersedes", []) or not sources):
            continue
        source_pattern = _POLICIES[evidence["policy_key"]][2]
        if not any(re.search(source_pattern, source.get("text", "")) for source in sources):
            continue
        # Bind historical quotations to the selected topic, scope and lineage.
        # They never become members of the current-evidence collection.
        if all(source.get("doc_id") == item["previous_doc_id"]
               and source.get("policy_key") == evidence["policy_key"]
               and source.get("conditions") == evidence["conditions"]
               and str(source.get("effective_date", "")) <= str(evidence["effective_date"])
               and not document_override(source.get("text", ""))
               and all(source.get(field) is not None for field in
                       ("text", "file_name", "paragraph", "version", "effective_date", "content_hash", "document_hash"))
               for source in sources):
            result.append(item)
    return result


def policy_for_goal(goal, data):
    question, presentation = policy_request(goal.get("policy_query", {}).get("business_question", goal["description"]))
    evidences = data.get("evidences", [])
    quarantined = [e for e in evidences if document_override(e.get("text", ""))]
    evidences = [e for e in evidences if e not in quarantined]
    topics = _topics(question)
    supported, uncovered, selected = [], [], []
    instruction_question = False
    # Context-only clauses inherit the overall topic; explicit topics remain
    # separate obligations, including two topics requesting the same dimension.
    parts = [p for p in re.split(r"以及|并且|同时|另外|和|与|[；;，,、？?。]", question) if p.strip()]
    for part in parts:
        if _DOCUMENT_ACTION.fullmatch(part.strip()):
            instruction_question = True
            continue
        local = _topics(part) & topics
        keys = local or topics
        dimensions = {name for name, (request, _) in _DIMENSIONS.items() if re.search(request, part)}
        unknown = _unknown_terms(part)
        if not keys or unknown:
            uncovered.append(part.strip())
            # A known topic with an unknown modifier in the SAME obligation is
            # not a proven applicable answer. Other independent units survive.
            continue
        if not dimensions:
            dimensions = {"general"}
        obligations = [(dimension, {key}) for dimension in sorted(dimensions) for key in sorted(local)
                       if dimension == "general" or dimension in _POLICIES[key][3]]
        for dimension in dimensions:
            if not any(item[0] == dimension for item in obligations):
                obligations.append((dimension, keys))
        for dimension, needed_keys in obligations:
            matches = []
            for key in sorted(needed_keys):
                label, _, source_pattern, capabilities = _POLICIES[key]
                if dimension != "general" and dimension not in capabilities:
                    continue
                for evidence in evidences:
                    body = evidence.get("text", "")
                    if (evidence.get("policy_key") == key and re.search(source_pattern, body)
                            and (dimension == "general" or re.search(_DIMENSIONS[dimension][1], body))
                            and all(evidence.get(field) is not None for field in
                                    ("evidence_id", "file_name", "paragraph", "version", "effective_date", "applied_on", "conditions"))):
                        matches.append(evidence)
                        if label not in supported:
                            supported.append(label)
            if not matches:
                labels = {"duration": "时限", "materials": "所需资料", "amount": "具体金额或费用", "cause": "具体原因",
                          "arrival": "到账日期或时间", "transaction_details": "阶段、进度或流水明细"}
                uncovered.append(part.strip() + "（" + labels.get(dimension, dimension) + "）")
            for evidence in matches:
                if evidence not in selected:
                    selected.append(evidence)
    if not supported and not uncovered:
        uncovered = [goal["description"]]
    replacements = _replacements_for(selected, data)
    if "versions" in presentation:
        if replacements:
            supported.append("新旧政策版本对照")
        else:
            uncovered.append("新旧政策差异及版本依据（未取得可核验的历史对照）")
    result = {**deepcopy(data), "evidences": deepcopy(selected), "supported_facets": supported,
              "replacements": deepcopy(replacements)}
    coverage = {"policy_uncovered": uncovered} if uncovered else {}
    if not selected:
        coverage["capability_gap"] = "knowledge_scope"
    if instruction_question or quarantined or data.get("quarantined"):
        coverage["document_actions_ignored"] = True
    return result if selected else None, "NO_DATA" if uncovered else None, coverage


def project_for_goal(action, goal, data):
    if action == "query_sql":
        return sql_for_goal(goal, data)
    if action == "retrieve_policy":
        return policy_for_goal(goal, data)
    gaps = refund_gaps(goal) if action == "query_order" else []
    return deepcopy(data), "UNSUPPORTED" if gaps else None, {
        "refund_gaps": gaps, "capability_gap": "refund_facts",
    } if gaps else {}
