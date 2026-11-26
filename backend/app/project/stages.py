"""四期 A27：阶段自动推进、阶段包与汇报 PPT、本周简报（docs/驾驶舱改版方案.md 第七、八节）。

阶段自动推进（程序规则，例外才找人）：
- 阶段即 schemas.PHASES（启动、调研、分析、设计、汇报、验收）；交付件属于哪一段取项目配置快照里的 stage，
  “落地辅导”归“汇报”这一段（介于设计与验收之间）。
- 某段的退出条件：本项目这一段的交付件全部已签署（没有交付件的段视为满足）。从当前阶段起逐段检查，
  满足就进入下一段，最远到“验收”；验收之后的结项、保留期由 retention.py 管。只前进不后退。
- 已删除、已登记验收的项目不推进；开关同交付链自动推进（AUTO_FLOW，测试默认关）。
- worker 每小时随 autoflow 一起扫；每次推进写审计 project.phase_auto（actor=platform，记起止阶段和规则名）。

阶段包（启动、分析、验收三段，负责人 2026-11-25 决策 2）：一个 zip，内含
汇报 PPT 草稿（平台内置版式或 EXPORT_TEMPLATE_PPTX，复用 export_pptx 的排版）+ 该段各交付件的 Word（复用 export.py，
各自照常记 deliverable.export 审计）+ 清单.txt（哪些收进、哪些没收进及原因）。汇报 PPT 一律标草稿，人审后再用。
随时按需现生成，不落库；审计 project.stage_pack 只记阶段、件数和大小，不记正文。

本周简报：本周一 0 点（北京时间）至今，按审计汇总——自动完成 = actor 为 platform 的成功操作；
人工处理 = 人做的处理类操作（MANUAL_LABELS，签署、退回、提交、写入、确认卡等，不含查看与导出）；
待办 = 未处理确认卡数、待签署交付件、当前阶段未签交付件。只出计数、编号和配置里的交付件名称，不含客户原文。

启动计划回写（四期 A28③）：D1 启动包成稿后，从“工作分解与里程碑”表格（阶段｜时间｜产出）取当前阶段起的第一行
写回项目“下一个里程碑”，从“项目周期”取总周数（“共 N 周”或起止日期）；阶段自动推进时按新阶段重取。
来源就是 D1 正文，顾问在编辑器里改了表格，下次推进即生效；不加表。审计 project.plan_writeback 只记行数、周数和阶段。
启动会 PPT 不另做：D1 直接按 A9 导出 PPT（?format=pptx），每章一页起。

Ponytail：stages.py 停在“最后才写最小代码”——阶段规则仓库里没有；排版、导出、审计全部复用已有模块，未加依赖。
"""
from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core import audit
from ..core.db import utcnow
from ..models import AuditLog, Confirmation, Deliverable, Project, User
from ..products import config as product_config
from ..schemas import PHASES
from ..services import quality_incidents
from . import autoflow, configs
from . import deliverables as dsvc
from . import export as export_svc
from . import export_pptx
from .export import STATUS_NAMES, TZ, ExportError, _xml_safe

LAST = PHASES.index("验收")
STAGE_ALIAS = {"落地辅导": "汇报"}
REPORT_PHASES = ("启动", "分析", "验收")
TRIGGER = "stage_exit"
ZIP_MIME = "application/zip"

MANUAL_LABELS = {
    "deliverable.sign": "签署交付件", "deliverable.return": "退回交付件", "deliverable.submit": "提交签署",
    "deliverable.write": "修改章节", "deliverable.outline": "调整大纲", "deliverable.form": "填写表单",
    "confirmation.resolve": "处理确认卡", "quality.request": "发起质量评审", "quality.waive": "豁免门禁问题",
    "survey.confirm": "确认调研结果", "survey.pack": "生成调研包", "run.start": "发起交付链",
    "project.note": "录入现场记录", "project.document": "上传资料", "project.summary_card": "提交摘要卡",
    "project.acceptance": "登记验收",
}
AUTO_LABELS = {
    "run.start": "自动发起交付链", "quality.request": "自动质量评审", "deliverable.submit": "门禁通过自动提交",
    "project.phase_auto": "阶段自动推进",
}


