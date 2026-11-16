---
anchors:
  '1': policy-rule
  '2': simulation-scope
catalog_id: DOC-04
conditions:
  channel: online
  region: CN
contract_version: C-004-v1
doc_id: return_shipping-v2
effective_date: '2026-10-01'
expires_at: null
historical: false
policy_key: return_shipping
responsibility: 退货运费承担（当前业务规则）
rules:
  quality_return_payer: merchant
source_type: candidate_synthetic
superseded_by: null
supersedes:
- return_shipping-v1
time_basis: query_date
title: 退货运费承担
version: 2
---
# 退货运费承担

## 业务规则 <a id="policy-rule"></a>

经核验属于商品质量问题的退货运费由商家承担；个人原因退货运费由用户承担。

## 模拟范围 <a id="simulation-scope"></a>

适用范围：中国大陆线上渠道。本文是候选人为面试构造的模拟电商平台规则，不代表真实法律或真实企业政策。版本按查询日期选择；具体商品条件以规则正文为准。
