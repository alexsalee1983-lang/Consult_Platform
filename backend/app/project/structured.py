"""四期 A34：交付件结构化输出与程序渲染（D3 事实清单、D7 实施路线图；schema 供 A33 组件页、A36、A39 复用）。

Agent 不再直接写成稿 Markdown，而是交结构化数据（真源），程序校验后渲染成固定章节的表格正文，
再走 deliverables.write_section 的同一套规则（机密级检测、严格档占位符、来源核对、版本只追加）。
Word 导出复用 export.py 对 Markdown 表格的渲染（Word 先行）；PPT / Excel 组件由 A33 从同一真源取。

校验分两级：errors（不写入，工具返回 422）、warnings（照写，返回给 Agent 和界面提示人处理）。
- D3：事实必须带证据编号（I / D / O / S）；判断不进事实表，单列“顾问判断与待确认”；
  单一低可信来源的事实提示待顾问确认；敏感表态剔除记录只收类别和角色，不收原文（schema 禁止多余字段）。
- D7：每条措施四要素（优先级、责任部门、周期、验收标准，公共规范 4.2）缺一 schema 拒绝；
  阶段与起始月一致（短期 1～6、中期 7～18、长期 19～36 月）、结束不超过 36 个月；依赖须存在且不晚于本措施开始；
  每条措施至少映射一项差距；给了差距清单（gap_list）时，未被任何措施覆盖的差距提示。

真源存放：每次结构化写入，在本次新写的第一个章节版本的 sources["structured"] 里存整份数据；
load() 取该交付件最新一份。不加表，不需要迁移。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Deliverable, DeliverableSection, SectionVersion

_EVIDENCE = re.compile(r"^(?:I|D|O|S)-\d{2,4}(?:-\d{1,3})?$")
MAX_ITEMS = 300
HORIZON_MONTHS = 36
PHASES = {"短期": (1, 6), "中期": (7, 18), "长期": (19, 36)}


class StructuredError(ValueError):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# ---- D3 事实清单 ----

class Fact(_Strict):
    id: str = Field(min_length=1, max_length=16)
    statement: str = Field(min_length=1, max_length=500)
    kind: Literal["事实", "判断"] = "事实"
    role: str = Field(default="", max_length=100)                 # 出自哪个角色（不写姓名）
    evidence: list[str] = Field(default_factory=list, max_length=20)
    confidence: Literal["高", "中", "低"] = "中"
    confirmed: bool = False                                       # 顾问已确认（单一低可信来源、矛盾定案）


class EvidenceGap(_Strict):
    item: str = Field(min_length=1, max_length=200)               # 缺什么
    need: str = Field(min_length=1, max_length=300)               # 需要什么证据
    request_to: str = Field(default="", max_length=100)           # 向客户哪个角色要


class Excluded(_Strict):
    category: str = Field(min_length=1, max_length=50)            # 剔除类别；不收原文
    role: str = Field(default="", max_length=100)


class FactSheet(_Strict):
    facts: list[Fact] = Field(min_length=1, max_length=MAX_ITEMS)
    gaps: list[EvidenceGap] = Field(default_factory=list, max_length=MAX_ITEMS)
    excluded: list[Excluded] = Field(default_factory=list, max_length=MAX_ITEMS)


# ---- D7 实施路线图 ----

class Measure(_Strict):
    id: str = Field(min_length=1, max_length=16)
    title: str = Field(min_length=1, max_length=200)
    priority: Literal["高", "中", "低"]
    owner: str = Field(min_length=1, max_length=100)              # 责任部门
    period: str = Field(min_length=1, max_length=50)              # 周期
    acceptance: str = Field(min_length=1, max_length=300)         # 验收标准
    phase: Literal["短期", "中期", "长期"]
    start_month: int = Field(ge=1, le=HORIZON_MONTHS)
    months: int = Field(ge=1, le=HORIZON_MONTHS)
    gaps: list[str] = Field(default_factory=list, max_length=50)  # 对应差距 / 发现编号
    depends: list[str] = Field(default_factory=list, max_length=20)
    resources: str = Field(default="", max_length=200)


class GapRef(_Strict):
    id: str = Field(min_length=1, max_length=32)
    title: str = Field(default="", max_length=200)


class Roadmap(_Strict):
    measures: list[Measure] = Field(min_length=1, max_length=MAX_ITEMS)
    gap_list: list[GapRef] = Field(default_factory=list, max_length=MAX_ITEMS)


SCHEMAS: dict[str, type[_Strict]] = {"D3": FactSheet, "D7": Roadmap}
TITLES: dict[str, list[str]] = {
    "D3": ["事实清单", "顾问判断与待确认", "证据缺口与补证请求", "敏感表态剔除记录"],
    "D7": ["措施清单", "短中长期路线图", "资源估算", "措施与差距映射"],
}


@dataclass
class CheckResult:
    model: _Strict
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _dupes(ids: list[str]) -> list[str]:
    seen: set[str] = set()
    return sorted({i for i in ids if i in seen or seen.add(i)})


def check(code: str, data: object) -> CheckResult:
    code = code.strip().upper()
    schema = SCHEMAS.get(code)
    if schema is None:
        raise StructuredError(422, f"结构化输出目前只支持 {'、'.join(SCHEMAS)}")
    try:
        model = schema.model_validate(data)
    except ValidationError as e:
        first = "; ".join(f"{'.'.join(map(str, x['loc']))}：{x['msg']}" for x in e.errors()[:5])
        raise StructuredError(422, f"{code} 结构化数据不符合格式：{first}") from e
    r = CheckResult(model=model)
    if isinstance(model, FactSheet):
        _check_d3(model, r)
    elif isinstance(model, Roadmap):
        _check_d7(model, r)
    return r


def _check_d3(m: FactSheet, r: CheckResult) -> None:
    if d := _dupes([f.id for f in m.facts]):
        r.errors.append(f"事实编号重复：{'、'.join(d)}")
    for f in m.facts:
        bad = [e for e in f.evidence if not _EVIDENCE.match(e)]
        if bad:
            r.errors.append(f"{f.id} 证据编号格式不对：{'、'.join(bad)}（应为 I-01、I-01-02、D-03、O-05、S-02）")
        if f.kind == "事实" and not f.evidence:
            r.errors.append(f"{f.id} 是事实但没有证据编号；没有证据的请标为“判断”")
        if f.kind == "事实" and len(f.evidence) == 1 and f.confidence == "低" and not f.confirmed:
            r.warnings.append(f"{f.id} 只有单一低可信来源，待顾问确认")


def _check_d7(m: Roadmap, r: CheckResult) -> None:
    ids = {x.id: x for x in m.measures}
    if d := _dupes([x.id for x in m.measures]):
        r.errors.append(f"措施编号重复：{'、'.join(d)}")
    for x in m.measures:
        lo, hi = PHASES[x.phase]
        if not lo <= x.start_month <= hi:
            r.errors.append(f"{x.id} 标为{x.phase}，起始月应在第 {lo}～{hi} 月，现为第 {x.start_month} 月")
        if x.start_month + x.months - 1 > HORIZON_MONTHS:
            r.errors.append(f"{x.id} 结束超过第 {HORIZON_MONTHS} 个月")
        if not x.gaps:
            r.errors.append(f"{x.id} 没有对应差距，每条措施至少映射一项差距")
        for dep in x.depends:
            if dep not in ids:
                r.errors.append(f"{x.id} 依赖的 {dep} 不存在")
            elif ids[dep].start_month > x.start_month:
                r.errors.append(f"{x.id} 依赖的 {dep} 开始得更晚（第 {ids[dep].start_month} 月）")
    if m.gap_list:
        covered = {g for x in m.measures for g in x.gaps}
        missing = [g.id for g in m.gap_list if g.id not in covered]
        if missing:
            r.warnings.append(f"差距 {'、'.join(missing)} 没有措施覆盖")


# ---- 渲染 ----

def _cell(s: str) -> str:
    return s.replace("|", "／").replace("\n", " ").strip() or "—"


def _table(head: list[str], rows: list[list[str]]) -> str:
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    lines += ["| " + " | ".join(_cell(c) for c in row) + " |" for row in rows]
    return "\n".join(lines)


def _refs(codes: list[str]) -> str:
    return " ".join(f"[{c}]" for c in codes)


def render(code: str, model: _Strict) -> list[tuple[str, str]]:
    """渲染成 [(章节标题, Markdown 正文)]，章节固定为 TITLES[code]。"""
    if isinstance(model, FactSheet):
        return _render_d3(model)
    if isinstance(model, Roadmap):
        return _render_d7(model)
    raise StructuredError(422, f"{code} 没有渲染规则")


def _render_d3(m: FactSheet) -> list[tuple[str, str]]:
    facts = [f for f in m.facts if f.kind == "事实"]
    pending = [f for f in m.facts if f.kind == "判断"
               or (len(f.evidence) == 1 and f.confidence == "低" and not f.confirmed)]
    t1 = _table(["编号", "事实", "出自角色", "证据", "可信度", "状态"],
                [[f.id, f.statement, f.role, _refs(f.evidence), f.confidence, "已确认" if f.confirmed else "自动确认"
                  if f.confidence != "低" or len(f.evidence) > 1 else "待确认"] for f in facts]) if facts \
        else "本次没有已证实的事实。"
    t2 = _table(["编号", "内容", "类型", "出自角色", "证据"],
                [[f.id, f.statement, "顾问判断" if f.kind == "判断" else "单一低可信来源", f.role,
                  _refs(f.evidence)] for f in pending]) if pending else "没有待确认的判断。"
    t3 = _table(["缺口", "需要的证据", "向谁要"], [[g.item, g.need, g.request_to] for g in m.gaps]) if m.gaps \
        else "没有证据缺口。"
    t4 = (_table(["类别", "出自角色"], [[x.category, x.role] for x in m.excluded])
          + f"\n\n共剔除 {len(m.excluded)} 条敏感表态，只供内部参考，不进入报告。") if m.excluded \
        else "没有剔除的敏感表态。"
    return list(zip(TITLES["D3"], [t1, t2, t3, t4], strict=True))


def _render_d7(m: Roadmap) -> list[tuple[str, str]]:
    ms = sorted(m.measures, key=lambda x: (x.start_month, x.id))
    t1 = _table(["编号", "措施", "优先级", "责任部门", "周期", "验收标准", "依赖"],
                [[x.id, x.title, x.priority, x.owner, x.period, x.acceptance, "、".join(x.depends)] for x in ms])
    end = max(x.start_month + x.months - 1 for x in ms)
    quarters = list(range(1, (end + 2) // 3 + 1))
    parts = []
    for phase, (lo, hi) in PHASES.items():
        items = [x for x in ms if x.phase == phase]
        if items:
            parts.append(f"### {phase}（第 {lo}～{hi} 月）\n\n" + "\n".join(
                f"- {x.id} {x.title}：第 {x.start_month}～{x.start_month + x.months - 1} 月" for x in items))
    gantt = _table(["措施", *[f"Q{q}" for q in quarters]],
                   [[f"{x.id} {x.title}", *["■" if x.start_month <= q * 3 and x.start_month + x.months - 1 > (q - 1) * 3
                                            else "" for q in quarters]] for x in ms])
    t2 = "\n\n".join(parts) + "\n\n甘特图（按季度）：\n\n" + gantt
    t3 = _table(["编号", "措施", "阶段", "所需资源"], [[x.id, x.title, x.phase, x.resources] for x in ms])
    titles = {g.id: g.title for g in m.gap_list}
    gap_ids = [g.id for g in m.gap_list] or sorted({g for x in ms for g in x.gaps})
    gap_ids += sorted({g for x in ms for g in x.gaps} - set(gap_ids))
    t4 = _table(["差距", "说明", "对应措施"],
                [[g, titles.get(g, ""), "、".join(x.id for x in ms if g in x.gaps) or "未覆盖"] for g in gap_ids])
    return list(zip(TITLES["D7"], [t1, t2, t3, t4], strict=True))


# ---- 真源读取 ----

def load(db: Session, project_id: int, code: str) -> dict | None:
    """取该交付件最新一份结构化真源（没有返回 None）。"""
    rows = db.scalars(select(SectionVersion).join(DeliverableSection).join(Deliverable)
                      .where(Deliverable.project_id == project_id, Deliverable.code == code.strip().upper())
                      .order_by(SectionVersion.id.desc()))
    for v in rows:
        if isinstance(v.sources, dict) and isinstance(v.sources.get("structured"), dict):
            return v.sources["structured"]
    return None
