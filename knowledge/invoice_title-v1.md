---
anchors:
  '1': policy-rule
  '2': simulation-scope
catalog_id: DOC-09
conditions:
  channel: online
  region: CN
contract_version: C-004-v1
doc_id: invoice_title-v1
effective_date: '2026-10-01'
expires_at: null
historical: false
policy_key: invoice_title
responsibility: 发票抬头资料（当前业务规则）
rules:
  required_fields: title_and_tax_id
source_type: candidate_synthetic
superseded_by: null
supersedes: []
time_basis: query_date
title: 发票抬头资料
version: 1
---
# 发票抬头资料

## 业务规则 <a id="policy-rule"></a>

企业开票需准确提供企业抬头和纳税人识别号；个人开票需提供个人抬头。

## 模拟范围 <a id="simulation-scope"></a>

适用范围：中国大陆线上渠道。本文是候选人为面试构造的模拟电商平台规则，不代表真实法律或真实企业政策。版本按查询日期选择；具体商品条件以规则正文为准。
