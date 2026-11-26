"""四期 A14：D1、D2 表单与自动成稿。

表单定义放在 `products/forms/<交付件编号>.yaml`（各产品通用）。顾问填表提交后，平台按模板**程序成稿**（不调模型）：
设置大纲，再把有字段的章节逐章写成第 1 版；没有字段的章节只进大纲、留给 Agent 起草（如 D2 访谈提纲）。
- 模板用 `{key}` 引用字段，按空行分段；一段里引用的字段全为空就整段去掉。list 字段每行一项，渲染为 Markdown 列表。
- `fixed` 段落由平台固定写入（资质边界、资料红线），顾问不能改；`fixed_by_tier` 按项目档位追加固定段落（#468 客户数据保护安排）。
- 字段 `prefill` 取项目产品配置作表单初值（frameworks 评估基线框架、deliverables 交付件清单），顾问可改。
- A28①：`table` 字段带 `columns`，每行一条、格间用全角竖线“｜”，成稿为 Markdown 表格；`default` 为公司标准写法初值；
  prefill 另可取项目与成员（period 周期、milestones 按产品交付件阶段排周次、roles 项目成员），顾问只需填目标与范围。
- 只在交付件还没有任何正文时可用（避免覆盖顾问或 Agent 已写的内容）；之后在编辑器里改。
- 写入前整份先过机密级检测和严格档占位符检查，任何一章不过都不写；之后走 deliverables.set_outline / write_section，
  版本、审计（不记原文）、判断数据埋点与手写一致。
"""
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml
from sqlalchemy.orm import Session

from ..models import Deliverable, Project, User
from ..products.config import catalog_dir
from . import deliverables as dsvc

FIELD_TYPES = ("text", "textarea", "list", "table")
PREFILLS = ("frameworks", "deliverables", "period", "milestones", "roles")
TIERS = ("standard", "strict")
MAX_FIELD = {"text": 200, "textarea": 2000, "list": 4000, "table": 4000}
MAX_LIST_ITEMS = 50
NOTE = "表单成稿"
_REF = re.compile(r"\{([a-z_][a-z0-9_]*)\}")


class FormError(ValueError):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    type: str
    required: bool = False
    hint: str = ""
    prefill: str = ""
    columns: tuple[str, ...] = ()
    default: str = ""
    example: str = ""     # 填写示例（只用虚拟客户），显示在输入框下方供顾问参考


@dataclass(frozen=True)
class Section:
    title: str
    fields: tuple[Field, ...] = ()
    template: str = ""
    fixed: str = ""
    fixed_by_tier: dict[str, str] = field(default_factory=dict)

    def fixed_for(self, tier: str) -> str:
        return "\n\n".join(x for x in (self.fixed, self.fixed_by_tier.get(tier, "")) if x)


@dataclass(frozen=True)
class Form:
    code: str
    title: str
    sections: tuple[Section, ...]

    def fields(self) -> list[Field]:
        return [f for s in self.sections for f in s.fields]


def _parse(data: dict, code: str) -> Form:
    """校验表单定义；写错直接报错（配置问题在测试和启动时暴露，不留到顾问提交时）。"""
    if not isinstance(data, dict) or data.get("code") != code:
        raise ValueError(f"forms/{code}.yaml 的 code 应为 {code}")
    seen: set[str] = set()
    sections = []
    for s in data.get("sections") or []:
        fields = []
        for f in s.get("fields") or []:
            key, ftype = str(f.get("key", "")), str(f.get("type", "text"))
            if not _REF.fullmatch("{" + key + "}") or key in seen or ftype not in FIELD_TYPES:
                raise ValueError(f"forms/{code}.yaml 字段 {key!r} 的 key 重复、不合法或 type 不在 {FIELD_TYPES}")
            prefill = str(f.get("prefill") or "")
            if prefill and prefill not in PREFILLS:
                raise ValueError(f"forms/{code}.yaml 字段 {key!r} 的 prefill 应为 {PREFILLS} 之一")
            columns = tuple(str(c) for c in f.get("columns") or ())
            if (ftype == "table") != bool(columns):
                raise ValueError(f"forms/{code}.yaml 字段 {key!r}：table 要写 columns，其他类型不写")
            seen.add(key)
            fields.append(Field(key, str(f.get("label") or key), ftype, bool(f.get("required")), str(f.get("hint") or ""),
                                prefill, columns, str(f.get("default") or "").strip(), str(f.get("example") or "").strip()))
        template = str(s.get("template") or "")
        unknown = set(_REF.findall(template)) - {f.key for f in fields}
        if unknown or (fields and not template):
            raise ValueError(f"forms/{code}.yaml 章节 {s.get('title')!r} 的模板缺失或引用了本章没有的字段 {sorted(unknown)}")
        by_tier = s.get("fixed_by_tier") or {}
        if not isinstance(by_tier, dict) or set(by_tier) - set(TIERS):
            raise ValueError(f"forms/{code}.yaml 章节 {s.get('title')!r} 的 fixed_by_tier 只能按 {TIERS} 分档")
        sections.append(Section(str(s.get("title") or "").strip(), tuple(fields), template, str(s.get("fixed") or "").strip(),
                                {k: str(v or "").strip() for k, v in by_tier.items()}))
    if not sections or any(not s.title for s in sections):
        raise ValueError(f"forms/{code}.yaml 至少要有一章，且每章要有标题")
    return Form(code, str(data.get("title") or code), tuple(sections))


