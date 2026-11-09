"""Agent 起草知识条目（python -m app.cli kb-draft --source <资料文件> --out <目录> --as <用户>）。

决策记录 2026-11-09：业界知识由知识库建设 Agent 从专家提供的公开资料原文起草，经专家双人审核后入库；
测试环境可用外部模型（经网关，LLM_EXTERNAL=true）。

- 输入只能是公开资料（法律法规、允许使用的标准章节等），先过机密级检测，命中则不发给模型。
- 资料按章节分段（每段不超过 6000 字）交给 Agent；系统提示 = 公共规范 + kb-builder.md + 输出格式要求。
- Agent 按 app/kb/files.py 的条目文件格式输出，平台逐条校验元数据；来源一律记为公开资料，
  来源说明用命令行给的 --source-detail。
- **条款号只能来自原文**：依据类（L4）条目章节标题里的条款号（“第二十一条”“8.1.5”这类），必须在
  这段资料原文里出现，否则整条退回（公共规范第 3 节：不得凭记忆补写条款号）。
- 通过校验的条目写成 <key>.md；没通过的写到 _rejected/ 并注明原因。起草结果只是文件，**不直接入库**：
  专家修改后用 kb-import 导入，仍要双人审核。
- 用量、审计照常记录，不记资料和输出内容。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.orm import Session

from ..agents import registry
from ..core import audit
from ..models import Agent, User
from ..services import gateway, guard, usage
from . import parse
from .files import Entry, EntryError, format_entry, parse_entry

AGENT = "kb-builder"
LEVEL = "reasoning"
SEGMENT_CHARS = 6000
SEPARATOR = "=====条目分隔====="
CLAUSE_RE = re.compile(r"第[一二三四五六七八九十百零〇\d]+条|(?<![\d.])\d+(?:\.\d+){1,4}(?![\d.])")

RULES = f"""

---

# 平台调用说明：从公开资料起草知识条目（本次只起草，不入库）

输入是专家提供的**公开资料原文片段**。请按上面的工作流程，把它加工成若干条知识条目，每条一个“条目文件”：

```
---
key: 小写字母数字和连字符组成的固定标识，如 csl-art21-mlps
title: 简明标题
layer: L1～L5
domain: GOV / CMP / DAT / OPS / RES / ARC
product: SOC 或 GEN
doc_type: TPL / GLD / POL / MTD / REF / MAP / CAS / OBJ / PIT
stage: 启动 / 调研 / 分析 / 设计 / 落地辅导 / 验收 / 通用
capabilities: [管理 / 运营 / 技术支撑 中的一项或几项]
industries: [通用]
source_type: public
summary: 一两句话的摘要
ref_document: （依据类必填）文件名
ref_version: （依据类必填）版本或年份
ref_status: （依据类必填）current / upcoming / repealed
---
## 章节标题（依据类用原文里的条款号开头）

正文：用自己的话做解读和要点归纳，可引用原文中的关键短句，不要整段照抄。
```

