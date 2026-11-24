"""统一的 Agent 调用层（决策记录 2026-11-16）：平台里所有“让某个 Agent 回答一次”都经这里。

平台编排、Agent 只出文字：模型不自主调工具，读写数据由平台经 tools.invoke 代 Agent 完成。
这里只做一次调用的公共部分，业务规则（审计、结论重算、写入）仍在调用方：
- Agent 已登记、已启用、md 可读，否则 AgentUnavailable，不调模型；
- 模型档：md 写了 model_tier 以 md 为准，没写用调用方给的默认档；
- 系统提示默认 = 公共规范 + 角色 md + 调用方附加的规则（交付侧由调用方传入带项目卡的 system）；
- 只经网关 gateway.complete；每次调用记一条用量（不记内容），网关错误照原样抛出；
- role / exclude_families 原样交给网关（三期 G2）：评审、知识审核传 role="review" 和生成方的模型家族，
  仲裁传 role="arbitrate" 和两方家族；网关按模型目录选不同家族的模型，选不出时同家族降级并在 completion 上标记；
- 给了 parse 时校验结构化输出：解析不了就带 RETRY_NOTE 重试一次，仍不行抛 OutputInvalid。演示模式不解析。
以后公司模型支持工具调用（二期 A7），工具循环只加在这里，入口仍是 tools.invoke。
stream()（三期 A15 写稿助手）：同样的公共部分，经 gateway.stream 逐段返回；level_override 为顾问显式选的快慢档，
优先于 md 的 model_tier；拦截与开始前失败照原样抛出，调用方负责审计。
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from ..core.db import SessionLocal
from ..models import Agent, Project
from ..services import gateway, usage
from . import registry

RETRY_NOTE = "（平台说明：上一次输出不符合要求的格式，无法解析。请严格按上面要求的格式重新输出全部内容，不要附加其他说明。）"


class AgentUnavailable(Exception):
    pass


class OutputInvalid(Exception):
    def __init__(self, completion: gateway.Completion):
        self.completion = completion
        super().__init__("Agent 输出格式无法解析")


@dataclass
class Result:
    completion: gateway.Completion
    data: Any
    version: str        # 提示词版本（公共规范 + 角色 md 的内容哈希）
    level: str


def load(db: Session, code: str) -> registry.AgentDoc:
    from . import roster

    code = roster.resolve(db, code)        # C6-P：旧代号已停用时转到新代号
    agent = db.get(Agent, code)
    if agent is None or not agent.enabled:
        raise AgentUnavailable(f"Agent {code} 未登记或已停用")
    from ..services import agent_md   # 四期 F3②e：有回滚指针时按指针取版本库里的 md

    doc = agent_md.effective(db, code)
    if doc is None:
        raise AgentUnavailable(f"Agent {code} 的 md 缺失或 name 不一致")
    return doc


def system_for(doc: registry.AgentDoc, extra: str = "") -> str:
    return f"{doc.common.strip()}\n\n---\n\n{doc.body.strip()}{extra}"


def json_object(text: str) -> dict:
    """取输出里的第一个 JSON 对象；没有或不是对象抛 ValueError。"""
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("no json")
    data = json.loads(m.group(0))          # JSONDecodeError 是 ValueError 的子类
    if not isinstance(data, dict):
        raise ValueError("not object")
    return data


def call(db: Session, code: str, message: str, *, actor: str, doc: registry.AgentDoc | None = None,
         system: str | None = None, extra: str = "", project: Project | None = None, level: str = "standard",
         parse: Callable[[str], Any] | None = None, role: str = "generate",
         exclude_families: Iterable[str] = ()) -> Result:
    doc = doc or load(db, code)
    lv = doc.level if doc.level_set else level
    sys_prompt = system if system is not None else system_for(doc, extra)
    tier, pcode = (project.tier, project.code) if project is not None else ("standard", "")
    exclude = frozenset(exclude_families)

    def once(msg: str) -> gateway.Completion:
        try:
            c = gateway.complete(msg, tier=tier, project_code=pcode, level=lv, system=sys_prompt, role=role,
                                 exclude_families=exclude)
        except gateway.GatewayError as e:
            usage.record(db, username=actor, project_code=pcode, level=lv, model=gateway.model_for(lv),
                         status="error", stream=False, attempts=e.attempts, agent_code=code)
            raise
        usage.record(db, username=actor, project_code=pcode, level=lv, model=c.model, status="ok", stream=False,
                     completion=c, agent_code=code)
        return c

    c = once(message)
    if parse is None or c.demo:
        return Result(c, None, doc.version, lv)
    for retry in (False, True):
        if retry:
            c = once(f"{message}\n\n{RETRY_NOTE}")
        try:
            return Result(c, parse(c.text), doc.version, lv)
        except ValueError:
            continue
    raise OutputInvalid(c)


def stream(db: Session, code: str, message: str, *, actor: str, system: str, project: Project,
           doc: registry.AgentDoc | None = None, level: str = "standard",
           level_override: bool = False) -> Iterator[str | gateway.Completion]:
    """逐段产出回复文本，最后产出 Completion；结束（含中途失败）时记一条用量。调用方提前关闭（停止）不记用量。
    流式响应在请求的数据库会话关闭后才迭代，用量用新会话记。"""
    doc = doc or load(db, code)
    lv = level if level_override or not doc.level_set else doc.level
    gen = gateway.stream(message, tier=project.tier, project_code=project.code, level=lv, system=system)
    try:
        for item in gen:
            if isinstance(item, gateway.Completion):
                with SessionLocal() as s:
                    usage.record(s, username=actor, project_code=project.code, level=lv, model=item.model, status="ok",
                                 stream=True, completion=item, agent_code=code)
            yield item
    except gateway.GatewayError as e:
        with SessionLocal() as s:
            usage.record(s, username=actor, project_code=project.code, level=lv, model=gateway.model_for(lv),
                         status="error", stream=True, attempts=e.attempts, agent_code=code)
        raise