class StageError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def phase_index(name: str) -> int | None:
    name = STAGE_ALIAS.get(name, name)
    return PHASES.index(name) if name in PHASES else None


def _by_phase(db: Session, p: Project) -> dict[int, list[Deliverable]]:
    """阶段序号 → 本项目这一段的交付件（按配置快照的 stage；快照有而项目没有的交付件不算）。"""
    try:
        stage_of = {d["code"]: d["stage"] for d in configs.snapshot(db, p)["deliverables"]}
    except product_config.CatalogError:
        stage_of = {}
    out: dict[int, list[Deliverable]] = {i: [] for i in range(len(PHASES))}
    for d in sorted(p.deliverables, key=lambda x: (len(x.code), x.code)):
        i = phase_index(stage_of.get(d.code, ""))
        if i is not None:
            out[i].append(d)
    return out


def _done(items: list[Deliverable]) -> bool:
    return all(d.status == "signed" for d in items)


def target(db: Session, p: Project, groups: dict[int, list[Deliverable]] | None = None) -> int:
    groups = groups if groups is not None else _by_phase(db, p)
    k = max(p.phase, 0)
    while k < LAST and _done(groups[k]):
        k += 1
    return max(k, p.phase)


def scan(db: Session, project: Project | None = None) -> list[str]:
    """推进满足退出条件的项目，返回推进了的项目代号（可重复执行）。"""
    if not autoflow.enabled():
        return []
    projects = [project] if project is not None else list(db.scalars(
        select(Project).where(Project.deleted_at.is_(None), Project.accepted_on.is_(None))))
    moved: list[str] = []
    for p in projects:
        if p.deleted_at is not None or p.accepted_on is not None or p.phase >= LAST:
            continue
        to = target(db, p)
        if to <= p.phase:
            continue
        before = p.phase
        p.phase = to
        db.commit()
        audit.record(db, actor="platform", action="project.phase_auto", result="allowed", project_code=p.code,
                     from_phase=PHASES[before], to_phase=PHASES[to], trigger=TRIGGER)
        plan_writeback(db, p, "platform")
        moved.append(p.code)
    return moved


# ---- 启动计划回写（A28③）----

MILESTONE_HEAD = ["阶段", "时间", "产出"]


def _cells(line: str) -> list[str]:
    return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", line.strip().strip("|"))]


def milestone_rows(body: str) -> list[tuple[str, str, str]]:
    """正文里“阶段 | 时间 | 产出”表格的数据行；没有这张表返回空。"""
    lines = [x for x in body.splitlines() if x.strip().startswith("|")]
    head = next((i for i, x in enumerate(lines) if _cells(x)[:3] == MILESTONE_HEAD), None)
    if head is None:
        return []
    rows = [(_cells(x) + ["", ""])[:3] for x in lines[head + 2:]]
    return [(a, b, c) for a, b, c in rows if a]


def pick_milestone(rows: list[tuple[str, str, str]], phase: int) -> str:
    """当前阶段起的第一行（落地辅导算汇报段）；都已过去返回空。"""
    for stage, when, out in rows:
        i = phase_index(stage)
        if i is not None and i >= phase:
            return f"{stage}（{when}）：{out}" if when else f"{stage}：{out}"
    return ""


def weeks_of(period: str) -> int | None:
    m = re.search(r"共\s*(\d+)\s*周", period)
    if m:
        n = int(m.group(1))
    else:
        d = re.findall(r"\d{4}-\d{2}-\d{2}", period)
        try:
            n = round((date.fromisoformat(d[1]) - date.fromisoformat(d[0])).days / 7) if len(d) >= 2 else 0
        except ValueError:
            n = 0
    return n if 1 <= n <= 104 else None


