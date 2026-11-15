"""控制映射矩阵（二期 B3，接口约定第六节）：映射矩阵条目的正文解析和 kb.mapping 查询。

映射矩阵是普通知识条目（L4、类型 MAP），走正常的投料、审核、发布；正文里用 Markdown 表格写映射，
前两列的表头是源标准和目标标准，第三列“关系”，第四列“说明”（可省略）：

    | ISO/IEC 27001:2022 | GB/T 22239-2019 | 关系 | 说明 |
    |---|---|---|---|
    | A.5.1 | 8.1.6.1、8.1.6.2 | 部分覆盖 | 等保要求制定总体方针和管理制度…… |

- 一个单元格可写多个条款号（用“、”或逗号分隔），展开为多行；一份正文可以有多张表（不同标准对）。
- 形如 IPv4 的四级条款号（等保 8.1.6.1）要写成“第 8.1.6.1 条”，否则机密级检测按 IP 地址拦截（guard 的条款号
  豁免规则，测试 #38）；入库时去掉“第”“条”，只存 8.1.6.1。
- 关系：等同 / 部分覆盖 / 相关（也接受 equivalent / partial / related）。
- 建版本时解析（app/kb/items.py），解析不了的拒绝建版本；解析结果存 kb_mappings，随版本只追加。
- 只写条款号、关系和简短说明，不写标准原文（二期计划第六节：国家标准只收条款号、标题、解读和映射）。

查询只返回已发布、未下线的现行版本，受限条目按调用人可见范围过滤（review.can_view）；标准号按规整后的
前缀匹配（“ISO 27001” 匹配 “ISO/IEC 27001:2022”，“GB/T 22239” 匹配 “GB/T 22239-2019”），
源和目标可以反过来查（返回 reversed=true，源、目标列已对调）。审计只记标准号和条数，不记说明。
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core import audit
from ..models import KbItem, KbMapping, KbVersion, User
from ..services import guard
from . import meta

RELATIONS = {"等同": "equivalent", "部分覆盖": "partial", "相关": "related",
             "equivalent": "equivalent", "partial": "partial", "related": "related"}
RELATION_NAMES = {"equivalent": "等同", "partial": "部分覆盖", "related": "相关"}
STD_LEN, REF_LEN, NOTE_LEN, MAX_ROWS = 100, 40, 500, 3000
DEFAULT_LIMIT, MAX_LIMIT = 20, 50
_SEP_RE = re.compile(r"^\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?$")
_REF_SPLIT = re.compile(r"[、,，;；]")


class MappingError(ValueError):
    def __init__(self, status: int, detail):
        super().__init__(detail if isinstance(detail, str) else "映射查询被拒绝")
        self.status = status
        self.detail = detail


@dataclass
class Row:
    source_std: str
    source_ref: str
    target_std: str
    target_ref: str
    relation: str
    note: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def _cells(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


_CLAUSE_WRAP = re.compile(r"^第\s*(.+?)\s*[条款]$")
_CONTROL_RE = re.compile(r"^(?:[A-Za-z]{1,3}\.?)?\d+(?:\.\d+)*[a-z]?$")


def _ref(r: str) -> str:
    r = r.strip()
    m = _CLAUSE_WRAP.match(r)
    return m.group(1) if m else r


def _refs(cell: str) -> list[str]:
    return [_ref(r) for r in _REF_SPLIT.split(cell) if _ref(r)]


def parse_rows(body: str) -> list[Row]:
    """从映射矩阵正文解析映射行；格式不对抛 meta.MetaError（带行号），建版本时据此拒绝。"""
    lines = body.splitlines()
    rows: list[Row] = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not (line.startswith("|") and i + 1 < len(lines) and _SEP_RE.match(lines[i + 1].strip())):
            i += 1
            continue
        head = _cells(line)
        if len(head) < 3 or head[2] != "关系":
            i += 2              # 不是映射表（比如说明性的表格），跳过
            while i < len(lines) and lines[i].strip().startswith("|"):
                i += 1
            continue
        src_std, dst_std = head[0], head[1]
        if not src_std or not dst_std or len(src_std) > STD_LEN or len(dst_std) > STD_LEN:
            raise meta.MetaError(f"第 {i + 1} 行：映射表前两列表头要写源标准和目标标准（各至多 {STD_LEN} 字）")
        i += 2
        while i < len(lines) and lines[i].strip().startswith("|"):
            cells = _cells(lines[i])
            n = i + 1
            i += 1
            if len(cells) < 3:
                raise meta.MetaError(f"第 {n} 行：映射行至少要有源条款、目标条款和关系三列")
            relation = RELATIONS.get(cells[2].strip().lower() if cells[2].isascii() else cells[2].strip())
            if relation is None:
                raise meta.MetaError(f"第 {n} 行：关系只能是 等同 / 部分覆盖 / 相关")
            note = cells[3] if len(cells) > 3 else ""
            if len(note) > NOTE_LEN:
                raise meta.MetaError(f"第 {n} 行：说明至多 {NOTE_LEN} 字")
            sources, targets = _refs(cells[0]), _refs(cells[1])
            if not sources or not targets:
                raise meta.MetaError(f"第 {n} 行：源条款和目标条款都不能为空")
            if any(len(r) > REF_LEN for r in sources + targets):
                raise meta.MetaError(f"第 {n} 行：条款号至多 {REF_LEN} 字")
            rows += [Row(src_std, s, dst_std, t, relation, note) for s in sources for t in targets]
    if not rows:
        raise meta.MetaError("映射矩阵正文里没有映射表：表头为“源标准 | 目标标准 | 关系 | 说明”")
    if len(rows) > MAX_ROWS:
        raise meta.MetaError(f"一个映射矩阵至多 {MAX_ROWS} 行，请按标准对拆成多个条目")
    return rows


_STD_TRANS = str.maketrans({"／": "/", "：": ":", "－": "-", "—": "-", "–": "-"})


def std_key(s: str) -> str:
    """标准号规整：去空白、统一全角符号、大写、去掉 “/IEC”。"""
    k = re.sub(r"\s+", "", s.translate(_STD_TRANS)).upper()
    return k.replace("/IEC", "")


def std_matches(stored: str, asked: str) -> bool:
    """asked 是 stored 的规整前缀，且断在编号之后（“GB/T22239” 不匹配 “GB/T222390”）。"""
    a, s = std_key(asked), std_key(stored)
    if not a or not s.startswith(a):
        return False
    return len(s) == len(a) or not (s[len(a)].isdigit() and a[-1].isdigit())


def _ref_key(s: str) -> str:
    return re.sub(r"\s+", "", s).upper()


def ref_matches(stored: str, control: str) -> bool:
    """条款号精确匹配；也匹配其下级条款（“8.1.6” 匹配 “8.1.6.1”）。"""
    s, c = _ref_key(stored), _ref_key(control)
    return s == c or s.startswith(c + ".")


def _published_rows(db: Session):
    return db.execute(
        select(KbMapping, KbVersion, KbItem)
        .join(KbVersion, KbVersion.id == KbMapping.version_id)
        .join(KbItem, KbItem.current_version_id == KbVersion.id)
        .where(KbVersion.status == "published", KbItem.retired.is_(False), KbItem.doc_type == "MAP")
        .order_by(KbItem.doc_id, KbMapping.seq)).all()


def lookup(db: Session, user: User, source: str, *, target: str = "", control: str = "", query: str = "",
           limit: int = DEFAULT_LIMIT) -> dict:
    """查控制映射：source 必填，target / control / query 可选（query 在说明和条款号里按词匹配，词都要出现）。"""
    from . import review

    source, target, control, query = source.strip(), target.strip(), control.strip(), query.strip()
    if not source:
        raise MappingError(422, "source（源标准）必填")
    if len(source) > STD_LEN or len(target) > STD_LEN or len(control) > REF_LEN or len(query) > 200:
        raise MappingError(422, "参数过长：标准号至多 100 字，条款号至多 40 字，检索词至多 200 字")
    control = _ref(control)
    if control and not _CONTROL_RE.match(control):
        raise MappingError(422, "control 只写条款号，如 A.5.15、8.1.4.2")
    # 条款号按格式校验（四级条款号形如 IP，不送机密级检测）；标准号和检索词过机密级检测
    checked = guard.check(" ".join(x for x in (source, target, query) if x))
    if checked.blocked:
        audit.record(db, actor=user.username, action="kb.mapping", result="blocked",
                     categories=sorted({f.kind for f in checked.findings}))
        raise MappingError(422, {"blocked": True, "summary": checked.summary() + "，没有查询"})
    limit = max(1, min(limit, MAX_LIMIT))
    terms = [t.lower() for t in query.split()]
    seen: dict[int, bool] = {}
    items: list[dict] = []
    for m, v, item in _published_rows(db):
        if item.id not in seen:
            seen[item.id] = review.can_view(db, user, item, v)
        if not seen[item.id]:
            continue
        if std_matches(m.source_std, source) and (not target or std_matches(m.target_std, target)):
            reversed_ = False
            s_std, s_ref, t_std, t_ref = m.source_std, m.source_ref, m.target_std, m.target_ref
        elif std_matches(m.target_std, source) and (not target or std_matches(m.source_std, target)):
            reversed_ = True
            s_std, s_ref, t_std, t_ref = m.target_std, m.target_ref, m.source_std, m.source_ref
        else:
            continue
        if control and not ref_matches(s_ref, control):
            continue
        if terms:
            hay = f"{m.note} {s_ref} {t_ref}".lower()
            if not all(t in hay for t in terms):
                continue
        items.append({"source_std": s_std, "source_ref": s_ref, "target_std": t_std, "target_ref": t_ref,
                      "relation": m.relation, "relation_name": RELATION_NAMES[m.relation], "note": m.note,
                      "doc_id": item.doc_id, "version": v.version, "reversed": reversed_})
    total = len(items)
    audit.record(db, actor=user.username, action="kb.mapping", result="allowed", source=source, target=target,
                 control=control, query_length=len(query), total=total)
    out: dict = {"items": items[:limit], "total": total}
    if not total:
        out["note"] = "映射库中没有找到相关映射：请按公共规范第 3 节标注【依据待核实】，不得凭记忆补写映射关系或条款号"
    return out
