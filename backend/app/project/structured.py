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

import math
import re
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Deliverable, DeliverableSection, Project, SectionVersion

_EVIDENCE = re.compile(r"^(?:I|D|O|S)-\d{2,4}(?:-\d{1,3})?$")
MAX_ITEMS = 300
HORIZON_MONTHS = 36
LEVELS = ("高", "中", "低")
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


# ---- D4 差距分析（四期 A36）：发现表是真源，程序写固定段；模型只交总体结论与分域分析 ----

Level = Literal["高", "中", "低", ""]


class FindingRow(_Strict):                                        # 由 with_findings() 从发现表填，不由模型给
    fid: str = Field(min_length=1, max_length=8)
    domain: str = Field(default="", max_length=40)
    item: str = Field(default="", max_length=200)
    current: str = Field(default="", max_length=500)
    conclusion: str = Field(default="", max_length=16)
    evidence: list[str] = Field(default_factory=list, max_length=50)
    impact: Level = ""
    likelihood: Level = ""
    current_score: float | None = None
    target_score: float | None = None
    status: str = Field(default="draft", max_length=16)


class DomainAnalysis(_Strict):
    domain: str = Field(min_length=1, max_length=40)
    analysis: str = Field(min_length=1, max_length=3000)          # 差距与成因
    impact: str = Field(default="", max_length=2000)              # 业务影响
    findings: list[str] = Field(default_factory=list, max_length=100)


class GapAnalysis(_Strict):
    summary: str = Field(min_length=1, max_length=3000)
    domains: list[DomainAnalysis] = Field(default_factory=list, max_length=60)
    findings: list[FindingRow] = Field(min_length=1, max_length=MAX_ITEMS)


# ---- D5 设计方案（四期 A36）：模型给结构化方案，程序校验测算算式并出表 ----

class ModelPart(_Strict):
    title: str = Field(min_length=1, max_length=100)
    text: str = Field(min_length=1, max_length=5000)


class Staffing(_Strict):
    role: str = Field(min_length=1, max_length=100)
    seats: int = Field(ge=0, le=500)                              # 每班席位
    shifts: int = Field(ge=1, le=5)                               # 班次
    factor: float = Field(ge=1, le=3)                             # 轮换系数（休假、培训）
    headcount: int = Field(ge=0, le=5000)
    basis: str = Field(default="", max_length=300)


class Process(_Strict):
    id: str = Field(min_length=1, max_length=16)
    name: str = Field(min_length=1, max_length=100)
    owner: str = Field(default="", max_length=100)
    trigger: str = Field(default="", max_length=200)
    output: str = Field(default="", max_length=200)


class AlertLevel(_Strict):
    level: str = Field(min_length=1, max_length=16)               # 从重到轻排列
    definition: str = Field(min_length=1, max_length=300)
    response_minutes: int = Field(ge=1, le=100000)
    escalation: str = Field(default="", max_length=200)


class Metric(_Strict):
    id: str = Field(min_length=1, max_length=16)
    name: str = Field(min_length=1, max_length=100)
    formula: str = Field(min_length=1, max_length=300)
    target: str = Field(default="", max_length=100)
    frequency: str = Field(default="", max_length=20)


class DesignPlan(_Strict):
    model: list[ModelPart] = Field(min_length=1, max_length=30)
    staffing: list[Staffing] = Field(default_factory=list, max_length=100)
    processes: list[Process] = Field(default_factory=list, max_length=MAX_ITEMS)
    alert_levels: list[AlertLevel] = Field(default_factory=list, max_length=20)
    metrics: list[Metric] = Field(default_factory=list, max_length=MAX_ITEMS)


# ---- D6 制度包（四期 A36）：程序套公文骨架（第一章 / 第一条），模型起草条款 ----

class Chapter(_Strict):
    title: str = Field(min_length=1, max_length=50)
    articles: list[str] = Field(min_length=1, max_length=100)


