---
anchors:
  '1': policy-rule
  '2': simulation-scope
catalog_id: DOC-27
conditions:
  channel: online
  region: CN
contract_version: C-004-v1
doc_id: invoice-v1
effective_date: '2025-01-01'
expires_at: null
historical: true
policy_key: invoice
responsibility: 发票开具时限（历史对照，仅用于版本冲突测试）
rules:
  issue_business_days: 10
source_type: candidate_synthetic
superseded_by: invoice-v2
supersedes: []
time_basis: query_date
title: 发票开具时限
version: 1
---
# 发票开具时限

## 业务规则 <a id="policy-rule"></a>

订单完成且开票资料核验通过后，电子发票在十个工作日内开具。

## 模拟范围 <a id="simulation-scope"></a>

适用范围：中国大陆线上渠道。本文是候选人为面试构造的模拟电商平台规则，不代表真实法律或真实企业政策。版本按查询日期选择；具体商品条件以规则正文为准。
