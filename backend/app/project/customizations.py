"""项目定制项（四期 A30，《平台规划刷新设计》第五节、第十节第 3 条、第十一节第 6 条）。

三个入口汇成一张表 customizations（迁移 0059）：
- D1 客户要求栏（启动包表单字段 client_requirements，每行一条）：属“客户书面要求”层；顾问填表提交即视为确认。
- 顾问对话（POST /api/projects/{code}/customizations，source=chat）：平台按行和分号拆成一条条，每条发一张“定制项确认”卡。
- 编辑器（source=editor）：只对某一章的一次性要求，提交即生效，该章下一次写入新版本后记为已用（used）。

规则：
1. 正文先过机密级检测，命中 422 不保存；类别由平台判：增删章节为结构类，用语 / 称呼 / 口径为口径类，其余为形式类。
2. 不可接受（unacceptable，写平台理由、不发卡）：资质结论用语（quality.qualification_matches）、越过红线的要求
   （写入口令密钥、完整拓扑与 IP 规划、漏洞利用细节、业务数据或个人信息样本、真实客户名称）、去掉来源标注、
   删除产品必备章节（项目配置快照里 kind=required_sections 的门禁）。
3. 生效：形式类、口径类顾问确认即生效；结构类顾问确认后发“定制项审批”卡，只有本项目项目经理能处理，确认后才生效。
4. 优先级：红线与资质 ＞ 客户书面要求 ＞ 项目定制项 ＞ 行业 ＞ 公司做法与黄金样例 ＞ 产品默认大纲。
   同一交付件同一章节标题的结构类冲突（一条要加、一条要删），高层的赢，同层以后生效的为准，输的标 superseded。
   形式类、口径类按优先级排序交给 Agent（prompt_block），冲突时以靠前者为准。
5. 质量门禁逐条核对（review_issues）：结构类“增加”的章节在不在、“删除”的是否已删，只看必备内容在不在、不看页数；
   形式类、口径类由评审 Agent 逐条核对（checklist_block）。
6. 上游变更：定制项生效或生效后撤回的时间记 changed_at；该时间晚于某章当前版本写入时间的，该章“需更新”（needs_update）；
   晚于最近一次完成的质量评审的，签署门禁要求重新评审（gate_reason）。
7. 审计只记编号、入口、类别、状态和长度，不记正文。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core import audit
from ..core.db import utcnow
from ..core.permissions import PROJECT_MANAGER, membership
from ..models import Confirmation, Customization, Deliverable, Project, User
from ..products import config
from ..services import guard
from . import configs, deliverables

SOURCES = {"d1": "D1 客户要求栏", "chat": "顾问对话", "editor": "编辑器"}
LAYERS = ("红线与资质", "客户书面要求", "项目定制项", "行业", "公司做法与黄金样例", "产品默认大纲")
LAYER_OF = {"d1": 1, "chat": 2, "editor": 2}
KIND_NAMES = {"form": "形式类", "wording": "口径类", "structure": "结构类"}
STATUS_NAMES = {"proposed": "待顾问确认", "pending_pm": "待项目经理确认", "active": "已生效", "used": "已用（一次性）",
                "unacceptable": "不可接受", "rejected": "未采纳", "withdrawn": "已撤回"}
EFFECTIVE = ("active",)
CARD_KIND = "定制项确认"         # 不在 confirmations.KINDS 里：Agent 不能发，只由平台发
PM_KIND = "定制项审批"           # 结构类：只有本项目项目经理能处理
CARD_AGENT = "项目定制项"
ADOPT = "A"
GATE_DIMENSION = "定制项逐条核对"
MAX_ITEMS = 20
MAX_TEXT = 500

_DCODE = re.compile(r"(?<![A-Za-z0-9])D([1-9])(?![0-9])")
_ADD = r"增加|新增|加上|加一章|补一章|增设|单列"
_REMOVE = r"删除|删去|删掉|去掉|取消|不要|不写"
_STRUCT = re.compile(rf"({_ADD}|{_REMOVE})\s*(?:一章|一节|章节)?\s*[“「《\"']([^”」》\"'\n]{{1,60}})[”」》\"']\s*(?:一章|章节|这一章|一节|章)?"
                     rf"|({_ADD}|{_REMOVE})\s*([^，。；,;\s“「《]{{2,40}}?)\s*(?:一章|章节|这一章|一节)")
_WORDING = re.compile(r"口径|措辞|用语|表述|称呼|叫法|说法|术语|统称|统一写|统一称|不用“|不要用|改称")
_REDLINE = re.compile(r"(?:写入|写明|写上|附上|附|列出|列明|放入|包含|提供|贴出|展示)[^。；;\n]{0,12}"
                      r"(?:口令|密码|密钥|私钥|完整(?:的)?(?:网络)?拓扑|IP\s*(?:地址)?规划|IP\s*地址清单|漏洞利用|利用细节|"
                      r"业务数据样本|个人信息样本|身份证号|客户真名|真实(?:客户|机构)?名称)")
_NO_SOURCES = re.compile(rf"(?:{_REMOVE}|不标|不加)[^。；;\n]{{0,6}}(?:来源标注|来源|出处|依据标注|引用标注)")
_SPLIT = re.compile(r"[\n；;]+")
_BULLET = re.compile(r"^\s*(?:[-*•·]|\d+[.．、)）]|[（(]\d+[)）])\s*")


class CustomizationError(ValueError):
    def __init__(self, status: int, detail):
        super().__init__(detail if isinstance(detail, str) else "被拒绝")
        self.status, self.detail = status, detail


@dataclass(frozen=True)
class Parsed:
    text: str
    kind: str
    action: str = ""
    section_title: str = ""
    deliverable: str = ""


def split_items(text: str) -> list[str]:
    """按行和分号拆成一条条，去掉编号和项目符号。"""
    out = []
    for part in _SPLIT.split(text or ""):
        part = _BULLET.sub("", part).strip(" \t，,。")
        if part:
            out.append(part)
    return out


def classify(text: str, codes: set[str]) -> Parsed:
    """平台判类别：增删章节为结构类（取出动作和章节标题），用语口径为口径类，其余为形式类；文中第一个 Dn 为适用交付件。"""
    m = _DCODE.search(text)
    dcode = f"D{m.group(1)}" if m and f"D{m.group(1)}" in codes else ""
    s = _STRUCT.search(text)
    if s:
        verb, title = (s.group(1), s.group(2)) if s.group(1) else (s.group(3), s.group(4))
        action = "remove" if re.fullmatch(_REMOVE, verb) else "add"
        title = title.strip()
        if title and not _NO_SOURCES.search(text):
            return Parsed(text, "structure", action, title[:128], dcode)
    return Parsed(text, "wording" if _WORDING.search(text) else "form", deliverable=dcode)


def required_sections(db: Session, project: Project) -> dict[str, list[str]]:
    """产品必备章节清单：项目配置快照里 kind=required_sections 的门禁（第十一节第 6 条复用 A6 门禁配置）。"""
    try:
        gates = configs.snapshot(db, project)["gates"]
    except config.CatalogError:
        return {}
    out: dict[str, list[str]] = {}
    for g in gates:
        if g.get("kind") == "required_sections":
            out.setdefault(g.get("deliverable") or "", []).extend(g.get("sections", []))
    return out


def unacceptable_reason(db: Session, project: Project, p: Parsed) -> str:
    from .quality import qualification_matches            # quality 引用本模块，延迟导入免循环

    if qualification_matches(p.text):
        return "越过资质边界：平台和交付件不出具或暗示测评、密评、认证结论，只能写差距分析、符合性初判和整改建议"
    if _REDLINE.search(p.text):
        return "越过数据保护红线：口令密钥、完整拓扑与 IP 规划、漏洞利用细节、业务数据和个人信息样本、客户真名不进交付件"
    if _NO_SOURCES.search(p.text):
        return "削掉质量底线：来源标注是质量门禁必查项，不能去掉"
    if p.kind == "structure" and p.action == "remove":
        req = required_sections(db, project)
        targets = [p.deliverable] if p.deliverable else list(req)
        for code in targets:
            for need in [*req.get(code, []), *req.get("", [])]:
                if need in p.section_title or p.section_title in need:
                    return f"碰到产品必备章节：{code or '各交付件'}《{need}》是必备章节，不能删除"
    return ""


def _guard(db: Session, user: User, project: Project, texts: list[str]) -> None:
    result = guard.check("\n".join(texts))
    if result.blocked:
        audit.record(db, actor=user.username, action="customization.create", result="blocked",
                     project_code=project.code, reason="guard", categories=sorted({f.kind for f in result.findings}))
        raise CustomizationError(422, {"blocked": True, "summary": f"{result.summary()}，没有保存",
                                       "findings": [{"kind": f.kind, "label": f.label, "excerpt": f.excerpt}
                                                    for f in result.findings[:10]]})


def _codes(project: Project) -> set[str]:
    return {d.code for d in project.deliverables}


def _card(db: Session, user: User, project: Project, item: Customization, kind: str) -> Confirmation:
    name = KIND_NAMES[item.kind]
    if kind == PM_KIND:
        impact = "结构类定制项（增删章节）须项目经理确认后才生效；选 B 不生效"
        options = [{"key": "A", "text": "确认，按此增删章节"}, {"key": "B", "text": "不同意，不生效"}]
    else:
        impact = ("结构类：确认后交项目经理确认才生效" if item.kind == "structure"
                  else f"{name}：确认即生效，之后起草、改写和质量评审都按这条执行")
        options = [{"key": "A", "text": "采纳为项目定制项"}, {"key": "B", "text": "不采纳"}]
    title = f"{kind} · {item.deliverable_code or '全部交付件'} · {name}：{item.text}"[:256]
    c = Confirmation(project_id=project.id, kind=kind, title=title, agent=CARD_AGENT, agent_code="", options=options,
                     recommendation=ADOPT, reason=f"定制项 C-{item.id}（{SOURCES[item.source]}）：{item.text}",
                     impact=impact, deliverable_code=item.deliverable_code, section_seq=item.section_seq,
                     created_by=user.username)
    db.add(c)
    db.flush()
    item.card_id = c.id
    audit.record(db, actor=user.username, action="confirmation.create", result="allowed", project_code=project.code,
                 confirmation_id=c.id, kind=kind, customization=item.id)
    return c


def _activate(item: Customization, username: str) -> None:
    item.status, item.decided_by, item.changed_at = "active", username, utcnow()


def _record(db: Session, user: User, project: Project, action: str, item: Customization) -> None:
    audit.record(db, actor=user.username, action=action, result="allowed", project_code=project.code,
                 customization=item.id, source=item.source, kind=item.kind, status=item.status,
                 deliverable=item.deliverable_code or None, length=len(item.text))


def _new(db: Session, user: User, project: Project, p: Parsed, source: str, seq: int | None = None) -> Customization:
    item = Customization(project_id=project.id, source=source, kind=p.kind, action=p.action,
                         section_title=p.section_title, deliverable_code=p.deliverable, section_seq=seq,
                         one_time=source == "editor", text=p.text, status="proposed", created_by=user.username)
    reason = unacceptable_reason(db, project, p)
    if reason:
        item.status, item.reason = "unacceptable", reason
    db.add(item)
    db.flush()
    return item


def _validate(texts: list[str]) -> None:
    if not texts or len(texts) > MAX_ITEMS:
        raise CustomizationError(422, f"定制项每次 1～{MAX_ITEMS} 条")
    if any(len(t) > MAX_TEXT for t in texts):
        raise CustomizationError(422, f"每条定制项不超过 {MAX_TEXT} 字")


def from_chat(db: Session, user: User, project: Project, text: str) -> list[Customization]:
    """对话入口：拆成一条条，每条可接受的发一张“定制项确认”卡；不可接受的只记理由、不发卡。"""
    texts = split_items(text)
    _validate(texts)
    _guard(db, user, project, texts)
    codes, out = _codes(project), []
    for t in texts:
        item = _new(db, user, project, classify(t, codes), "chat")
        if item.status == "proposed":
            _card(db, user, project, item, CARD_KIND)
        _record(db, user, project, "customization.create", item)
        out.append(item)
    db.commit()
    return out


def from_editor(db: Session, user: User, project: Project, d: Deliverable, seq: int, text: str) -> Customization:
    """编辑器入口：只对某一章的一次性要求，提交即生效（顾问本人提的，不再发卡）；结构类请在对话里提。"""
    text = (text or "").strip()
    _validate([text] if text else [])
    if deliverables.get_section(d, seq) is None:
        raise CustomizationError(422, f"{d.code} 没有第 {seq} 章")
    _guard(db, user, project, [text])
    p = classify(text, _codes(project))
    if p.kind == "structure":
        raise CustomizationError(422, "增删章节是结构类定制项，须项目经理确认，请在顾问对话里提")
    item = _new(db, user, project, Parsed(text, p.kind, deliverable=d.code), "editor", seq)
    if item.status == "proposed":
        _activate(item, user.username)
    _record(db, user, project, "customization.create", item)
    db.commit()
    return item


def from_d1(db: Session, user: User, project: Project, raw: str) -> list[Customization]:
    """D1 客户要求栏（客户书面要求层）：顾问填表提交即确认，形式类、口径类直接生效，结构类发项目经理审批卡。
    由 forms.submit 在写完 D1 后调用；整份表单已过机密级检测。"""
    texts = split_items(raw)
    if not texts:
        return []
    codes, out = _codes(project), []
    for t in texts:
        item = _new(db, user, project, classify(t, codes), "d1")
        if item.status == "proposed":
            if item.kind == "structure":
                item.status = "pending_pm"
                _card(db, user, project, item, PM_KIND)
            else:
                _activate(item, user.username)
        _record(db, user, project, "customization.create", item)
        out.append(item)
    db.commit()
    return out


def check_can_resolve(db: Session, user: User, c: Confirmation) -> None:
    """“定制项审批”卡只有本项目项目经理能处理。"""
    if c.kind == PM_KIND:
        m = membership(db, c.project_id, user.id)
        if m is None or m.project_role != PROJECT_MANAGER:
            raise CustomizationError(403, "结构类定制项须本项目项目经理确认")


def on_card_resolved(db: Session, user: User, c: Confirmation) -> Customization | None:
    """定制项确认卡、审批卡处理后改定制项状态（resolve 接口在卡处理后调用）。"""
    if c.kind not in (CARD_KIND, PM_KIND):
        return None
    item = db.scalar(select(Customization).where(Customization.card_id == c.id))
    if item is None or item.status not in ("proposed", "pending_pm"):
        return None
    adopt = c.choice == ADOPT or (not c.choice and bool(c.resolution))
    project = c.project
    if not adopt:
        item.status, item.decided_by = "rejected", user.username
    elif c.kind == CARD_KIND and item.kind == "structure":
        item.status, item.decided_by = "pending_pm", user.username
        _card(db, user, project, item, PM_KIND)
    else:
        _activate(item, user.username)
    _record(db, user, project, "customization.decide", item)
    db.commit()
    return item


def withdraw(db: Session, user: User, project: Project, item: Customization) -> Customization:
    if item.status not in ("proposed", "pending_pm", "active"):
        raise CustomizationError(409, f"这条定制项{STATUS_NAMES[item.status]}，不能撤回")
    if item.status == "active":
        item.changed_at = utcnow()                  # 生效后撤回同样是上游变更
    if item.card_id:
        card = db.get(Confirmation, item.card_id)
        if card is not None and card.status == "open":
            card.status, card.resolution, card.resolved_by, card.resolved_at = \
                "resolved", "定制项已撤回", user.username, utcnow()
    item.status, item.decided_by = "withdrawn", user.username
    _record(db, user, project, "customization.withdraw", item)
    db.commit()
    return item


def get(db: Session, project: Project, cid: int) -> Customization | None:
    item = db.get(Customization, cid)
    return item if item is not None and item.project_id == project.id else None


def list_items(db: Session, project: Project) -> list[Customization]:
    return list(db.scalars(select(Customization).where(Customization.project_id == project.id)
                           .order_by(Customization.id)))


# ---- 优先级裁决 ----

def _applies(item: Customization, dcode: str | None, seq: int | None) -> bool:
    if dcode is None:
        return not item.deliverable_code
    if item.deliverable_code and item.deliverable_code != dcode:
        return False
    return item.section_seq is None or seq is None or item.section_seq == seq


def resolve_priority(items: list[Customization]) -> dict[int, int]:
    """结构类冲突裁决：同一交付件、同一章节标题上，高层（客户书面要求）赢，同层以后生效的为准。返回 {输的: 赢的}。"""
    groups: dict[tuple[str, str], list[Customization]] = {}
    for i in items:
        if i.status in EFFECTIVE and i.kind == "structure":
            groups.setdefault((i.deliverable_code, i.section_title), []).append(i)
    lost: dict[int, int] = {}
    for g in groups.values():
        if len({i.action for i in g}) < 2:
            continue
        win = min(g, key=lambda i: (LAYER_OF[i.source], -(i.changed_at.timestamp() if i.changed_at else 0), -i.id))
        lost.update({i.id: win.id for i in g if i.action != win.action})
    return lost


def effective(db: Session, project: Project, dcode: str | None, seq: int | None = None) -> list[Customization]:
    """对本交付件（及章）生效、未被覆盖的定制项，按优先级（层，再按生效先后）排序；dcode=None 只取项目级的。"""
    items = list_items(db, project)
    lost = resolve_priority(items)
    eff = [i for i in items if i.status in EFFECTIVE and i.id not in lost and _applies(i, dcode, seq)]
    return sorted(eff, key=lambda i: (LAYER_OF[i.source], i.changed_at or i.created_at, i.id))


def _line(i: Customization) -> str:
    where = (i.deliverable_code or "全部交付件") + (f" 第 {i.section_seq} 章（一次性）" if i.section_seq else "")
    if i.kind == "structure":
        what = f"{'增加' if i.action == 'add' else '删除'}章节《{i.section_title}》（{i.text}）"
    else:
        what = i.text
    return f"- C-{i.id} [{LAYERS[LAYER_OF[i.source]]} · {KIND_NAMES[i.kind]} · {where}] {what}"


def prompt_block(db: Session, project: Project, dcode: str | None, seq: int | None = None) -> str:
    """交给起草、改写 Agent 的定制项（必须遵循）；没有生效的定制项返回空串。"""
    eff = effective(db, project, dcode, seq)
    if not eff:
        return ""
    order = " ＞ ".join(LAYERS)
    return ("# 本项目定制项（必须遵循）\n"
            f"优先级：{order}。下列各条已按优先级排好，相互冲突时以靠前者为准；任何定制项都不得突破红线与资质边界。\n"
            + "\n".join(_line(i) for i in eff))


def checklist_block(db: Session, project: Project, dcode: str) -> str:
    """交给质量评审 Agent 逐条核对的形式类、口径类定制项（结构类由平台规则核对）。"""
    eff = [i for i in effective(db, project, dcode) if i.kind != "structure" and not i.one_time]
    if not eff:
        return ""
    return ("\n\n# 定制项逐条核对\n逐条核对下列定制项是否落实，没落实的列为“重要”问题，位置写定制项编号；只核对内容在不在，不看页数。\n"
            + "\n".join(_line(i) for i in eff))


# ---- 门禁逐条核对、上游变更 ----

def needs_update(db: Session, project: Project, d: Deliverable) -> list[int]:
    """定制项生效（或生效后撤回）晚于该章当前版本写入时间的章：按上游变更处理，标“需更新”。"""
    items = [i for i in list_items(db, project) if i.changed_at is not None and i.status in ("active", "withdrawn", "used")]
    out = []
    for s in d.sections:
        v = deliverables.get_version(s)
        if v is None:
            continue
        written = v.created_at
        if any(_applies(i, d.code, s.seq) and _after(i.changed_at, written) for i in items):
            out.append(s.seq)
    return out


def _after(a, b) -> bool:
    if a.tzinfo is None or b.tzinfo is None:          # SQLite 读回不带时区
        a, b = a.replace(tzinfo=None), b.replace(tzinfo=None)
    return a > b


def review_issues(db: Session, project: Project, d: Deliverable, versions: dict) -> list[dict]:
    """质量门禁“定制项逐条核对”的平台规则：结构类增加的章节在不在（阻断）、删除的是否已删（重要）、需更新的章（重要）。"""
    from .quality import _issue                       # quality 引用本模块，延迟导入免循环

    titles = [s.title for s in d.sections]
    issues = []
    for i in effective(db, project, d.code):
        if i.kind != "structure":
            continue
        present = any(i.section_title in t or t in i.section_title for t in titles)
        if i.action == "add" and not present:
            issues.append(_issue("阻断", "大纲", GATE_DIMENSION, f"定制项 C-{i.id}：缺章节《{i.section_title}》",
                                 "按定制项在大纲里加上这一章并写正文", "rule"))
        elif i.action == "remove" and present:
            issues.append(_issue("重要", "大纲", GATE_DIMENSION, f"定制项 C-{i.id}：章节《{i.section_title}》还在",
                                 "按定制项删去这一章", "rule"))
    for seq in needs_update(db, project, d):
        s = deliverables.get_section(d, seq)
        if s is not None and int(versions.get(str(seq), 0) or 0) == s.current_version:
            issues.append(_issue("重要", f"第 {seq} 章 {s.title}", GATE_DIMENSION,
                                 "定制项在本章写入后有变更（上游变更），本章需更新", "按当前生效的定制项改写本章", "rule"))
    return issues


def gate_reason(db: Session, project: Project, d: Deliverable, reviewed_at) -> str:
    """签署门禁：最近一次完成的评审之后有影响本交付件的定制项变更，须重新评审。"""
    if reviewed_at is None:
        return ""
    changed = [i for i in list_items(db, project)
               if i.changed_at is not None and _applies(i, d.code, None) and _after(i.changed_at, reviewed_at)]
    return f"质量评审之后有 {len(changed)} 条定制项变更（上游变更），请按定制项更新后重新评审" if changed else ""


def consume_one_time(db: Session, project: Project, d: Deliverable, seq: int) -> int:
    """编辑器入口的一次性定制项：该章写入新版本后记为已用（deliverables.write_section 调用，调用方提交）。"""
    rows = db.scalars(select(Customization).where(
        Customization.project_id == project.id, Customization.one_time.is_(True), Customization.status == "active",
        Customization.deliverable_code == d.code, Customization.section_seq == seq)).all()
    for i in rows:
        i.status = "used"
    return len(rows)


def out(i: Customization, lost: dict[int, int]) -> dict:
    return {"id": i.id, "code": f"C-{i.id}", "source": i.source, "source_name": SOURCES[i.source],
            "layer": LAYERS[LAYER_OF[i.source]], "kind": i.kind, "kind_name": KIND_NAMES[i.kind], "action": i.action,
            "section_title": i.section_title, "deliverable": i.deliverable_code, "section": i.section_seq,
            "one_time": i.one_time, "text": i.text, "status": i.status, "status_name": STATUS_NAMES[i.status],
            "reason": i.reason, "card_id": i.card_id, "superseded": i.id in lost, "overridden_by": lost.get(i.id),
            "created_by": i.created_by, "created_at": i.created_at, "decided_by": i.decided_by}


def delete_for_project(db: Session, project_id: int) -> int:
    from sqlalchemy import delete
    return db.execute(delete(Customization).where(Customization.project_id == project_id)).rowcount or 0