class Policy(_Strict):
    id: str = Field(min_length=1, max_length=16)
    title: str = Field(min_length=1, max_length=100)
    level: str = Field(default="", max_length=20)                 # 客户制度层级
    action: Literal["新建", "修订", "保留"]
    old_title: str = Field(default="", max_length=100)
    chapters: list[Chapter] = Field(default_factory=list, max_length=30)
    gaps: list[str] = Field(default_factory=list, max_length=50)
    forms: list[str] = Field(default_factory=list, max_length=30)


class Form(_Strict):
    id: str = Field(min_length=1, max_length=16)
    name: str = Field(min_length=1, max_length=100)
    policy: str = Field(min_length=1, max_length=16)
    fields: list[str] = Field(default_factory=list, max_length=60)


class PolicyPack(_Strict):
    policies: list[Policy] = Field(min_length=1, max_length=100)
    forms: list[Form] = Field(default_factory=list, max_length=MAX_ITEMS)


SCHEMAS: dict[str, type[_Strict]] = {"D3": FactSheet, "D4": GapAnalysis, "D5": DesignPlan, "D6": PolicyPack,
                                     "D7": Roadmap}
TITLES: dict[str, list[str]] = {
    "D3": ["事实清单", "顾问判断与待确认", "证据缺口与补证请求", "敏感表态剔除记录"],
    "D4": ["总体结论", "发现表", "成熟度初判", "差距热力图", "分域分析"],
    "D5": ["目标运营模型", "组织与人员测算", "流程清单", "告警分级", "指标体系"],
    "D6": ["制度文件", "配套表单", "新旧制度对照", "制度与差距映射"],
    "D7": ["措施清单", "短中长期路线图", "资源估算", "措施与差距映射"],
}
# 资质边界（CLAUDE.md）：不出具或暗示测评、密评、认证结论
_QUAL = re.compile(r"(?:通过|满足|达到)了?(?:等保|等级保护|密评|认证)|(?:测评|认证|密评)结论|等级(?:保护)?测评(?:合格|通过)")


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
    elif isinstance(model, GapAnalysis):
        _check_d4(model, r)
    elif isinstance(model, DesignPlan):
        _check_d5(model, r)
    elif isinstance(model, PolicyPack):
        _check_d6(model, r)
    return r


def _qual(where: str, *texts: str, r: CheckResult) -> None:
    if any(_QUAL.search(t) for t in texts):
        r.errors.append(f"{where} 出现测评 / 认证结论用语，只能写差距分析、符合性初判、整改建议")


def _check_d4(m: GapAnalysis, r: CheckResult) -> None:
    fids = {f.fid for f in m.findings}
    _qual("总体结论", m.summary, r=r)
    for d in m.domains:
        _qual(f"分域分析“{d.domain}”", d.analysis, d.impact, r=r)
        if bad := [x for x in d.findings if x not in fids]:
            r.errors.append(f"分域分析“{d.domain}”引用的发现 {'、'.join(bad)} 不在发现表里")
    if drafts := [f.fid for f in m.findings if f.status == "draft"]:
        r.warnings.append(f"发现 {'、'.join(drafts)} 尚未经顾问确认")
    covered = {x for d in m.domains for x in d.findings}
    if missing := [f.fid for f in m.findings if f.fid not in covered]:
        r.warnings.append(f"发现 {'、'.join(missing)} 没有在分域分析里展开")


def _need(x: Staffing) -> int:
    return math.ceil(round(x.seats * x.shifts * x.factor, 6))


def _check_d5(m: DesignPlan, r: CheckResult) -> None:
    for p in m.model:
        _qual(f"目标运营模型“{p.title}”", p.text, r=r)
    for x in m.staffing:
        if x.headcount != _need(x):
            r.errors.append(f"{x.role} 人数算错：席位 {x.seats} × 班次 {x.shifts} × 轮换系数 {x.factor:g} "
                            f"向上取整应为 {_need(x)}，现为 {x.headcount}")
    for label, ids in (("流程", [p.id for p in m.processes]), ("指标", [k.id for k in m.metrics]),
                       ("告警级别", [a.level for a in m.alert_levels])):
        if d := _dupes(ids):
            r.errors.append(f"{label}编号重复：{'、'.join(d)}")
    for a, b in zip(m.alert_levels, m.alert_levels[1:], strict=False):
        if b.response_minutes < a.response_minutes:
            r.errors.append(f"告警级别 {b.level} 比更重的 {a.level} 响应时限还短（{b.response_minutes} < "
                            f"{a.response_minutes} 分钟），级别应从重到轻排列")


