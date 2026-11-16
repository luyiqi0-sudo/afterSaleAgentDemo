---
anchors:
  '1': policy-rule
  '2': simulation-scope
catalog_id: DOC-08
conditions:
  channel: online
  region: CN
contract_version: C-004-v1
doc_id: address-v1
effective_date: '2026-10-01'
expires_at: null
historical: false
policy_key: address
responsibility: 收货地址修改（当前业务规则）
rules:
  address_stage: before_shipment
source_type: candidate_synthetic
superseded_by: null
supersedes: []
time_basis: query_date
title: 收货地址修改
version: 1
---
# 收货地址修改

## 业务规则 <a id="policy-rule"></a>

未发货订单可申请修改收货地址；已发货订单需联系物流核实，不能保证修改成功。

## 模拟范围 <a id="simulation-scope"></a>

适用范围：中国大陆线上渠道。本文是候选人为面试构造的模拟电商平台规则，不代表真实法律或真实企业政策。版本按查询日期选择；具体商品条件以规则正文为准。