硬性要求：
1. 条款号、标准编号、文件名、版本号**只能取自输入原文**；原文里没有的一律不写，不得凭记忆补写。
2. 依据类（L4）条目只解读输入原文中出现的条款；没有条款号的内容写成 L1～L3 的方法或领域知识。
3. 不写任何客户、项目、人员信息。
4. 多个条目之间用单独一行 `{SEPARATOR}` 分隔；除条目外不要输出其他文字。"""


@dataclass
class DraftReport:
    accepted: list[Entry] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)    # (原始输出片段, 原因)
    calls: int = 0
    model: str = ""
    note: str = ""


class DraftError(ValueError):
    pass


def _segments(sections: list[parse.Section]) -> list[str]:
    out, buf = [], ""
    for s in sections:
        block = f"{s.heading}\n{s.text}" if s.heading else s.text
        if buf and len(buf) + len(block) > SEGMENT_CHARS:
            out.append(buf)
            buf = ""
        buf = f"{buf}\n\n{block}" if buf else block
        while len(buf) > SEGMENT_CHARS:
            out.append(buf[:SEGMENT_CHARS])
            buf = buf[SEGMENT_CHARS:]
    if buf.strip():
        out.append(buf)
    return out


def check_clauses(e: Entry, source: str) -> str | None:
    """依据类条目：章节标题里的条款号必须在原文里出现。返回不通过的原因，通过返回 None。"""
    if e.meta.layer != "L4":
        return None
    heads = re.findall(r"^#{1,6}\s+(.+)$", e.content.body, re.M)
    if not heads:
        return "依据类条目没有章节标题（应以原文条款号开头）"
    plain = re.sub(r"\s+", "", source)
    for h in heads:
        for clause in CLAUSE_RE.findall(h):
            if clause not in plain:
                return f"条款号“{clause}”不在提供的资料原文里"
    if e.content.ref_document and re.sub(r"\s+", "", e.content.ref_document) not in plain:
        return f"依据文件名“{e.content.ref_document}”不在提供的资料原文里"
    return None


def draft(db: Session, user: User, filename: str, data: bytes, *, source_detail: str,
          defaults: dict | None = None) -> DraftReport:
    try:
        doc = parse.parse(filename, data)
    except parse.ParseError as e:
        raise DraftError(str(e)) from e
    full = doc.full_text()
    result = guard.check(full)
    if result.blocked:
        raise DraftError(result.summary() + "：资料没有发给模型。公开资料里不应有这类内容，请检查文件")
    agent = db.get(Agent, AGENT)
    if agent is None or not agent.enabled:
        raise DraftError("知识库建设 Agent 未登记或已停用")
    md = registry.load(AGENT)
    system = f"{md.common.strip()}\n\n---\n\n{md.body.strip()}{RULES}"
    report = DraftReport()
    for seg in _segments(doc.sections):
        prompt = f"资料来源：{source_detail}\n\n以下是资料原文片段：\n\n{seg}"
        try:
            out = gateway.complete(prompt, tier="standard", project_code="", level=LEVEL, system=system)
        except gateway.GatewayError as e:
            usage.record(db, username=user.username, project_code="", level=LEVEL, model=gateway.model_for(LEVEL),
                         status="error", stream=False, attempts=e.attempts, agent_code=AGENT)
            audit.record(db, actor=user.username, action="kb.draft_agent", result="error", error=str(e)[:100])
            raise DraftError(f"模型调用失败：{e}") from e
        usage.record(db, username=user.username, project_code="", level=LEVEL, model=out.model, status="ok",
                     stream=False, completion=out, agent_code=AGENT)
        report.calls += 1
        report.model = out.model
        if out.demo:
            report.note = "未接入模型（演示模式），没有起草"
            break
        for chunk in [c.strip() for c in out.text.split(SEPARATOR) if c.strip()]:
            chunk = re.sub(r"^```\w*\n|\n```$", "", chunk.strip())
            try:
                e = parse_entry(chunk)
                for k, v in (defaults or {}).items():
                    if not getattr(e.meta, k, None):
                        setattr(e.meta, k, v)
                e.content.source_type, e.content.source_detail = "public", source_detail
                reason = check_clauses(e, seg)
                if reason:
                    report.rejected.append((chunk, reason))
                else:
                    report.accepted.append(e)
            except EntryError as ex:
                report.rejected.append((chunk, str(ex)))
    audit.record(db, actor=user.username, action="kb.draft_agent", result="allowed", calls=report.calls,
                 accepted=len(report.accepted), rejected=len(report.rejected), model=report.model,
                 source_length=len(full))
    return report


def write_report(report: DraftReport, out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for e in report.accepted:
        p = out_dir / f"{e.key}.md"
        if p.exists():                       # 同名不覆盖专家已改过的文件
            p = out_dir / f"{e.key}.new.md"
        p.write_text(format_entry(e), encoding="utf-8")
        written.append(p)
    if report.rejected:
        rej = out_dir / "_rejected"
        rej.mkdir(exist_ok=True)
        for i, (chunk, reason) in enumerate(report.rejected, 1):
            (rej / f"rejected-{i:03d}.md").write_text(f"<!-- 退回原因：{reason} -->\n\n{chunk}\n", encoding="utf-8")
    return written
