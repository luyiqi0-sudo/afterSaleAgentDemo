---
anchors:
  '1': policy-rule
  '2': simulation-scope
catalog_id: DOC-14
conditions:
  channel: online
  region: CN
contract_version: C-004-v1
doc_id: delivery_delay-v1
effective_date: '2026-10-01'
expires_at: null
historical: false
policy_key: delivery_delay
responsibility: 配送延误（当前业务规则）
rules:
  escalate_after_days: 3
source_type: candidate_synthetic
superseded_by: null
supersedes: []
time_basis: query_date
title: 配送延误
version: 1
---
# 配送延误

## 业务规则 <a id="policy-rule"></a>

物流连续三个自然日未更新可申请人工核查；预计送达时间应以实际物流信息为依据。

## 模拟范围 <a id="simulation-scope"></a>

适用范围：中国大陆线上渠道。本文是候选人为面试构造的模拟电商平台规则，不代表真实法律或真实企业政策。版本按查询日期选择；具体商品条件以规则正文为准。
