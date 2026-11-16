---
anchors:
  '1': policy-rule
  '2': simulation-scope
catalog_id: DOC-29
conditions:
  channel: online
  region: CN
contract_version: C-004-v1
doc_id: return_shipping-v1
effective_date: '2025-01-01'
expires_at: null
historical: true
policy_key: return_shipping
responsibility: 退货运费承担（历史对照，仅用于版本冲突测试）
rules:
  quality_return_payer: customer
source_type: candidate_synthetic
superseded_by: return_shipping-v2
supersedes: []
time_basis: query_date
title: 退货运费承担
version: 1
---
# 退货运费承担

## 业务规则 <a id="policy-rule"></a>

退货运费均由用户承担，包括商品质量问题退货。

## 模拟范围 <a id="simulation-scope"></a>

适用范围：中国大陆线上渠道。本文是候选人为面试构造的模拟电商平台规则，不代表真实法律或真实企业政策。版本按查询日期选择；具体商品条件以规则正文为准。