def _check_d6(m: PolicyPack, r: CheckResult) -> None:
    pids = {p.id for p in m.policies}
    fids = {f.id for f in m.forms}
    if d := _dupes([p.id for p in m.policies]):
        r.errors.append(f"制度编号重复：{'、'.join(d)}")
    if d := _dupes([f.id for f in m.forms]):
        r.errors.append(f"表单编号重复：{'、'.join(d)}")
    for p in m.policies:
        if p.action == "修订" and not p.old_title:
            r.errors.append(f"{p.id} 是修订，须写原制度名称（old_title）")
        if p.action != "保留" and not p.chapters:
            r.errors.append(f"{p.id} 是{p.action}，须给章节和条款")
        if p.action == "保留" and p.chapters:
            r.warnings.append(f"{p.id} 标为保留，给的条款不进正文")
        if p.action != "保留" and not p.gaps:
            r.warnings.append(f"{p.id} 没有对应差距")
        if bad := [x for x in p.forms if x not in fids]:
            r.errors.append(f"{p.id} 引用的表单 {'、'.join(bad)} 不存在")
        _qual(p.id, *(a for c in p.chapters for a in c.articles), r=r)
    for f in m.forms:
        if f.policy not in pids:
            r.errors.append(f"表单 {f.id} 所属制度 {f.policy} 不存在")


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
    if isinstance(model, GapAnalysis):
        return _render_d4(model)
    if isinstance(model, DesignPlan):
        return _render_d5(model)
    if isinstance(model, PolicyPack):
        return _render_d6(model)
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


def _render_d4(m: GapAnalysis) -> list[tuple[str, str]]:
    fs = m.findings
    t1 = _table(["编号", "能力域", "要求要点", "现状", "证据", "符合性初判", "影响", "可能性"],
                [[f.fid, f.domain, f.item, f.current, _refs(f.evidence), f.conclusion, f.impact, f.likelihood]
                 for f in fs])
    by_dom: dict[str, list[FindingRow]] = {}
    for f in fs:
        by_dom.setdefault(f.domain or "未分域", []).append(f)
    rows = []
    for dom, items in by_dom.items():
        cur = [f.current_score for f in items if f.current_score is not None]
        tgt = [f.target_score for f in items if f.target_score is not None]
        if cur and tgt:
            rows.append([dom, f"{sum(cur) / len(cur):.1f}", f"{sum(tgt) / len(tgt):.1f}",
                         "、".join(f.fid for f in items)])
    t2 = (_table(["能力域", "现状初判", "目标初判", "依据（发现）"], rows)
          + "\n\n分数为差距分析的成熟度初判，不是测评或认证结论。") if rows else "发现表没有分数，本项目不给成熟度初判。"
    grid = [[lv, *[str(sum(1 for f in fs if f.impact == lv and f.likelihood == lk)) for lk in LEVELS]]
            for lv in LEVELS]
    concl = sorted({f.conclusion for f in fs if f.conclusion})
    dom_rows = [[dom, *[str(sum(1 for f in items if f.conclusion == c)) for c in concl]]
                for dom, items in by_dom.items()]
    t3 = "影响 × 可能性（发现条数）：\n\n" + _table(["影响＼可能性", *LEVELS], grid)
    if concl:
        t3 += "\n\n各能力域符合性初判分布：\n\n" + _table(["能力域", *concl], dom_rows)
    t4 = "\n\n".join(f"### {d.domain}\n\n{d.analysis}" + (f"\n\n业务影响：{d.impact}" if d.impact else "")
                     + (f"\n\n对应发现：{_refs(d.findings)}" if d.findings else "") for d in m.domains) \
        or "（分域分析待补）"
    return list(zip(TITLES["D4"], [m.summary, t1, t2, t3, t4], strict=True))


