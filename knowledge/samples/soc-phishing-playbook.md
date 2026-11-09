---
key: soc-phishing-playbook
title: 钓鱼邮件处置剧本要点
layer: L2
domain: RES
product: SOC
doc_type: TPL
stage: 设计
capabilities: [运营]
industries: [通用]
source_type: public
source_detail: 示范条目（开发侧按业界通行做法整理，正式入库前需专家改写或替换）
summary: 钓鱼邮件处置按确认、遏制、清除、恢复、复盘五步进行，重点是快速查清波及范围。
---

## 确认

核对发件人、链接和附件，判断是否为钓鱼；同时检索同一时间段内是否有同类邮件发给其他人。

## 遏制

隔离同类邮件，封禁恶意链接和发件来源；对已点击链接或运行附件的终端进行隔离检查。

## 清除与恢复

清除恶意程序，重置可能泄露的账号口令，确认业务恢复正常。

## 复盘

记录处置时间线，评估是否需要对全员做针对性提醒，并把新发现的特征加入检测规则。