def plan_writeback(db: Session, p: Project, actor: str) -> bool:
    """按 D1 正文刷新项目“下一个里程碑”和总周数；有变化才写库并记审计。"""
    d = next((x for x in p.deliverables if x.code == "D1"), None)
    if d is None:
        return False
    bodies = [v.body for s in d.sections if (v := dsvc.get_version(s)) is not None]
    rows = next((r for b in bodies if (r := milestone_rows(b))), [])
    m = re.search(r"项目周期\*\*：(.+)", "\n".join(bodies))
    weeks = weeks_of(m.group(1)) if m else None
    nxt = pick_milestone(rows, p.phase)[:128] if rows else p.next_milestone
    total = weeks or p.total_weeks
    if nxt == p.next_milestone and total == p.total_weeks:
        return False
    p.next_milestone, p.total_weeks = nxt, total
    db.commit()
    audit.record(db, actor=actor, action="project.plan_writeback", result="allowed", project_code=p.code,
                 rows=len(rows), total_weeks=total, phase=PHASES[p.phase])
    return True


def overview(db: Session, p: Project) -> dict:
    groups = _by_phase(db, p)
    phases = [{"name": name, "report": name in REPORT_PHASES, "done": _done(groups[i]),
               "deliverables": [{"code": d.code, "title": d.title, "status": d.status} for d in groups[i]]}
              for i, name in enumerate(PHASES)]
    cur = p.phase if 0 <= p.phase < len(PHASES) else 0
    return {"current": PHASES[cur], "target": PHASES[target(db, p, groups)], "phases": phases}


# ---- 阶段包 ----

@dataclass
class Pack:
    data: bytes
    filename: str


def _report(user: User, p: Project, phase: str, items: list[Deliverable], now: datetime) -> bytes:
    prs, _ = export_pptx._load_template()
    values = {"{{项目代号}}": p.code, "{{服务}}": p.service, "{{行业}}": p.industry,
              "{{交付件编号}}": "", "{{交付件名称}}": f"{phase}阶段汇报", "{{修订}}": "",
              "{{导出日期}}": now.strftime("%Y-%m-%d"), "{{状态}}": "草稿"}
    export_pptx._replace_placeholders(prs, {k: _xml_safe(v) for k, v in values.items()})
    deck = export_pptx._Deck(prs)
    if not len(prs.slides):
        s = deck.slide(f"{p.code} {phase}阶段汇报")
        tf = deck._box(s).text_frame
        deck._runs(tf.paragraphs[0], f"{_xml_safe(p.service)} · {now:%Y-%m-%d}", 20)
    rows = [["编号", "名称", "状态", "修订"]] + [[d.code, _xml_safe(d.title), STATUS_NAMES.get(d.status, d.status),
                                              f"v{d.revision}"] for d in items]
    deck.table_slide(f"{phase}阶段交付件", rows if items else [["编号", "名称", "状态", "修订"], ["—", "本段没有交付件", "", ""]])
    for d in items:
        for sec in d.sections:
            v = dsvc.get_version(sec)
            deck.section(f"{d.code} {sec.seq}　{_xml_safe(sec.title)}", _xml_safe(v.body) if v else "（本章还没有正文）")
    deck.mark(True, f"导出：{user.username}　{now:%Y-%m-%d %H:%M}　{p.code} {phase}阶段汇报（草稿，需人审）")
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


