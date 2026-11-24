"""写稿助手 v1（三期 A15）：交付件编辑页侧栏的问 / 改，规则只在这里。

- 经报告与汇报 Agent（report-writer，已有读项目区、知识检索授权，不新增工具授权），模型只经 agents/call.py 的 stream；
- 自动上下文：交付件编号与标题、章节标题、编辑中的正文（前端传，未保存的也算；不传取当前版本）、选中的文字；
  提问前按问题检索知识库（knowledge.retrieve，经工具层），命中条目附进系统提示；
- 快慢分档：fast → light 档，deep → reasoning 档，是顾问的显式选择，优先于 md 的 model_tier；
- 问：回答问题；改：只输出改写后的文本，原有来源标注原样保留，新内容按公共规范标注来源；
- 回复里的来源标注按 deliverables.parse_sources 核对后随 done 返回；插入由前端 insertWithSources 原样放到光标处，
  保存仍走交付件写入接口（机密级、严格档占位符、来源核对），签署前照样过质量门禁，助手不绕过任何规则；
- 四期 A16③ 三个一键工作流（负责人 2026-11-24 同意，#396），不需要顾问写要求，程序先核对、再交模型：
  补来源（cite）：程序找出无来源标注的论断句，按这些句子检索知识库，模型只给这些句子补 [doc_id] / 证据编号，
  找不到依据的句末标“（待补依据）”，其余文字不动；
  口径检查（check）：程序用质量门禁同一套规则找资质边界用语，模型统一术语、改资质用语、核对占位符与严格档用语；
  管理层摘要（summary）：压成不超过 300 字的管理层摘要，保留来源标注，作为新段落插入；
  产出仍走前端的前后对比 + 接受 / 回退（摘要为插入），程序核对结果随 done 的 findings 返回；
- 不建表（迁移号 0045 空号）；审计只记编号、模式、档位、长度、摘要哈希和来源计数，不记问题、正文和回复。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy.orm import Session

from ..agents import knowledge, runtime
from ..models import Deliverable, DeliverableSection, Project, User
from . import deliverables as svc
from . import quality

AGENT = "report-writer"
SPEED_LEVEL = {"fast": "light", "deep": "reasoning"}
MODE_NAME = {"ask": "问", "edit": "改", "cite": "一键补来源", "check": "一键口径检查", "summary": "一键管理层摘要"}
WORKFLOWS = ("cite", "check", "summary")
SUMMARY_CHARS = 300
UNSOURCED_MAX = 30
_TAG = re.compile(r"\[[^\[\]\n]{1,60}\]|【依据[^】]*】|（待补依据）")
_SENTENCE = re.compile(r"[^。！？!?\n]+[。！？!?]?")
_CLAIM_MIN = 8      # 短于这个字数的不当论断（标题、短语）
CONTEXT_CHARS = 12_000      # 正文过长时只带前后各一半，防止撑爆上下文
SELECTION_CHARS = 4_000

ASK_NOTE = ("请结合本章正文、项目卡和知识库检索结果回答顾问的问题。回答里使用知识库内容注明 [doc_id]，"
            "项目事实注明证据编号；没有依据的注明 [推断]。不得出具或暗示测评、密评、认证结论。")
EDIT_NOTE = ("请按顾问的修改要求改写{target}。只输出改写后的文本（Markdown），不要解释、不要加前后缀；"
             "原文里的来源标注（如 [I-01] [O-05] [L4-…] [推断]）原样保留，新增内容按公共规范标注来源，"
             "没有依据的注明 [推断]。不得出具或暗示测评、密评、认证结论，用“差距分析、符合性初判、整改建议”。")


CITE_NOTE = ("任务：给{target}里下面列出的“无来源标注的论断”补来源。能从本章正文、项目卡或知识库检索结果找到依据的，"
             "在句末加来源标注（知识库条目用 [doc_id]，项目事实用证据编号）；找不到依据的，在句末加“（待补依据）”，"
             "不要编造来源。其余文字一字不改，原有标注原样保留。只输出补好来源后的完整文本（Markdown），不要解释。")
CHECK_NOTE = ("任务：对{target}做口径检查并改写：术语前后统一；资质边界用语一律改为“差距分析、符合性初判、整改建议”"
              "（程序已找到的资质边界用语见下）；客户名称只用占位符或客户代号，严格档项目一律用占位符。"
              "只改有问题的地方，来源标注原样保留。只输出改写后的完整文本（Markdown），不要解释。")
SUMMARY_NOTE = ("任务：把{target}压成管理层摘要，不超过 {n} 字，先结论后依据，写主要差距和优先整改建议，"
                "保留关键论断的来源标注，没有依据的注明 [推断]。不得出具或暗示测评、密评、认证结论。"
                "只输出摘要正文（Markdown），不要标题以外的前后缀。")


def unsourced_sentences(text: str) -> list[str]:
    """没有任何来源标注的论断句（去掉标题行和过短的句子），最多 UNSOURCED_MAX 条。"""
    out: list[str] = []
    for line in text.splitlines():
        if line.lstrip().startswith(("#", "|", ">")):
            continue
        for m in _SENTENCE.finditer(line):
            sent = m.group(0).strip(" \t-*·")
            if len(sent) >= _CLAIM_MIN and not _TAG.search(sent):
                out.append(sent)
    return out[:UNSOURCED_MAX]


def qualification_phrases(text: str) -> list[str]:
    """资质边界用语（与签署门禁 quality.qualification_hits 同一规则），去重保序。"""
    return list(dict.fromkeys(quality.qualification_matches(text)))


def findings(mode: str, text: str) -> dict:
    if mode not in WORKFLOWS:
        return {}
    return {"unsourced": len(unsourced_sentences(text)), "qualification": qualification_phrases(text)}


@dataclass
class Prepared:
    system: str
    message: str
    level: str
    refs: list[dict]
    version: str
    findings: dict


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n\n（平台说明：中间省略 {len(text) - limit} 字）\n\n{text[-half:]}"


def build_message(d: Deliverable, s: DeliverableSection, mode: str, prompt: str, context: str, selection: str) -> str:
    lines = [f"（平台说明：写稿助手 · {MODE_NAME[mode]}。交付件 {d.code}《{d.title}》第 {s.seq} 章《{s.title}》。）", ""]
    target = "下面“选中的文字”" if selection.strip() else "本章正文"
    if mode == "ask":
        lines += [f"顾问的问题：{prompt.strip()}", "", ASK_NOTE]
    elif mode in WORKFLOWS:
        text = selection if selection.strip() else context
        note = {"cite": CITE_NOTE, "check": CHECK_NOTE, "summary": SUMMARY_NOTE}[mode]
        lines += [note.format(target=target, n=SUMMARY_CHARS)]
        if prompt.strip():
            lines += ["", f"顾问的补充要求：{prompt.strip()}"]
        if mode == "cite":
            lines += ["", "## 程序找到的无来源标注的论断"] + ([f"- {x}" for x in unsourced_sentences(text)] or ["- （无）"])
        if mode == "check":
            q = qualification_phrases(text)
            lines += ["", "## 程序找到的资质边界用语", "、".join(q) if q else "（无）"]
    else:
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
    text = selection if selection.strip() else context
    query = prompt
    if mode == "cite":       # 按无来源论断检索依据
        query = " ".join(unsourced_sentences(text))[:500] or s.title
    elif mode in WORKFLOWS:
        query = (prompt.strip() or f"{d.title} {s.title}")[:500]
    kb_block, refs = knowledge.retrieve(db, user, AGENT, project, query)
    system = runtime.system_prompt(doc, card, kb_block)
    return Prepared(system=system, message=build_message(d, s, mode, prompt, context, selection),
                    level=SPEED_LEVEL[speed], refs=refs, version=doc.version, findings=findings(mode, text))
