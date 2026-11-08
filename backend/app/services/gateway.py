"""模型网关：所有大模型调用的唯一出口。

1. 机密级拦截：发往模型前再检查一次，命中即拒绝。
2. 按能力等级路由到公司白名单模型（推理 / 标准 / 轻量）。
3. 按项目档位加系统提示：严格档一律用占位符，不补充摘要卡以外的客户事实。
4. 未配置模型地址时返回演示回复，便于先在本地跑通流程。

模型为公司私有化部署，自动假名化能力在后续版本中保留开关，默认关闭。
"""
from __future__ import annotations

from dataclasses import dataclass

import httpx

from ..core.config import get_settings
from .guard import GuardResult, check

SYSTEM_BASE = (
    "你是本公司安全咨询团队的内部助手，服务对象是公司顾问，不直接面对客户。"
    "你产出的对客内容都是草稿，须由顾问审核署名。"
    "不得出具等保测评、密评或认证结论，统一使用“差距分析、符合性初判、整改建议”。"
    "法规标准条款只能引用依据库条目，查不到时标注【依据待核实】，不得凭记忆补写条款号。"
    "整改建议必须写明优先级、责任部门、周期、验收标准。"
)
SYSTEM_STRICT = (
    "本项目为严格档：只能使用客户画像和去标识摘要卡，输出一律用占位符"
    "（如【客户】【信息科技部负责人】【系统 A】），缺少的事实写“待现场补充”。"
)


class BlockedError(Exception):
    def __init__(self, result: GuardResult):
        super().__init__(result.summary())
        self.result = result


@dataclass
class Completion:
    text: str
    model: str
    demo: bool


def _model_for(level: str) -> str:
    s = get_settings()
    return {"reasoning": s.llm_model_reasoning, "light": s.llm_model_light}.get(level, s.llm_model_standard)


def build_messages(message: str, tier: str, project_code: str) -> list[dict]:
    system = SYSTEM_BASE + (SYSTEM_STRICT if tier == "strict" else "") + f"当前项目代号：{project_code}。"
    return [{"role": "system", "content": system}, {"role": "user", "content": message}]


def complete(message: str, *, tier: str, project_code: str, level: str = "standard") -> Completion:
    result = check(message)
    if result.blocked:
        raise BlockedError(result)

    s = get_settings()
    model = _model_for(level)
    if not s.llm_base_url:
        return Completion(
            text=("【演示回复】模型地址尚未配置（.env 中的 LLM_BASE_URL）。"
                  "你的问题已通过机密级检查，配置公司白名单大模型后这里会返回正式答复。"),
            model=model, demo=True)

    headers = {"Authorization": f"Bearer {s.llm_api_key}"} if s.llm_api_key else {}
    payload = {"model": model, "messages": build_messages(message, tier, project_code), "temperature": 0.3}
    with httpx.Client(timeout=s.llm_timeout_seconds) as client:
        resp = client.post(s.llm_base_url.rstrip("/") + "/chat/completions", json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    return Completion(text=data["choices"][0]["message"]["content"], model=data.get("model", model), demo=False)
