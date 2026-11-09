---
key: pit-default-alert-threshold
title: 踩坑：照搬厂商默认告警阈值
layer: L5
domain: OPS
product: SOC
doc_type: PIT
stage: 设计
capabilities: [运营, 技术支撑]
industries: [通用]
source_type: public
source_detail: 示范条目（开发侧按业界通行做法整理，正式入库前需专家改写或替换）
summary: 直接沿用安全设备的默认告警阈值，会造成告警过多、分析员疲劳，真正的高危告警被淹没。
---

## 现象

上线初期每天产生大量低价值告警，分析员处理不过来，开始批量忽略，一级事件被延误发现。

## 原因

厂商默认阈值按通用环境设定，没有结合客户的资产重要性和业务特点调整。

## 做法

上线前做一到两周的基线观察，按资产重要性分组设置阈值；上线后按规则统计误报率，每月调优一次。
