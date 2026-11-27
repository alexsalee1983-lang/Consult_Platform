"""四期 E3③：现场议程与证据不足自动分流（驾驶舱改版方案第五、七节）。无新表，从答案池（survey.pool）和题库现算。

- 范围：本项目已生成的调研包里的题（多个包取并集）。
- 受访角色：题库每题的 respondent_roles；没写的按产品 inputs.personas（C21）的关注域匹配；再没有的取答过这题的角色，
  都没有的归“未指定角色”（只进议程，永不算已访谈、不转人工，免得老题库一访谈就刷卡）。
  某角色“已访谈” = 答案池里有该角色经现场题卡或开放讨论回传的答案；客户资料、客户自评不算访谈。
- 每道未办完的题按三段放进相关角色的现场议程（调研助手按此顺序走）：
  ① 快速确认：已有客户资料 / 客户自评答案但还待确认的封闭题（对方点头或改）；
  ② 议程：跨角色矛盾、没有答案的封闭题（证据不足）、开放讨论题；
  ③ 补问：获取渠道为现场题卡（需观察、演示、判断）且还没有答案的封闭题。
  自动确认、人工确认过的题不进议程；相关角色都已访谈、仍待确认的单条答案留在事实确认单由人确认。
- 证据不足自动分流：题有可预填资料来源、还没有客户资料答案的，同时列进补资料请求；
  相关角色还有没访谈的 → 进其现场议程；都访谈过了 → 有可预填资料的只留补资料请求，没有任何渠道可补的转人工。
- 矛盾：相关角色还有没访谈的 → 进其议程“现场定案”；都访谈过仍矛盾 → 转人工。
- 转人工 = 以调研访谈 Agent 名义经 tools.invoke("confirm.create") 发“证据不足”/“来源冲突”确认卡；卡标题只写题号与控制项，
  同标题未处理的卡不重发。每次回传导入后自动分流（survey.import_results 调 route）。
审计只记题数和卡片编号，不记答案和题面原文。
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..agents import tools
from ..agents.knowledge import product_code
from ..core import audit
from ..models import Confirmation, Project, User
from ..models.survey import SurveyQuestion
from ..products.config import catalog_dir
from . import survey

AGENT = "research-interview"
INTERVIEW_CHANNELS = {"onsite", "discussion"}
UNASSIGNED = "未指定角色"
SECTIONS = ("quick_confirm", "agenda", "followup")


@lru_cache(maxsize=16)
def _personas_file(path: str, mtime: float) -> tuple[tuple[str, tuple[str, ...]], ...]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    inputs = data.get("inputs") if isinstance(data, dict) else None
    raw = inputs.get("personas") if isinstance(inputs, dict) else None
    return tuple((str(p.get("role", "")).strip(), tuple(str(d) for d in p.get("focus") or []))
                 for p in raw or [] if isinstance(p, dict) and str(p.get("role", "")).strip())


def personas(project: Project) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """产品配置 inputs.personas：（角色, 关注域）；配置还没有 inputs 时为空。"""
    code = product_code(project.service)
    path = catalog_dir() / "products" / f"{code.lower()}.yaml"
    if not code or not path.is_file():
        return ()
    return _personas_file(str(path), path.stat().st_mtime)


def _roles(meta: dict, persona: tuple[tuple[str, tuple[str, ...]], ...], talked: list[str]) -> list[str]:
    """题的相关角色：题库受访角色 > personas 关注域 > 已答过的角色 > 未指定角色（永不算已访谈，不转人工）。"""
    if meta.get("roles"):
        return list(dict.fromkeys([*meta["roles"], *talked]))
    by_focus = [r for r, focus in persona if meta.get("domain") and meta["domain"] in focus]
    return list(dict.fromkeys([*by_focus, *talked])) or [UNASSIGNED]


def _answer(db: Session, a) -> dict:
    return {"id": a.id, "role": a.respondent_role, "channel": a.channel, "source": a.source, "value": a.value,
            "label": survey.option_label(db, a.qid, a.value) if a.value else "", "fact_status": a.fact_status}


def build(db: Session, project: Project) -> dict:
    """现算议程：按角色分三段，加补资料请求和转人工清单。"""
    qids = list(dict.fromkeys(qid for p in survey.list_packs(db, project) for qid in p.qids))
    qs = {q.qid: q for q in db.scalars(select(SurveyQuestion).where(SurveyQuestion.qid.in_(qids)))}
    pool = {row["qid"]: row for row in survey.pool(db, project)}
    answers = [a for row in pool.values() for a in row["answers"]]
    interviewed = {a.respondent_role for a in answers if a.channel in INTERVIEW_CHANNELS}
    persona = personas(project)
    roles: dict[str, dict] = {r: {"role": r, **{s: [] for s in SECTIONS}} for r, _ in persona}
    requests: list[dict] = []
    manual: list[dict] = []
    for qid in qids:
        q = qs.get(qid)
        if q is None:
            continue
        meta = survey.question_meta(q)
        row = pool.get(qid, {"status": "", "answers": []})
        live = [a for a in row["answers"] if a.kind == "closed" and a.fact_status in ("pending", "auto", "confirmed")]
        talked = sorted({a.respondent_role for a in row["answers"] if a.channel in INTERVIEW_CHANNELS
                         or a.channel == "self_assess"})
        relevant = _roles(meta, persona, talked)
        todo = [r for r in relevant if r not in interviewed]
        item = {"qid": qid, "control": q.control, "kind": q.kind, "key_control": q.key_control,
                "text": survey.question_json(q, project.industry)["text"], "roles": relevant}
        status = row["status"]
        if status in ("confirmed", "auto"):
            continue
        if q.kind == "open":
            if not any(a.text for a in row["answers"]) and todo:
                _place(roles, todo, "agenda", {**item, "reason": "open"})
            continue
        if status == "conflict":
            entry = {**item, "reason": "conflict", "answers": [_answer(db, a) for a in live]}
            if todo:
                _place(roles, todo, "agenda", entry)
            else:
                manual.append({**entry, "card_kind": "来源冲突"})
            continue
        if status == "pending":
            if todo and not any(a.channel in INTERVIEW_CHANNELS for a in live):
                _place(roles, todo, "quick_confirm", {**item, "reason": "confirm",
                                                      "answers": [_answer(db, a) for a in live]})
            continue
        # 没有可用答案：证据不足
        materials = [] if any(a.source == "document" for a in row["answers"]) else list(meta.get("prefill") or [])
        if materials:
            requests.append({**item, "materials": materials})
        if todo:
            onsite = meta.get("channel") == "onsite"
            _place(roles, todo, "followup" if onsite else "agenda", {**item, "reason": "observe" if onsite else "insufficient"})
        elif not materials:
            manual.append({**item, "reason": "insufficient", "card_kind": "证据不足"})
    if manual:                       # 已发过的卡带上卡号（标题只含题号与控制项）
        cards = {c.title: c for c in db.scalars(select(Confirmation).where(
            Confirmation.project_id == project.id, Confirmation.agent_code == AGENT,
            Confirmation.title.in_([_card(i)["title"] for i in manual])))}
        for i in manual:
            c = cards.get(_card(i)["title"])
            i["card_id"], i["card_status"] = (c.id, c.status) if c else (None, "")
    out_roles = [{**r, "interviewed": r["role"] in interviewed} for r in roles.values()
                 if any(r[s] for s in SECTIONS)]
    return {"roles": out_roles, "requests": requests, "manual": manual,
            "counts": {"questions": len(qids), "requests": len(requests), "manual": len(manual),
                       **{s: sum(len(r[s]) for r in out_roles) for s in SECTIONS}}}


def _place(roles: dict[str, dict], targets: list[str], section: str, item: dict) -> None:
    for r in targets:
        roles.setdefault(r, {"role": r, **{s: [] for s in SECTIONS}})[section].append(item)


def _card(item: dict) -> dict:
    if item["card_kind"] == "来源冲突":
        return {"kind": "来源冲突", "title": f"调研来源冲突 · {item['qid']}（{item['control']}）",
                "options": ["A 以可信度最高的来源为准，在事实确认单确认", "B 再约相关角色或补现场证据后定案"],
                "recommendation": "B", "reason": "相关受访角色都已访谈，答案池里这道题的各来源仍不一致",
                "impact": "选 A 在事实确认单确认一条后矛盾解除；选 B 该题留在答案池待补，D3 事实清单暂不写这一项"}
    return {"kind": "证据不足", "title": f"调研证据不足 · {item['qid']}（{item['control']}）",
            "options": ["A 按“证据不足”写入差距分析，整改建议里列补证", "B 另行约谈或补资料后再定"],
            "recommendation": "A", "reason": "相关受访角色都已访谈，也没有可预填的资料来源，这道题仍没有可用答案",
            "impact": "选 A 该项按证据不足处理、不作符合性初判；选 B 该题继续留在答案池待补"}


def route(db: Session, user: User, project: Project) -> dict:
    """自动分流：转人工的题发确认卡（同标题未处理的不重发），返回议程并在转人工条目上带卡号。"""
    plan = build(db, project)
    ids: list[int] = []
    for item in plan["manual"]:
        try:
            r = tools.invoke(db, user, AGENT, "confirm.create", _card(item), project.code)
        except tools.ToolError:      # 调研访谈 Agent 未启用或未授权：留在转人工清单，不发卡
            continue
        item["card_id"] = r["id"]
        ids.append(r["id"])
    if plan["manual"] or plan["requests"]:
        audit.record(db, actor=user.username, action="survey.route", result="ok", project_code=project.code,
                     manual=len(plan["manual"]), requests=len(plan["requests"]), card_ids=ids,
                     qids=[i["qid"] for i in plan["manual"]])
    return plan