def _render_d5(m: DesignPlan) -> list[tuple[str, str]]:
    t1 = "\n\n".join(f"### {p.title}\n\n{p.text}" for p in m.model)
    t2 = (_table(["岗位", "每班席位", "班次", "轮换系数", "人数", "测算依据"],
                 [[x.role, str(x.seats), str(x.shifts), f"{x.factor:g}", str(x.headcount), x.basis]
                  for x in m.staffing])
          + f"\n\n人数 = 席位 × 班次 × 轮换系数，向上取整；合计 {sum(x.headcount for x in m.staffing)} 人。") \
        if m.staffing else "本方案不涉及人员测算。"
    t3 = _table(["编号", "流程", "责任方", "触发", "输出"],
                [[p.id, p.name, p.owner, p.trigger, p.output] for p in m.processes]) if m.processes \
        else "（流程清单待补）"
    t4 = _table(["级别", "定义", "响应时限（分钟）", "升级"],
                [[a.level, a.definition, str(a.response_minutes), a.escalation] for a in m.alert_levels]) \
        if m.alert_levels else "本方案不涉及告警分级。"
    t5 = _table(["编号", "指标", "计算方式", "目标值", "频率"],
                [[k.id, k.name, k.formula, k.target, k.frequency] for k in m.metrics]) if m.metrics \
        else "（指标体系待补）"
    return list(zip(TITLES["D5"], [t1, t2, t3, t4, t5], strict=True))


_DIGITS = "零一二三四五六七八九"


def chinese_numeral(n: int) -> str:
    """1～999 的中文数字（公文“第一章 / 第二十三条”用），其余原样返回阿拉伯数字。"""
    if not 0 < n < 1000:
        return str(n)
    h, t, u = n // 100, n // 10 % 10, n % 10
    out = f"{_DIGITS[h]}百" if h else ""
    if t:
        out += ("" if t == 1 and not h else _DIGITS[t]) + "十"
    elif h and u:
        out += "零"
    return out + (_DIGITS[u] if u else "")


def _render_d6(m: PolicyPack) -> list[tuple[str, str]]:
    docs = []
    for p in m.policies:
        if p.action == "保留":                     # 保留的制度不重写正文，只进对照表
            continue
        lines, n = [f"### {p.id} {p.title}" + (f"（{p.level}）" if p.level else "")], 0
        for ci, c in enumerate(p.chapters, 1):
            lines.append(f"**第{chinese_numeral(ci)}章 {c.title}**")
            for a in c.articles:                    # 条号跨章连续（公文惯例）
                n += 1
                lines.append(f"第{chinese_numeral(n)}条 {a}")
        docs.append("\n\n".join(lines))
    t1 = "\n\n".join(docs) or "本次没有新建或修订的制度。"
    t2 = _table(["编号", "表单", "所属制度", "字段"], [[f.id, f.name, f.policy, "、".join(f.fields)] for f in m.forms]) \
        if m.forms else "没有配套表单。"
    t3 = _table(["原制度", "新制度", "处理", "层级"],
                [[p.old_title or "（无）", p.title, p.action, p.level] for p in m.policies])
    gaps: dict[str, list[str]] = {}
    for p in m.policies:
        for g in p.gaps:
            gaps.setdefault(g, []).append(f"{p.id} {p.title}")
    t4 = _table(["差距", "对应制度"], [[g, "、".join(v)] for g, v in sorted(gaps.items())]) if gaps \
        else "没有制度与差距的映射。"
    return list(zip(TITLES["D6"], [t1, t2, t3, t4], strict=True))


def with_findings(db: Session, project: Project, data: object) -> object:
    """D4：发现表一律取真源（app/project/findings.py），覆盖模型给的 findings。"""
    from . import findings as fsvc                 # 延迟导入免循环

    if not isinstance(data, dict):
        return data
    rows = [{"fid": f.fid, "domain": f.domain, "item": f.item, "current": f.current, "conclusion": f.conclusion,
             "evidence": [str(e) for e in (f.evidence_refs or [])],
             "impact": f.impact if f.impact in LEVELS else "", "likelihood": f.likelihood if f.likelihood in LEVELS else "",
             "current_score": f.current_score, "target_score": f.target_score, "status": f.status}
            for f in fsvc.list_for_project(db, project)]
    return {**data, "findings": rows}


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
