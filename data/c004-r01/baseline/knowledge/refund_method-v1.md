---
anchors:
  '1': policy-rule
  '2': simulation-scope
catalog_id: DOC-20
conditions:
  channel: online
  region: CN
contract_version: C-004-v1
doc_id: refund_method-v1
effective_date: '2026-10-01'
expires_at: null
historical: false
policy_key: refund_method
responsibility: 退款渠道说明（当前业务规则）
rules:
  refund_channel: original_payment
source_type: candidate_synthetic
superseded_by: null
supersedes: []
time_basis: query_date
title: 退款渠道说明
version: 1
---
# 退款渠道说明

## 业务规则 <a id="policy-rule"></a>

审核通过的退款原则上原路退回；本文仅说明渠道，实际退款金额及进度必须查询退款事实。

## 模拟范围 <a id="simulation-scope"></a>

适用范围：中国大陆线上渠道。本文是候选人为面试构造的模拟电商平台规则，不代表真实法律或真实企业政策。版本按查询日期选择；具体商品条件以规则正文为准。
