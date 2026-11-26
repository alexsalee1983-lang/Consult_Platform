"""四期 A28②：D1 启动包表单“从合同导入”（决策记录 2026-11-26：合同要点由 Agent 抽取，成稿仍由程序按表单生成）。

流程：
1. 上传合同（.docx / .pdf / .md / .txt，≤2MB）：先判成员（非成员 404），严格档一律拒收（只走摘要卡路径），
   解析出正文后过机密级检测，命中则不保存（与纪要导入共用 evidence.extract_text）。
2. 正文经 storage.py 暂存在项目区 `<项目代号>/form_import/<随机编号>/contract.txt`，排后台任务 project.form_import。
3. 任务经 agents/call.py（→ gateway）请项目经理 Agent（delivery-pm）只抽取表单的必填项与可预填项，输出 JSON；
   平台只收表单里有的字段、按字段类型整理和截断，结果再过一次机密级检测，存为同目录 result.json，随后删掉合同正文。
4. 前端取回结果回填表单，顾问核对后照常提交，由 forms.submit 程序成稿；本模块不写交付件。
审计只记项目、交付件、字数、正文摘要哈希和抽取到的字段数，不记合同原文和抽取结果。
暂存在项目前缀下，删除项目时随项目区一并清理。
"""
from __future__ import annotations

import json
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..agents import call
from ..core import audit
from ..core.db import SessionLocal
from ..core.permissions import membership
from ..models import Deliverable, Job, Project, User
from ..services import gateway, guard, jobs, storage
from . import deliverables as dsvc
from . import evidence
from .forms import MAX_FIELD, Field, Form, load_form

JOB_KIND = "project.form_import"
AGENT = "delivery-pm"
ACTION = "deliverable.form_import"
KINDS = ("docx", "pdf", "markdown", "text")
KINDS_MSG = " Word（.docx）、PDF（文字版）、Markdown（.md）和纯文本（.txt）"
MAX_CONTRACT_CHARS = 60_000      # 送模型的合同正文上限（超出截断，只影响抽取完整度）

TASK = """

---

## 本次任务：从合同抽取启动包表单要点

下面给你一份咨询服务合同的正文（已经过机密级检测）。只抽取下列表单字段，不起草启动包正文，不编造合同里没有的内容；
合同没写的字段给空字符串。只输出一个 JSON 对象，键为字段键，值为字符串：
- list 类型每行一项；table 类型每行一条、格间用全角竖线“｜”，按给定列顺序；
- 不写口令、密钥、IP、个人联系方式；人员只写岗位或角色；
- 不出具或暗示测评、认证结论。
"""


class ImportError_(ValueError):
    def __init__(self, status: int, detail: object):
        super().__init__(str(detail))
        self.status, self.detail = status, detail


def import_fields(form: Form) -> list[Field]:
    """合同能回答的字段：必填项与可预填项（A28① 的 5 个必填 + 周期、里程碑、交付件、角色、框架）。"""
    return [f for f in form.fields() if f.required or f.prefill]


def _prefix(project: Project, token: str) -> str:
    return f"{project.code}/form_import/{token}/"


def start(db: Session, user: User, project: Project, d: Deliverable, filename: str, data: bytes) -> int:
    form = load_form(d.code)
    if form is None or not import_fields(form):
        raise ImportError_(404, f"{d.code} 没有可导入的表单")
    try:
        text = evidence.extract_text(db, user, project, filename, data, action=ACTION, label="合同文件",
                                     kinds=KINDS, kinds_msg=KINDS_MSG, deliverable=d.code)
    except evidence.EvidenceError as e:
        raise ImportError_(e.status, e.detail) from None
    if not storage.enabled():
        raise ImportError_(503, "对象存储未启用，暂时不能导入合同")
    prefix = _prefix(project, uuid.uuid4().hex)
    storage.put(evidence.AREA, prefix + "contract.txt", text.encode("utf-8"), "text/plain; charset=utf-8")
    job_id = jobs.enqueue(db, JOB_KIND, {"project": project.code, "deliverable": d.code, "user": user.username,
                                         "prefix": prefix}, created_by=user.username, project_code=project.code,
                          max_attempts=2)
    audit.record(db, actor=user.username, action=ACTION, result="queued", project_code=project.code,
                 deliverable=d.code, chars=len(text), digest=evidence.text_digest(text), job=job_id)
    db.commit()
    return job_id


