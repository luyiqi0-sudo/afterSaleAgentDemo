---
anchors:
  '1': policy-rule
  '2': simulation-scope
catalog_id: DOC-26
conditions:
  channel: online
  region: CN
contract_version: C-004-v1
doc_id: returns-v1
effective_date: '2025-01-01'
expires_at: null
historical: true
policy_key: returns
responsibility: 普通商品退货期限（历史对照，仅用于版本冲突测试）
rules:
  return_days: 7
source_type: candidate_synthetic
superseded_by: returns-v2
supersedes: []
time_basis: query_date
title: 普通商品退货期限
version: 1
---
# 普通商品退货期限

## 业务规则 <a id="policy-rule"></a>

在线购买的普通商品，自签收日起七个自然日内可申请无理由退货，商品须完好且未影响二次销售。

## 模拟范围 <a id="simulation-scope"></a>

适用范围：中国大陆线上渠道。本文是候选人为面试构造的模拟电商平台规则，不代表真实法律或真实企业政策。版本按查询日期选择；具体商品条件以规则正文为准。
