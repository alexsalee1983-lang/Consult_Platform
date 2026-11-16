# batch7：NIST 框架与指南（内容线）

来源均为 nvlpubs.nist.gov 官方 PDF，属美国政府作品（公有领域），可以收原文（二期开发计划第十一节“版权分档”）。切分方式为“按章节”（`section`）；框架编号和带点章节号的切分依赖 #148。

| 文件 | 编号 | 用于 | 条数 |
|---|---|---|---|
| CSF 2.0 | NIST CSWP 29 | NSG、ISO 映射 | 103（退回 1） |
| 安全与隐私控制 | SP 800-53 Rev. 5 | NSG、ISO 映射 | 562（退回 3） |
| 事件响应 | SP 800-61 Rev. 3 | IRE、SOC | 10（只切章节；CSF 社区画像表格不切） |
| 零信任架构 | SP 800-207 | ARC | 56（退回 1） |
| 风险评估 | SP 800-30 Rev. 1 | NSG | 26 |
| 风险管理框架 | SP 800-37 Rev. 2 | NSG | 19 |
| 安全软件开发框架 | SP 800-218（SSDF 1.1） | ARC | 39（退回 10） |
| 隐私框架 | CSWP 01162020（1.0） | DSM | 105 |
| AI 风险管理框架 | NIST AI 100-1 | ARC | 95 |

共 9 份，1015 条。

“退回”是被机密级检测命中，主要是映射列里形似 IP 的条款号，例如 `NISTLABEL: 2.2.2.2`。这些条目由 kb-source-publish 转人工审核；guard 不放宽。

版本说明：
- SP 800-53 Rev. 5 的 PDF 为 2020 年 12 月更新版；之后的 5.1.1 / 5.2.0 补丁版只更新在线目录（OSCAL），季度核对（B5）时留意。
- 隐私框架 1.1 截至入库时只是征求意见稿，所以仍收 1.0。

导入（测试机后端容器内）：

```bash
python -m app.cli kb-source-split /srv/knowledge/sources/batch7/manifest.csv --dir /srv/knowledge/sources/batch7 --out /tmp/batch7
python -m app.cli kb-source-publish /tmp/batch7 --as <知识专家>
```