@lru_cache(maxsize=16)
def _load(path: str, mtime: float, code: str) -> Form:
    return _parse(yaml.safe_load(Path(path).read_text(encoding="utf-8")), code)


def load_form(code: str) -> Form | None:
    if not re.fullmatch(r"D\d", code or ""):
        return None
    path = catalog_dir() / "products" / "forms" / f"{code}.yaml"
    if not path.is_file():
        return None
    return _load(str(path), path.stat().st_mtime, code)


STAGE_WEIGHT = {"启动": 1, "调研": 2, "分析": 2, "设计": 3, "落地辅导": 3, "验收": 1}   # 排周次用的相对工作量


def stage_weeks(product: object | None, total_weeks: int) -> dict[str, str]:
    """按产品交付件出现的阶段（meta.STAGES 顺序）和相对工作量切分项目周期，如 {"启动": "第 1 周", "调研": "第 2–4 周"}。"""
    present = {getattr(d, "stage", "") for d in getattr(product, "deliverables", ())}
    stages = [s for s in STAGE_WEIGHT if s in present]
    total, units = max(total_weeks, len(stages), 1), sum(STAGE_WEIGHT[s] for s in stages)
    out: dict[str, str] = {}
    acc = done = 0
    for s in stages:
        acc += STAGE_WEIGHT[s]
        end = max(done + 1, round(acc * total / units))
        out[s] = f"第 {done + 1} 周" if end == done + 1 else f"第 {done + 1}–{end} 周"
        done = end
    return out


def prefill_values(product: object | None, project: Project | None = None,
                   members: Sequence[tuple[str, str]] = ()) -> dict[str, str]:
    """表单初值：产品配置（评估基线框架、交付件、按阶段排的里程碑）、项目周期、项目成员 [(显示名, 项目角色名)]；
    拿不到的项为空，顾问照常填。"""
    out: dict[str, str] = {}
    weeks = project.total_weeks if project is not None and project.total_weeks else 12
    if project is not None:
        out["period"] = f"共 {weeks} 周（起止日期以合同约定为准）"
    if product is not None:
        dl = list(getattr(product, "deliverables", ()))
        when = stage_weeks(product, weeks)
        out["frameworks"] = "\n".join(getattr(product, "frameworks", ()))
        out["deliverables"] = "\n".join(f"{d.code} {d.title}｜{when.get(d.stage, '')}｜客户项目负责人｜书面确认" for d in dl)
        out["milestones"] = "\n".join(f"{s}｜{w}｜{'、'.join(d.title for d in dl if d.stage == s)}" for s, w in when.items())
    if members:
        rows = [f"我方｜{n}（{r}）｜{'统筹进度、质量与客户沟通，签署交付件' if r == '项目经理' else '调研、分析与交付件编写'}"
                for n, r in members]
        out["roles"] = "\n".join(rows + ["客户方｜项目负责人（待填）｜确认范围与计划、协调资源、组织验收",
                                         "客户方｜项目对接人（待填）｜提供资料、安排访谈与现场工作"])
    return out


def schema(form: Form, tier: str = "standard", product: object | None = None, project: Project | None = None,
           members: Sequence[tuple[str, str]] = ()) -> dict:
    pre = prefill_values(product, project, members)
    return {"code": form.code, "title": form.title,
            "sections": [{"title": s.title, "fixed": s.fixed_for(tier), "drafted_by_agent": not s.fields and not s.fixed_for(tier),
                          "fields": [{"key": f.key, "label": f.label, "type": f.type, "required": f.required,
                                      "hint": f.hint, "max": MAX_FIELD[f.type], "columns": list(f.columns), "example": f.example,
                                      "default": pre.get(f.prefill) or f.default}
                                     for f in s.fields]}
                         for s in form.sections]}


