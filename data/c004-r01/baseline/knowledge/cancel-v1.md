---
anchors:
  '1': policy-rule
  '2': simulation-scope
catalog_id: DOC-07
conditions:
  channel: online
  region: CN
contract_version: C-004-v1
doc_id: cancel-v1
effective_date: '2026-10-01'
expires_at: null
historical: false
policy_key: cancel
responsibility: 取消订单（当前业务规则）
rules:
  cancellation_stage: before_shipment
source_type: candidate_synthetic
superseded_by: null
supersedes: []
time_basis: query_date
title: 取消订单
version: 1
---
# 取消订单

## 业务规则 <a id="policy-rule"></a>

尚未发货的订单可申请取消；发货后需按退货流程办理。

## 模拟范围 <a id="simulation-scope"></a>

适用范围：中国大陆线上渠道。本文是候选人为面试构造的模拟电商平台规则，不代表真实法律或真实企业政策。版本按查询日期选择；具体商品条件以规则正文为准。
