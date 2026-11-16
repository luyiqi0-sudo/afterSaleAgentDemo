---
anchors:
  '1': policy-rule
  '2': simulation-scope
catalog_id: DOC-13
conditions:
  channel: online
  region: CN
contract_version: C-004-v1
doc_id: wrong_item-v1
effective_date: '2026-10-01'
expires_at: null
historical: false
policy_key: wrong_item
responsibility: 错发处理（当前业务规则）
rules:
  verified_shipping_payer: merchant
source_type: candidate_synthetic
superseded_by: null
supersedes: []
time_basis: query_date
title: 错发处理
version: 1
---
# 错发处理

## 业务规则 <a id="policy-rule"></a>

经核实错发商品的，商家承担换回运费；处理前需核对订单和实收商品。

## 模拟范围 <a id="simulation-scope"></a>

适用范围：中国大陆线上渠道。本文是候选人为面试构造的模拟电商平台规则，不代表真实法律或真实企业政策。版本按查询日期选择；具体商品条件以规则正文为准。