def _clean(form: Form, values: dict) -> dict[str, str]:
    if not isinstance(values, dict):
        raise FormError(422, "values 应为 {字段: 内容}")
    fields = {f.key: f for f in form.fields()}
    unknown = sorted(set(values) - set(fields))
    if unknown:
        raise FormError(422, f"表单没有这些字段：{'、'.join(unknown)}")
    out: dict[str, str] = {}
    missing = []
    for key, f in fields.items():
        v = values.get(key, "")
        if not isinstance(v, str):
            raise FormError(422, f"“{f.label}”应为文本")
        v = v.strip()
        if len(v) > MAX_FIELD[f.type]:
            raise FormError(422, f"“{f.label}”不超过 {MAX_FIELD[f.type]} 字")
        if f.type == "list":
            items = [x.strip().lstrip("-*•").strip() for x in v.splitlines()]
            items = [x for x in items if x]
            if len(items) > MAX_LIST_ITEMS:
                raise FormError(422, f"“{f.label}”不超过 {MAX_LIST_ITEMS} 项")
            v = "\n".join(f"- {x}" for x in items)
        elif f.type == "table":
            v = _table(f, v)
        elif f.type == "text":
            v = " ".join(v.split())
        if f.required and not v:
            missing.append(f.label)
        out[key] = v
    if missing:
        raise FormError(422, f"请填写：{'、'.join(missing)}")
    return out


def _row(cells: Sequence[str]) -> str:
    return "| " + " | ".join(c.replace("|", "\\|") for c in cells) + " |"


def _table(f: Field, v: str) -> str:
    """每行一条、格间全角竖线；多出的格并入最后一格，不足补空；半角竖线转义，免得破坏表格。"""
    n, rows = len(f.columns), []
    for line in v.splitlines():
        cells = [c.strip() for c in line.split("｜")]
        if not any(cells):
            continue
        rows.append(cells[:n - 1] + ["｜".join(cells[n - 1:])] if len(cells) > n else cells + [""] * (n - len(cells)))
    if len(rows) > MAX_LIST_ITEMS:
        raise FormError(422, f"“{f.label}”不超过 {MAX_LIST_ITEMS} 行")
    if not rows:
        return ""
    return "\n".join([_row(f.columns), "| " + " | ".join("---" for _ in f.columns) + " |", *map(_row, rows)])


def _render_section(s: Section, vals: dict[str, str], tier: str) -> str:
    paras = re.split(r"\n\s*\n", s.template.strip())
    keep = []
    for para in paras:                                 # 这一段引用的字段都没填：整段去掉
        refs = _REF.findall(para)
        keep.append(not refs or any(vals.get(k) for k in refs))
    for i in range(len(paras) - 2, -1, -1):            # 不引用字段的小标题段跟着下一段去留
        if not _REF.findall(paras[i]) and not keep[i + 1]:
            keep[i] = False
    parts = [_REF.sub(lambda m: vals.get(m.group(1), ""), p).strip() for p, k in zip(paras, keep) if k]
    if s.fixed_for(tier):
        parts.append(s.fixed_for(tier))
    return "\n\n".join(p for p in parts if p)


def render(form: Form, values: dict, tier: str = "standard") -> list[tuple[str, str]]:
    """返回 [(章节标题, 正文)]；只出大纲不写正文的章节正文为空串。"""
    vals = _clean(form, values)
    return [(s.title, _render_section(s, vals, tier) if s.fields or s.fixed_for(tier) else "") for s in form.sections]


def submit(db: Session, user: User, project: Project, d: Deliverable, form: Form, values: dict) -> Deliverable:
    chapters = render(form, values, project.tier)
    action = "deliverable.form"
    dsvc._check_writable(db, user, project, d, action)
    written = [s.seq for s in d.sections if s.current_version]
    if written:
        raise FormError(409, f"{d.code} 第 {'、'.join(map(str, written))} 章已有正文，表单只用于首次成稿，请在编辑器里修改")
    text = "\n".join(t + "\n" + b for t, b in chapters)
    dsvc._guard(db, user, project, d, action, text)          # 整份先查，任何一章不过都不写
    dsvc._placeholders(db, user, project, d, action, text)
    d = dsvc.set_outline(db, user, project, d, [t for t, _ in chapters])
    for seq, (_, body) in enumerate(chapters, 1):
        if body:
            dsvc.write_section(db, user, project, d, seq, body, note=NOTE)
    db.refresh(d)
    return d