def _cell(v: object) -> str:
    if isinstance(v, list):
        return "｜".join(_cell(x) for x in v)
    if isinstance(v, dict):
        return "｜".join(_cell(x) for x in v.values())
    return "" if v is None else " ".join(str(v).split())


def normalize(form: Form, data: dict) -> dict[str, str]:
    """只收可导入字段；数组按行合并（table 的行可以是数组或对象），按字段上限截断，空值不回填。"""
    out: dict[str, str] = {}
    for f in import_fields(form):
        v = data.get(f.key)
        if isinstance(v, list):
            v = "\n".join(_cell(x) for x in v)
        elif v is None:
            continue
        v = str(v).strip()
        if f.type == "text":
            v = " ".join(v.split())
        if v:
            out[f.key] = v[:MAX_FIELD[f.type]]
    return out


def _message(form: Form, text: str) -> str:
    lines = []
    for f in import_fields(form):
        cols = f"，列：{'｜'.join(f.columns)}" if f.columns else ""
        lines.append(f"- {f.key}（{f.label}，{f.type}{cols}）")
    return "表单字段：\n" + "\n".join(lines) + "\n\n合同正文：\n" + text[:MAX_CONTRACT_CHARS]


def run(db: Session, user: User, project: Project, d: Deliverable, prefix: str) -> dict:
    form = load_form(d.code)
    if form is None:
        raise jobs.JobError("表单不存在", retry=False)
    text = storage.get(evidence.AREA, prefix + "contract.txt").decode("utf-8")
    res = call.call(db, AGENT, _message(form, text), actor=user.username, extra=TASK, project=project,
                    parse=call.json_object)
    values = normalize(form, res.data or {})          # 演示模式没有结构化输出，回填为空
    if values and guard.check("\n".join(values.values())).blocked:
        raise jobs.JobError("抽取结果命中机密级检测，未回填", retry=False)
    storage.put(evidence.AREA, prefix + "result.json", json.dumps({"values": values}, ensure_ascii=False).encode(),
                "application/json")
    storage.delete(evidence.AREA, prefix + "contract.txt")      # 合同正文不留存
    return {"fields": len(values), "demo": bool(res.completion.demo)}


def _jobs_of(db: Session, project: Project, d: Deliverable) -> list[tuple[Job, dict]]:
    rows = db.scalars(select(Job).where(Job.kind == JOB_KIND, Job.project_code == project.code)
                      .order_by(Job.id.desc()).limit(20)).all()
    return [(j, p) for j in rows if (p := json.loads(j.payload or "{}")).get("deliverable") == d.code]


def state(db: Session, project: Project, d: Deliverable) -> dict:
    """最近一次导入：status 为空表示没导入过；done 时带 values（顾问核对后再提交表单）。"""
    last = next(iter(_jobs_of(db, project, d)), None)
    if last is None:
        return {"status": "", "error": "", "values": {}}
    job, payload = last
    values: dict = {}
    if job.status == "done":
        try:
            values = json.loads(storage.get(evidence.AREA, payload["prefix"] + "result.json")).get("values", {})
        except (storage.StorageError, KeyError, ValueError):
            return {"status": "failed", "error": "导入结果已不可用，请重新导入", "values": {}}
    return {"status": job.status, "error": job.last_error, "values": values}


@jobs.handler(JOB_KIND)
def _job(payload: dict, ctx: jobs.JobContext) -> None:
    with SessionLocal() as db:
        project = db.scalar(select(Project).where(Project.code == payload.get("project", "")))
        d = dsvc.get_deliverable(db, project, payload.get("deliverable", "")) if project else None
        user = db.scalar(select(User).where(User.username == payload.get("user", "")))
        if d is None or user is None or not user.is_active or membership(db, project.id, user.id) is None:
            raise jobs.JobError("发起人已不是项目成员或交付件不存在", retry=False)
        try:
            out = run(db, user, project, d, str(payload.get("prefix", "")))
        except (gateway.GatewayError, call.OutputInvalid, call.AgentUnavailable, call.QualificationBlocked) as e:
            db.rollback()
            audit.record(db, actor=user.username, action=ACTION, result="error", project_code=project.code,
                         deliverable=d.code, reason=type(e).__name__)
            db.commit()
            raise jobs.JobError(f"抽取失败（{type(e).__name__}），可重新导入或直接填表", retry=False) from e
        audit.record(db, actor=user.username, action=ACTION, result="allowed", project_code=project.code,
                     deliverable=d.code, **out)
        db.commit()
