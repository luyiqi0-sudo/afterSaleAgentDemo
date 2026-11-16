---
anchors:
  '1': policy-rule
  '2': simulation-scope
catalog_id: DOC-05
conditions:
  channel: online
  region: CN
contract_version: C-004-v1
doc_id: ticket_response-v2
effective_date: '2026-10-01'
expires_at: null
historical: false
policy_key: ticket_response
responsibility: 售后工单响应时限（当前业务规则）
rules:
  response_hours: 24
source_type: candidate_synthetic
superseded_by: null
supersedes:
- ticket_response-v1
time_basis: query_date
title: 售后工单响应时限
version: 2
---
# 售后工单响应时限

## 业务规则 <a id="policy-rule"></a>

售后工单创建成功后，客服将在二十四小时内首次响应；响应不代表问题已解决。

## 模拟范围 <a id="simulation-scope"></a>

适用范围：中国大陆线上渠道。本文是候选人为面试构造的模拟电商平台规则，不代表真实法律或真实企业政策。版本按查询日期选择；具体商品条件以规则正文为准。
