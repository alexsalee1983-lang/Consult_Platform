"""交付链按知识槽位装配（三期 A12 最小版）。

产品配置的步骤写了 slots（`products/config.py` 的 parse_slots，C10 槽位规格出来后照它填）时，交付链这一步
除了原来按步骤检索词检索（agents/knowledge.py，保留作底），再按槽位逐个检索：
- 每个槽位用自己的检索词、按条目类型（doc_type）经 kb.search 取 need 条，所以沿用 Agent × 工具授权、审计和可见范围；
- 结果按槽位分组附进系统提示；同一条目只附一次，后面的槽位照样算它取到了；
- 摘录按条目类型分额度截断（模板、黄金样例给得多，常见坑、依据给得少），同类型条目平分该类型额度；
- 没取够的槽位在提示里写明“缺的部分标【依据待核实】”；必需槽位没取到的由 runs 发“知识不足”卡；
- 运行记录只记槽位 id、类型、所需条数和取到的 doc_id（report），不记检索词和正文。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from ..kb import meta
from ..models import Project, User
from ..products.config import MAX_SLOTS  # noqa: F401（测试与配置共用上限）
from . import tools

SNIPPET_MAX = 400               # kb.search 返回的摘录上限
TYPE_BUDGET = {"TPL": 1600, "GLD": 1600, "MTD": 1200, "POL": 900, "CAS": 900, "MAP": 800}
DEFAULT_BUDGET = 400            # 其余类型（常见坑、依据、术语等）每个类型的摘录总额度


def budget(doc_type: str, n: int) -> int:
    """某类型有 n 条命中时，每条摘录的字数上限。"""
    return min(SNIPPET_MAX, TYPE_BUDGET.get(doc_type, DEFAULT_BUDGET) // max(n, 1))


@dataclass
class Assembly:
    block: str
    refs: list[dict] = field(default_factory=list)
    report: list[dict] = field(default_factory=list)

    @property
    def missing_required(self) -> list[str]:
        return [r["id"] for r in self.report if r["required"] and not r["ok"]]


def assemble(db: Session, user: User, agent_code: str, project: Project, slots: tuple[dict, ...]) -> Assembly | None:
    """没有槽位或没有 kb.search 授权时返回 None（照旧只用步骤检索）。工具出错抛 tools.ToolError，由交付链处理。"""
    if not slots or "kb.search" not in set(tools.granted_tools(db, agent_code)):
        return None
    hits: dict[str, dict] = {}
    report: list[dict] = []
    for s in slots:
        r = tools.invoke(db, user, agent_code, "kb.search",
                         {"query": s["query"], "doc_type": s["type"], "limit": s["need"]}, project.code)
        got = [h["doc_id"] for h in r.get("items", [])[: s["need"]]]
        for h in r.get("items", [])[: s["need"]]:
            hits.setdefault(h["doc_id"], {**h, "slot": s["id"], "type": s["type"]})
        report.append({"id": s["id"], "type": s["type"], "need": s["need"], "required": s["required"],
                       "got": got, "ok": len(got) >= s["need"]})
    per_type: dict[str, int] = {}
    for h in hits.values():
        per_type[h["type"]] = per_type.get(h["type"], 0) + 1
    lines = ["# 按本步知识清单装配的知识（平台按槽位检索；缺的槽位相关内容标【依据待核实】，不硬写）", ""]
    for s, rep in zip(slots, report, strict=True):
        lines.append(f"## 槽位 {s['id']}（{meta.DOC_TYPES.get(s['type'], s['type'])}，需 {s['need']} 条，"
                     f"取到 {len(rep['got'])} 条）")
        own = [h for h in hits.values() if h["slot"] == s["id"]]
        for h in own:
            head = f"[{h['doc_id']}] {h['title']}" + (f" / {h['heading']}" if h.get("heading") else "")
            lines.append(f"- {head}\n  摘录：{h['snippet'][:budget(h['type'], per_type[h['type']])]}")
        shared = [d for d in rep["got"] if hits[d]["slot"] != s["id"]]
        if shared:
            lines.append(f"- 同上：{'、'.join(shared)}")
        if not rep["ok"]:
            lines.append("- 库里没有足够的已发布条目，本槽位相关内容标【依据待核实】")
        lines.append("")
    refs = [{"doc_id": h["doc_id"], "title": h["title"], "heading": h.get("heading", ""), "layer": h["layer"],
             "citation": h.get("citation", "")} for h in hits.values()]
    return Assembly("\n".join(lines).rstrip(), refs, report)