def stage_pack(db: Session, user: User, p: Project, phase: str) -> Pack:
    if phase not in REPORT_PHASES:
        raise StageError(422, f"阶段包只出{'、'.join(REPORT_PHASES)}三段")
    items = _by_phase(db, p)[PHASES.index(phase)]
    now = utcnow().astimezone(TZ)
    try:
        report = _report(user, p, phase, items, now)
    except ExportError as e:
        raise StageError(e.status, e.detail) from e
    manifest = [f"{p.code} {phase}阶段包 {now:%Y-%m-%d %H:%M}", f"汇报 PPT：{p.code}-{phase}阶段汇报-草稿.pptx"]
    buf = io.BytesIO()
    words = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"{p.code}-{phase}阶段汇报-草稿.pptx", report)
        for d in items:
            if quality_incidents.export_frozen(db, p.code, d.code) is not None:
                manifest.append(f"{d.code}：未收入（质量事件冻结导出）")
                continue
            try:
                r = export_svc.export_deliverable(db, user, p, d)
            except ExportError as e:
                manifest.append(f"{d.code}：未收入（{e.detail}）")
                continue
            z.writestr(r.filename, r.data)
            words += 1
            manifest.append(f"{d.code}：{r.filename}（{STATUS_NAMES.get(d.status, d.status)}）")
        if not items:
            manifest.append("本段没有交付件")
        z.writestr("清单.txt", "\n".join(manifest) + "\n")
    data = buf.getvalue()
    audit.record(db, actor=user.username, action="project.stage_pack", result="allowed", project_code=p.code,
                 phase=phase, deliverables=len(items), word_files=words, size=len(data))
    return Pack(data=data, filename=f"{p.code}-{phase}阶段包-{now:%Y%m%d}.zip")


# ---- 本周简报 ----

def week_start(now: datetime) -> datetime:
    local = now.astimezone(TZ)
    monday = (local - timedelta(days=local.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    return monday


def _counts(rows: list[tuple[str, int]], labels: dict[str, str]) -> dict:
    items = [{"action": a, "label": labels.get(a, a), "count": n} for a, n in sorted(rows, key=lambda x: (-x[1], x[0]))]
    return {"total": sum(n for _, n in rows), "items": items}


def weekly_brief(db: Session, p: Project, now: datetime | None = None) -> dict:
    now = now or utcnow()
    start = week_start(now)
    base = select(AuditLog.action, func.count()).where(
        AuditLog.project_code == p.code, AuditLog.result == "allowed", AuditLog.at >= start, AuditLog.at <= now)
    auto_rows = [(a, n) for a, n in db.execute(base.where(AuditLog.actor == "platform").group_by(AuditLog.action))]
    manual_rows = [(a, n) for a, n in db.execute(
        base.where(AuditLog.actor != "platform", AuditLog.action.in_(MANUAL_LABELS)).group_by(AuditLog.action))]
    auto, manual = _counts(auto_rows, AUTO_LABELS), _counts(manual_rows, MANUAL_LABELS)
    cards = db.scalar(select(func.count()).select_from(Confirmation).where(
        Confirmation.project_id == p.id, Confirmation.status == "open")) or 0
    ordered = sorted(p.deliverables, key=lambda x: (len(x.code), x.code))
    awaiting = [d.code for d in ordered if d.status == "review"]
    cur = p.phase if 0 <= p.phase < len(PHASES) else 0
    unsigned = [d.code for d in _by_phase(db, p)[cur] if d.status != "signed"]
    todo = {"confirmations": cards, "awaiting_sign": awaiting, "phase_unsigned": unsigned}
    text = (f"{p.code} 本周简报（{start:%m-%d}～{now.astimezone(TZ):%m-%d}）：当前阶段 {PHASES[cur]}；"
            f"本周自动完成 {auto['total']} 项，人工处理 {manual['total']} 项；"
            f"待办：确认卡 {cards} 张，待签署 {len(awaiting)} 件"
            + (f"（{'、'.join(awaiting)}）" if awaiting else "")
            + f"，本阶段未签 {len(unsigned)} 件" + (f"（{'、'.join(unsigned)}）" if unsigned else "") + "。")
    return {"project": p.code, "week_start": start.date().isoformat(), "phase": PHASES[cur],
            "auto": auto, "manual": manual, "todo": todo, "text": text}
