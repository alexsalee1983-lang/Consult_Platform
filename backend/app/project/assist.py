"""写稿助手 v1（三期 A15）：交付件编辑页侧栏的问 / 改，规则只在这里。

- 经报告与汇报 Agent（report-writer，已有读项目区、知识检索授权，不新增工具授权），模型只经 agents/call.py 的 stream；
- 自动上下文：交付件编号与标题、章节标题、编辑中的正文（前端传，未保存的也算；不传取当前版本）、选中的文字；
  提问前按问题检索知识库（knowledge.retrieve，经工具层），命中条目附进系统提示；
- 快慢分档：fast → light 档，deep → reasoning 档，是顾问的显式选择，优先于 md 的 model_tier；
- 问：回答问题；改：只输出改写后的文本，原有来源标注原样保留，新内容按公共规范标注来源；
- 回复里的来源标注按 deliverables.parse_sources 核对后随 done 返回；插入由前端 insertWithSources 原样放到光标处，
  保存仍走交付件写入接口（机密级、严格档占位符、来源核对），签署前照样过质量门禁，助手不绕过任何规则；
- 不建表（迁移号 0045 空号）；审计只记编号、模式、档位、长度、摘要哈希和来源计数，不记问题、正文和回复。
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from ..agents import knowledge, runtime
from ..models import Deliverable, DeliverableSection, Project, User
from . import deliverables as svc

AGENT = "report-writer"
SPEED_LEVEL = {"fast": "light", "deep": "reasoning"}
MODE_NAME = {"ask": "问", "edit": "改"}
CONTEXT_CHARS = 12_000      # 正文过长时只带前后各一半，防止撑爆上下文
SELECTION_CHARS = 4_000

ASK_NOTE = ("请结合本章正文、项目卡和知识库检索结果回答顾问的问题。回答里使用知识库内容注明 [doc_id]，"
            "项目事实注明证据编号；没有依据的注明 [推断]。不得出具或暗示测评、密评、认证结论。")
EDIT_NOTE = ("请按顾问的修改要求改写{target}。只输出改写后的文本（Markdown），不要解释、不要加前后缀；"
             "原文里的来源标注（如 [I-01] [O-05] [L4-…] [推断]）原样保留，新增内容按公共规范标注来源，"
             "没有依据的注明 [推断]。不得出具或暗示测评、密评、认证结论，用“差距分析、符合性初判、整改建议”。")


@dataclass
class Prepared:
    system: str
    message: str
    level: str
    refs: list[dict]
    version: str


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n\n（平台说明：中间省略 {len(text) - limit} 字）\n\n{text[-half:]}"


def build_message(d: Deliverable, s: DeliverableSection, mode: str, prompt: str, context: str, selection: str) -> str:
    lines = [f"（平台说明：写稿助手 · {MODE_NAME[mode]}。交付件 {d.code}《{d.title}》第 {s.seq} 章《{s.title}》。）", ""]
    if mode == "ask":
        lines += [f"顾问的问题：{prompt.strip()}", "", ASK_NOTE]
    else:
        target = "下面“选中的文字”" if selection.strip() else "本章正文"
        lines += [f"顾问的修改要求：{prompt.strip()}", "", EDIT_NOTE.format(target=target)]
    lines += ["", "## 本章正文（编辑中）", _clip(context, CONTEXT_CHARS) or "（本章暂无正文）"]
    if selection.strip():
        lines += ["", "## 选中的文字", _clip(selection, SELECTION_CHARS)]
    return "\n".join(lines)


def prepare(db: Session, user: User, project: Project, d: Deliverable, s: DeliverableSection, *, mode: str,
            prompt: str, context: str | None, selection: str, speed: str) -> Prepared:
    """组装系统提示和提问。Agent 不可用抛 runtime.AgentNotReady，读项目区失败抛 tools.ToolError。"""
    doc = runtime.ready_doc(db, AGENT)
    card = runtime.project_card(db, user, AGENT, project.code)
    if context is None:
        v = svc.get_version(s)
        context = v.body if v else ""
    kb_block, refs = knowledge.retrieve(db, user, AGENT, project, prompt)
    system = runtime.system_prompt(doc, card, kb_block)
    return Prepared(system=system, message=build_message(d, s, mode, prompt, context, selection),
                    level=SPEED_LEVEL[speed], refs=refs, version=doc.version)
