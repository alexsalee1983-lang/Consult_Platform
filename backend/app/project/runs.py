"""交付链编排（M3 ④）：标准档交付链 3→4→7，平台自写流程控制（决策记录 2026-11-10，不用 LangGraph）。

步骤（《Agent组开发项目方案》第五节）：
  ③ facts  调研访谈 Agent：读本项目证据（纪要、资料、现场观察）→ 写 D3 事实清单
  ④ gap    依据与差距分析 Agent：读 D3、检索依据库 → 差距分析表、业务影响 TOP 5、依据库缺口
           （该 Agent 没有写交付件的授权，产出存在运行记录里，作为下一步输入）
  ⑦ draft  报告与汇报 Agent：读差距分析和 D3 → 写 D4 现状评估报告初稿

规则：
- 每一步是一个后台任务（jobs 种类 agent_run.step），可重复执行：任务开始先看运行状态，不是 queued 就什么也不做。
- Agent 读写项目区一律经工具层 tools.invoke（授权、硬规则、成员检查、审计）：证据经 project.evidence，
  写交付件经 doc.generate，发卡经 confirm.create，检索经 kb.search / basis.lookup（app/agents/knowledge.py）。
  上一步交付件的正文由平台按发起人的成员身份读出交给下一步（依据与差距分析 Agent 没有 doc.generate 授权）。
- 模型调用只经网关 gateway.complete；用量记 usage.record；审计只记编号、步骤、字数、用量，不记提示和产出原文。
- 停下来问：没有任何证据时发“证据不足”卡、停在本步；模型在输出里提出的确认卡照发，本步产出照写，
  停在下一步之前。顾问把本次运行等待的卡都处理完后自动继续，处理结果作为“顾问已确认事项”交给后续步骤。
- 严格档不跑（资料不出客户现场，M4 摘要卡）；同一项目同时只能有一个未结束的运行；
  要写入的交付件（D3、D4）已签署的不跑（422，测试 #41）。
- 取消（测试 #41）：取消与 worker 改运行状态都先锁住运行这一行（SELECT … FOR UPDATE）再判断状态，
  worker 不会把“已取消”改回去。模型返回后、写入交付件和发卡之前再看一次状态，已取消就什么也不写；
  正在写入的那一小段里取消的，本步写完即停，运行保持“已取消”，不再发下一步。
- 最后一步的卡处理完直接结束运行（done），不再排任务（测试 #41）。
- 失败：模型暂时不可用按任务重试（最多 MAX_ATTEMPTS 次），机密级拦截、授权拒绝、输出格式不对等直接失败，
  失败原因只写类别，不含客户原文。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..agents import knowledge, runtime, tools
from ..core import audit
from ..core.db import SessionLocal, utcnow
from ..models import AgentRun, AgentRunStep, Confirmation, Project, ProjectMember, User
from ..services import gateway, guard, jobs, usage
from . import deliverables

JOB_KIND = "agent_run.step"
MAX_ATTEMPTS = 3
ACTIVE = ("queued", "running", "waiting")
EVIDENCE_CHARS = 6000        # 每条证据最多交给模型的字数
INPUT_CHARS = 24000          # 一步的输入材料总字数上限
MAX_CARDS = 3                # 一步最多发几张确认卡


@dataclass(frozen=True)
class Step:
    key: str
    number: int              # 交付链第几步
    agent: str
    deliverable: str         # 写入的交付件；空 = 产出存运行记录
    name: str


STEPS = (
    Step("facts", 3, "research-interview", "D3", "整理事实清单"),
    Step("gap", 4, "compliance-basis", "", "差距分析"),
    Step("draft", 7, "report-writer", "D4", "现状评估报告初稿"),
)
STEP_NAMES = {s.key: f"第 {s.number} 步 {s.name}" for s in STEPS}

OUTPUT_RULES = (
    "输出格式（平台据此解析，必须遵守）：\n"
    "1. 用 Markdown，每一章以“## 章节标题”开头，2～10 章；章节标题不超过 60 字，不要编号前缀以外的符号。\n"
    "2. 每条结论后标注来源：客户证据 [I-01] [D-02] [O-01]，知识库依据【依据：文件名 版本号 条款号｜依据库 ID】"
    "（依据库 ID 只用知识库检索结果里有的），推断 [推断]；不得编造证据编号和依据库 ID。\n"
    "3. 需要顾问拍板或补证时，在全文最后附一个代码块，语言标记写“确认卡”，内容是 JSON 数组，每张卡："
    '{"kind": "方案取舍|来源冲突|证据不足|范围确认", "title": "一句话问题", "options": ["A …", "B …"], '
    '"recommendation": "A", "reason": "推荐理由", "impact": "不同选择的影响"}。最多 3 张；没有就不要附。\n'
    "4. 只写客户代号，不写客户真实名称、人名、口令、IP 等机密级内容。"
)

INSTRUCTIONS = {
    "facts": "任务：交付链第 3 步。根据下面的本项目证据，整理 D3 事实清单：按 SOC 能力域（组织与人员、流程、"
             "技术平台与数据源、度量与改进等）分章列出已确认的事实，每条带证据编号；另起一章列“待补证据与矛盾点”。"
             "只写证据里有的事实，不推测现状。",
    "gap": "任务：交付链第 4 步。以下面的 D3 事实清单为输入，对照评估基线（SOC 能力框架、等保 2.0、行业要求，"
           "依据只能来自知识库检索结果，没有的标【依据待核实】）做差距分析：输出差距分析表（能力项、现状、要求与依据、"
           "差距、证据充分性）、业务影响 TOP 5、依据库缺口。证据不足的项列出来并发“证据不足”确认卡。"
           "用“差距分析、符合性初判”的说法，不出具测评或认证结论。",
    "draft": "任务：交付链第 7 步。根据下面的差距分析和 D3 事实清单，起草 D4 现状评估报告初稿：评估范围与方法、"
             "现状概述、主要差距与业务影响、依据说明、后续建议方向。结论必须来自差距分析，并标注证据编号和依据；"
             "这是初稿，供顾问审核署名，不写最终结论口吻。D4 是交给客户的报告：不写内部工作内容（差距编号 G-xx、"
             "差距分析表原样照搬、确认卡、“测试”“内部”等字样），差距分析的结论改写成报告语言。",
}


class RunError(ValueError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class StepFailed(Exception):
    """本步失败、不再重试。message 只写类别，不含客户原文。"""


class StepRetry(Exception):
    """本步暂时失败（模型不可用、输出格式不对），按任务重试。"""


class StepCancelled(Exception):
    """运行在本步执行期间被取消：不再写入、不再继续。"""


def _lock(db: Session, run: AgentRun) -> str:
    """锁住运行这一行并读最新状态（PostgreSQL 行锁，到本事务提交为止）。"""
    db.refresh(run, with_for_update=True)
    return run.status


# ---- 发起、取消、确认卡处理后继续 ----

def start(db: Session, user: User, project: Project) -> AgentRun:
    action = "run.start"
    if project.tier == "strict":
        _deny(db, user, project, action, "strict")
        raise RunError(422, "严格档项目资料不出客户现场，交付链不在平台上运行（M4 摘要卡）")
    active = db.scalar(select(AgentRun).where(AgentRun.project_id == project.id, AgentRun.status.in_(ACTIVE)))
    if active is not None:
        _deny(db, user, project, action, "active_run")
        raise RunError(409, f"本项目已有未结束的交付链运行（#{active.id}），请等它结束或先取消")
    signed = [s.deliverable for s in STEPS if s.deliverable and getattr(
        deliverables.get_deliverable(db, project, s.deliverable), "status", "") == "signed"]
    if signed:
        _deny(db, user, project, action, "signed")
        raise RunError(422, f"{'、'.join(signed)} 已签署，交付链不能再改写")
    for s in STEPS:
        try:
            runtime.ready_doc(db, s.agent)
        except runtime.AgentNotReady as e:
            _deny(db, user, project, action, "agent_not_ready")
            raise RunError(409, str(e)) from e
    run = AgentRun(project_id=project.id, status="queued", step_index=0, waiting_cards=[], card_ids=[],
                   created_by=user.username)
    db.add(run)
    db.flush()
    audit.record(db, actor=user.username, action=action, result="allowed", project_code=project.code, run_id=run.id)
    db.commit()
    _enqueue(db, run, project)
    db.refresh(run)
    return run


def cancel(db: Session, user: User, project: Project, run: AgentRun) -> AgentRun:
    if _lock(db, run) not in ACTIVE:
        raise RunError(409, "这次运行已经结束，不能取消")
    run.status, run.waiting_cards = "cancelled", []
    run.updated_at = run.finished_at = utcnow()
    audit.record(db, actor=user.username, action="run.cancel", result="allowed", project_code=project.code,
                 run_id=run.id, step=_step_key(run))
    db.commit()
    db.refresh(run)
    return run


def on_card_resolved(db: Session, card: Confirmation) -> AgentRun | None:
    """顾问处理了一张卡：如果某次运行在等这张卡、且它等的卡都处理完了，就继续跑。"""
    runs = db.scalars(select(AgentRun).where(AgentRun.project_id == card.project_id, AgentRun.status == "waiting"))
    for run in runs:
        if card.id not in (run.waiting_cards or []):
            continue
        pending = db.scalars(select(Confirmation.id).where(Confirmation.id.in_(run.waiting_cards),
                                                           Confirmation.status == "open")).all()
        if pending:
            return None
        if _lock(db, run) != "waiting":
            db.commit()
            return None
        finished = run.step_index >= len(STEPS)          # 最后一步发的卡：处理完就结束，不再排任务
        run.status, run.waiting_cards, run.updated_at = ("done" if finished else "queued"), [], utcnow()
        if finished:
            run.finished_at = run.updated_at
        audit.record(db, actor=card.resolved_by or "system", action="run.resume", result="allowed",
                     project_code=card.project.code, run_id=run.id, step=_step_key(run), next=run.status)
        db.commit()
        if not finished:
            _enqueue(db, run, card.project)
        return run
    return None


def list_runs(db: Session, project: Project) -> list[AgentRun]:
    return list(db.scalars(select(AgentRun).where(AgentRun.project_id == project.id).order_by(AgentRun.id.desc())))


def get_run(db: Session, project: Project, run_id: int) -> AgentRun | None:
    run = db.get(AgentRun, run_id)
    return run if run is not None and run.project_id == project.id else None


def _step_key(run: AgentRun) -> str:
    return STEPS[run.step_index].key if run.step_index < len(STEPS) else "end"


def _enqueue(db: Session, run: AgentRun, project: Project) -> None:
    jobs.enqueue(db, JOB_KIND, {"run_id": run.id}, created_by=run.created_by, project_code=project.code,
                 max_attempts=MAX_ATTEMPTS)


def _deny(db: Session, user: User, project: Project, action: str, reason: str) -> None:
    audit.record(db, actor=user.username, action=action, result="denied", project_code=project.code, reason=reason)
    db.commit()


# ---- 输出解析 ----

_CARD_BLOCK = re.compile(r"```\s*(?:确认卡|confirm)\s*\n(.*?)```", re.S | re.I)
_HEADING = re.compile(r"^##\s+(.+?)\s*#*\s*$", re.M)


def parse_output(text: str, default_title: str) -> tuple[list[tuple[str, str]], list[dict]]:
    """把模型输出拆成 [(章节标题, 正文)] 和确认卡列表。确认卡块 JSON 不合法时视为格式错误。"""
    cards: list[dict] = []
    for m in _CARD_BLOCK.finditer(text):
        try:
            data = json.loads(m.group(1))
        except ValueError as e:
            raise StepRetry("确认卡格式不对") from e
        cards += [c for c in (data if isinstance(data, list) else [data]) if isinstance(c, dict)]
    body = _CARD_BLOCK.sub("", text).strip()
    heads = list(_HEADING.finditer(body))
    sections: list[tuple[str, str]] = []
    if not heads:
        if body:
            sections.append((default_title, body))
    else:
        for i, h in enumerate(heads):
            end = heads[i + 1].start() if i + 1 < len(heads) else len(body)
            content = body[h.end():end].strip()
            if content:
                sections.append((h.group(1).strip()[:deliverables.MAX_TITLE], content))
    if not sections:
        raise StepRetry("模型没有给出正文")
    if len(sections) > deliverables.MAX_SECTIONS:
        raise StepRetry("章节过多")
    return sections, cards[:MAX_CARDS]


# ---- 每一步的输入 ----

def _evidence_input(db: Session, user: User, agent: str, project: Project) -> str:
    listing = tools.invoke(db, user, agent, "project.evidence", {}, project.code).get("evidences", [])
    parts, total = [], 0
    for e in listing:
        if total >= INPUT_CHARS:
            parts.append(f"（证据较多，其余 {len(listing) - len(parts)} 条未附正文，需要时按编号索取）")
            break
        r = tools.invoke(db, user, agent, "project.evidence",
                         {"code": e["code"], "limit": min(EVIDENCE_CHARS, INPUT_CHARS - total)}, project.code)
        text = r.get("text", "")
        total += len(text)
        role = f"，受访角色：{e['interviewee_role']}" if e.get("interviewee_role") else ""
        more = "（正文较长，已截取前部）" if r.get("next_offset") else ""
        parts.append(f"### [{e['code']}] {e['title']}{role}{more}\n{text}")
    return "\n\n".join(parts)


def _deliverable_text(db: Session, project: Project, code: str) -> str:
    """平台按发起人的成员身份读交付件当前版本，交给下一步 Agent 作为输入。"""
    d = deliverables.get_deliverable(db, project, code)
    if d is None:
        return ""
    parts = []
    for s in d.sections:
        v = deliverables.get_version(s)
        if v is not None:
            parts.append(f"## {s.title}\n{v.body}")
    return "\n\n".join(parts)[:INPUT_CHARS]


def _resolved_cards(db: Session, run: AgentRun) -> str:
    if not run.card_ids:
        return ""
    cards = db.scalars(select(Confirmation).where(Confirmation.id.in_(run.card_ids),
                                                  Confirmation.status == "resolved").order_by(Confirmation.id))
    lines = [f"- [{c.kind}] {c.title}：{c.resolution}" for c in cards]
    return ("# 顾问已确认事项（必须遵循）\n" + "\n".join(lines)) if lines else ""


def _last_output(run: AgentRun, key: str) -> str:
    for s in reversed(run.steps):
        if s.step == key and s.status == "done":
            return s.output
    return ""


# ---- 执行一步 ----

@jobs.handler(JOB_KIND)
def _step_job(payload: dict, ctx: jobs.JobContext) -> None:
    with SessionLocal() as db:
        run = db.get(AgentRun, int(payload["run_id"]))
        if run is None or run.status not in ("queued", "running"):
            return                                     # 已取消、在等确认卡或已结束：什么也不做
        project = db.get(Project, run.project_id)
        user = db.scalar(select(User).where(User.username == run.created_by))
        member = user is not None and db.scalar(select(ProjectMember).where(
            ProjectMember.project_id == project.id, ProjectMember.user_id == user.id)) is not None
        if _lock(db, run) not in ("queued", "running"):   # 加锁后再看一次：刚被取消的不改回 running
            db.commit()
            return
        if not member or not user.is_active:
            _fail(db, run, project, "发起人已不是项目成员或账号已停用")
            return
        if run.step_index >= len(STEPS):                 # 已经走完的运行直接结束，不让越界异常逃出去
            run.status, run.waiting_cards = "done", []
            run.updated_at = run.finished_at = utcnow()
            db.commit()
            return
        step = STEPS[run.step_index]
        run.status, run.updated_at = "running", utcnow()
        db.commit()
        try:
            _execute(db, user, project, run, step)
        except StepCancelled:
            db.rollback()
            audit.record(db, actor="worker", action="run.step", result="allowed", project_code=project.code,
                         run_id=run.id, step=step.key, agent=step.agent, next="cancelled", reason="cancelled")
            db.commit()
        except StepRetry as e:
            db.rollback()
            if ctx.attempt >= MAX_ATTEMPTS:
                _fail(db, run, project, f"{STEP_NAMES[step.key]}：{e}")
                raise jobs.JobError(str(e), retry=False) from e
            _requeue(db, run)
            raise jobs.JobError(str(e)) from e
        except StepFailed as e:
            db.rollback()
            _fail(db, run, project, f"{STEP_NAMES[step.key]}：{e}")
            raise jobs.JobError(str(e), retry=False) from e
        except Exception as e:  # noqa: BLE001  意外错误：按任务重试，到上限时把运行标为失败，不让它停在 running
            db.rollback()
            if ctx.attempt >= MAX_ATTEMPTS:
                _fail(db, run, project, f"{STEP_NAMES[step.key]}：内部错误（{type(e).__name__}）")
                raise jobs.JobError(type(e).__name__, retry=False) from e
            _requeue(db, run)
            raise


def _requeue(db: Session, run: AgentRun) -> None:
    if _lock(db, run) == "running":                     # 已取消的保持已取消
        run.status, run.updated_at = "queued", utcnow()
    db.commit()


def _fail(db: Session, run: AgentRun, project: Project, reason: str) -> None:
    if _lock(db, run) not in ACTIVE:                    # 已取消的保持已取消
        db.commit()
        return
    run.status, run.error, run.waiting_cards = "failed", reason[:200], []
    run.updated_at = run.finished_at = utcnow()
    audit.record(db, actor="worker", action="run.failed", result="error", project_code=project.code,
                 run_id=run.id, step=_step_key(run), error=reason[:200])
    db.commit()


def _execute(db: Session, user: User, project: Project, run: AgentRun, step: Step) -> None:
    try:
        doc = runtime.ready_doc(db, step.agent)
        card = runtime.project_card(db, user, step.agent, project.code)
    except runtime.AgentNotReady as e:
        raise StepFailed("Agent 暂不可用") from e
    except tools.ToolError as e:
        raise StepFailed(f"读项目卡被拒绝（{e.status}）") from e

    record = AgentRunStep(run_id=run.id, step=step.key, agent_code=step.agent, status="done",
                          prompt_version=doc.version, refs={})
    try:
        if step.key == "facts":
            material = _evidence_input(db, user, step.agent, project)
            if not material:
                _ask_for_evidence(db, user, project, run, step, record)
                return
            inputs, query = f"# 本项目证据\n\n{material}", "SOC 运营现状 事实 调研"
        elif step.key == "gap":
            facts = _deliverable_text(db, project, "D3")
            if not facts:
                raise StepFailed("D3 事实清单还没有正文")
            inputs, query = f"# D3 事实清单（当前版本）\n\n{facts}", "SOC 能力 差距分析 等保 基线 " + facts[:200]
        else:
            gap = _last_output(run, "gap")
            if not gap:
                raise StepFailed("没有差距分析结果")
            facts = _deliverable_text(db, project, "D3")
            inputs = f"# 差距分析（第 4 步产出）\n\n{gap}\n\n# D3 事实清单（当前版本）\n\n{facts}"[:INPUT_CHARS * 2]
            query = "SOC 现状评估报告 差距 建议 " + gap[:200]
        kb_block, refs = knowledge.retrieve(db, user, step.agent, project, query)
    except tools.ToolError as e:
        raise StepFailed(f"工具调用被拒绝（{e.status}）") from e

    confirmed = _resolved_cards(db, run)
    message = "\n\n".join(x for x in (INSTRUCTIONS[step.key], OUTPUT_RULES, confirmed, inputs) if x)
    try:
        c = gateway.complete(message, tier=project.tier, project_code=project.code, level=doc.level,
                             system=runtime.system_prompt(doc, card, kb_block))
    except gateway.BlockedError as e:
        _usage(db, user, project, doc.level, step, "blocked")
        raise StepFailed("输入命中机密级内容，未发往模型") from e
    except gateway.ExternalModelForbidden as e:
        raise StepFailed("当前配置不允许把本项目发往外部模型") from e
    except gateway.GatewayError as e:
        _usage(db, user, project, doc.level, step, "error")
        raise StepRetry("模型暂不可用") from e
    if c.demo:
        raise StepFailed("未配置大模型，交付链不能运行")
    usage.record(db, username=user.username, project_code=project.code, level=doc.level, model=c.model,
                 status="ok", stream=False, completion=c, agent_code=step.agent)
    if guard.check(c.text).blocked:
        raise StepFailed("模型输出命中机密级内容，未保存")
    default_title = deliverables.get_deliverable(db, project, step.deliverable).title if step.deliverable else step.name
    sections, cards = parse_output(c.text, default_title)
    db.refresh(run)                                     # 模型调用期间可能被取消：取消了就什么也不写
    if run.status != "running":
        raise StepCancelled()

    record.model, record.prompt_tokens, record.completion_tokens = c.model[:64], c.prompt_tokens, c.completion_tokens
    record.refs = {"kb_refs": len(refs)}
    try:
        if step.deliverable:
            record.refs.update(_write_deliverable(db, user, project, step, sections))
        else:
            record.output = "\n\n".join(f"## {t}\n{b}" for t, b in sections)
        card_ids = _send_cards(db, user, project, run, step, cards)
    except tools.ToolError as e:
        raise StepFailed(f"写入被拒绝（{e.status}）") from e
    record.refs["cards"] = card_ids
    record.finished_at = utcnow()
    db.add(record)
    if _lock(db, run) != "running":                     # 写入期间被取消：本步记录留下，运行保持已取消
        record.status = "cancelled"
        audit.record(db, actor="worker", action="run.step", result="allowed", project_code=project.code,
                     run_id=run.id, step=step.key, agent=step.agent, sections=len(sections), cards=len(card_ids),
                     next="cancelled", reason="cancelled")
        db.commit()
        return
    run.card_ids = [*run.card_ids, *card_ids]
    run.step_index += 1
    open_ids = [i for i in card_ids if db.get(Confirmation, i).status == "open"]
    if open_ids:
        run.status, run.waiting_cards = "waiting", open_ids
    elif run.step_index >= len(STEPS):
        run.status, run.finished_at = "done", utcnow()
    else:
        run.status = "queued"
    run.updated_at = utcnow()
    audit.record(db, actor="worker", action="run.step", result="allowed", project_code=project.code, run_id=run.id,
                 step=step.key, agent=step.agent, sections=len(sections), length=len(c.text), cards=len(card_ids),
                 prompt_tokens=c.prompt_tokens, completion_tokens=c.completion_tokens, next=run.status)
    db.commit()
    if run.status == "queued":
        _enqueue(db, run, project)


def _usage(db: Session, user: User, project: Project, level: str, step: Step, status: str) -> None:
    usage.record(db, username=user.username, project_code=project.code, level=level, model="", status=status,
                 stream=False, agent_code=step.agent)


def _ask_for_evidence(db: Session, user: User, project: Project, run: AgentRun, step: Step,
                      record: AgentRunStep) -> None:
    """没有任何证据：发“证据不足”卡，停在本步；顾问补了证据、处理卡后重跑本步。"""
    db.refresh(run)
    if run.status != "running":
        raise StepCancelled()
    r = tools.invoke(db, user, step.agent, "confirm.create", {
        "kind": "证据不足", "title": "本项目还没有访谈纪要或客户资料，无法整理 D3 事实清单",
        "options": ["A 已补充访谈纪要或资料，继续整理", "B 暂停交付链"], "recommendation": "A",
        "reason": "第 3 步只能根据本项目证据整理事实，不能推测现状",
        "impact": "选 A 后平台重新读取证据再整理；仍没有证据会再次停下", "deliverable": "D3"}, project.code)
    record.status, record.refs, record.finished_at = "waiting", {"cards": [r["id"]]}, utcnow()
    db.add(record)
    if _lock(db, run) != "running":
        record.status = "cancelled"
        db.commit()
        return
    run.card_ids = [*run.card_ids, r["id"]]
    run.status, run.waiting_cards, run.updated_at = "waiting", [r["id"]], utcnow()
    audit.record(db, actor="worker", action="run.step", result="allowed", project_code=project.code, run_id=run.id,
                 step=step.key, agent=step.agent, cards=1, next="waiting", reason="no_evidence")
    db.commit()


def _write_deliverable(db: Session, user: User, project: Project, step: Step,
                       sections: list[tuple[str, str]]) -> dict:
    """先写大纲再写各章（经 doc.generate）。已有正文的多余章节保留在大纲末尾（不能删有正文的章）；
    与当前版本相同的章不重复写。"""
    d = deliverables.require_deliverable(db, project, step.deliverable)
    titles = [t for t, _ in sections]
    kept = [s.title for s in d.sections if s.seq > len(titles) and s.current_version]
    tools.invoke(db, user, step.agent, "doc.generate",
                 {"action": "outline", "deliverable": step.deliverable, "sections": titles + kept}, project.code)
    db.refresh(d)
    versions = {}
    for seq, (_, body) in enumerate(sections, 1):
        current = deliverables.get_version(deliverables.require_section(d, seq))
        if current is not None and current.body.strip() == body.strip():
            versions[seq] = current.version
            continue
        r = tools.invoke(db, user, step.agent, "doc.generate",
                         {"action": "write", "deliverable": step.deliverable, "section": seq, "body": body,
                          "note": f"交付链{STEP_NAMES[step.key]}"}, project.code)
        versions[seq] = r["version"]
    return {"deliverable": step.deliverable, "versions": versions}


def _send_cards(db: Session, user: User, project: Project, run: AgentRun, step: Step,
                cards: list[dict]) -> list[int]:
    """模型提出的确认卡经 confirm.create 发出；单张卡不合规（422）时跳过，不让整步失败。
    本次运行里顾问已经处理过的同一问题（标题相同）不再发，避免反复停下。"""
    answered = set(db.scalars(select(Confirmation.title).where(
        Confirmation.id.in_(run.card_ids or [-1]), Confirmation.status == "resolved")))
    ids: list[int] = []
    for raw in cards:
        if str(raw.get("title", "")).strip() in answered:
            continue
        args = {k: raw.get(k) for k in ("kind", "title", "options", "recommendation", "reason", "impact")
                if raw.get(k) is not None}
        if step.deliverable:
            args["deliverable"] = step.deliverable
        try:
            r = tools.invoke(db, user, step.agent, "confirm.create", args, project.code)
        except tools.ToolError as e:
            if e.status == 422:
                continue
            raise
        if r["id"] not in ids:
            ids.append(r["id"])
    return ids
