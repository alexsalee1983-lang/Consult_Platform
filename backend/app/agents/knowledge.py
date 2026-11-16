"""对话前检索知识库：顾问每次提问，平台先按问题检索，把命中的条目附在 Agent 的系统提示里。

- 只经工具层调用（tools.invoke），所以沿用 Agent × 工具授权和审计：有 kb.search 授权才检索知识，
  有 basis.lookup 授权才另查 L4 依据库。
- 提问命中机密级内容时不检索（网关随后会拦截这条提问）。
- 按项目的服务产品过滤（同时带上通用知识）；只返回已发布、提问人看得到的条目。
- 检索失败不影响对话：没有知识就按公共规范第 3 节标注【依据待核实】。
- 返回给前端和存进消息的引用只有 doc_id、标题、章节、引用格式，不含正文。

这是 S2 的简单做法（检索结果注入上下文）；S3 交付链再让 Agent 自己决定何时调用哪个工具。
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from ..kb import meta
from ..models import Project, User
from ..services import guard
from . import tools

MAX_ITEMS = 5
MAX_BASIS = 3
SNIPPET_CHARS = 300
QUERY_CHARS = 500

log = logging.getLogger(__name__)


def product_code(service: str) -> str:
    """项目的服务产品名称 → 知识库服务产品缩写（如“SOC 运营体系设计”→ SOC）；对不上时不按产品过滤。
    二期 B2 起对话前检索不再用它（工具层按项目配置快照取产品），只留给没有快照的场合。"""
    for code, name in meta.PRODUCTS.items():
        if code != "GEN" and (service == name or name in service or service in name):
            return code
    return ""


def retrieve(db: Session, user: User, agent_code: str, project: Project, message: str) -> tuple[str, list[dict]]:
    """返回 (附加到系统提示的知识段落, 引用列表)。没有授权、没有命中或出错时返回空。"""
    if guard.check(message).blocked:
        return "", []
    granted = set(tools.granted_tools(db, agent_code))
    query = message.strip()[:QUERY_CHARS]
    hits: list[dict] = []
    try:
        if "kb.search" in granted:
            r = tools.invoke(db, user, agent_code, "kb.search",
                             {"query": query, "limit": MAX_ITEMS},   # 产品、模块标签由工具层按项目配置快照填（B2）
                             project.code)
            hits += [{**h, "kind": "knowledge"} for h in r.get("items", [])]
        if "basis.lookup" in granted:
            r = tools.invoke(db, user, agent_code, "basis.lookup", {"query": query, "limit": MAX_BASIS}, project.code)
            hits += [{**h, "kind": "basis"} for h in r.get("items", [])]
    except tools.ToolError as e:          # 检索出错不影响对话
        log.warning("对话前检索失败：%s", type(e).__name__)
        return "", []

    seen: set[str] = set()
    unique = [h for h in hits if not (h["doc_id"] in seen or seen.add(h["doc_id"]))]
    if not unique:
        return "", []
    lines = ["# 知识库检索结果（平台在本次提问前检索，仅供本轮回答参考）", ""]
    for h in unique:
        head = f"[{h['doc_id']}] {h['title']}" + (f" / {h['heading']}" if h.get("heading") else "")
        if h["layer"] == "L4":
            head += f"\n  引用格式：{h['citation']}"
            if h.get("needs_update_check"):
                head += "（复审日期已过，引用时提示“请确认是否有更新”）"
        lines.append(f"- {head}\n  摘录：{h['snippet'][:SNIPPET_CHARS]}")
    refs = [{"doc_id": h["doc_id"], "title": h["title"], "heading": h.get("heading", ""), "layer": h["layer"],
             "citation": h.get("citation", "")} for h in unique]
    return "\n".join(lines), refs
