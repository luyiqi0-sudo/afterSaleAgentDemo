---
anchors:
  '1': policy-rule
  '2': simulation-scope
catalog_id: DOC-10
conditions:
  channel: online
  region: CN
contract_version: C-004-v1
doc_id: invoice_correction-v1
effective_date: '2026-10-01'
expires_at: null
historical: false
policy_key: invoice_correction
responsibility: 发票更正（当前业务规则）
rules:
  correction_process: void_then_reissue
source_type: candidate_synthetic
superseded_by: null
supersedes: []
time_basis: query_date
title: 发票更正
version: 1
---
# 发票更正

## 业务规则 <a id="policy-rule"></a>

错误发票核验后先作废或红冲，再按正确资料重新开具，不直接篡改已开票据。

## 模拟范围 <a id="simulation-scope"></a>

适用范围：中国大陆线上渠道。本文是候选人为面试构造的模拟电商平台规则，不代表真实法律或真实企业政策。版本按查询日期选择；具体商品条件以规则正文为准。
