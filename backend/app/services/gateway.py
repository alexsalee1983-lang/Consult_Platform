"""模型网关：所有大模型调用的唯一出口（数据保护红线第 2 条）。

1. 机密级拦截：发往模型前再检查一次，命中即拒绝。
2. 按能力等级路由到公司白名单模型（推理 / 标准 / 轻量）。OpenAI 兼容接口（/chat/completions、/models）。
3. 按项目档位加系统提示：严格档一律用占位符，不补充摘要卡以外的客户事实。
4. 超时与重试：连接失败、超时、429、5xx 按指数退避重试；流式只在开始输出前重试，已输出的内容不重发。
5. 流式输出：stream() 逐段产出文本，最后产出用量。
6. 可用性检查：GET /models，结果缓存一段时间，供健康检查和界面状态使用。
7. 自动假名化：开关默认关闭（模型为公司私有化部署），见 services/pseudonym.py。
8. 未配置模型地址时返回演示回复，便于先在本地跑通流程。

用量（tokens、耗时、重试次数）随结果返回，由调用方写入 llm_usage 表；网关本身不记提问和回复内容。
"""
from __future__ import annotations

import json
import time
from collections.abc import Iterator
from dataclasses import dataclass, field

import httpx

from ..core.config import get_settings
from .guard import GuardResult, check
from .pseudonym import Pseudonymizer

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
DEMO_TEXT = ("【演示回复】模型地址尚未配置（.env 中的 LLM_BASE_URL）。"
             "你的问题已通过机密级检查，配置公司白名单大模型后这里会返回正式答复。")
RETRY_STATUS = frozenset({429, 500, 502, 503, 504})


class BlockedError(Exception):
    def __init__(self, result: GuardResult):
        super().__init__(result.summary())
        self.result = result


class GatewayError(Exception):
    """模型服务不可用（重试后仍失败）或返回了无法解析的结果。"""

    def __init__(self, message: str, attempts: int = 1):
        super().__init__(message)
        self.attempts = attempts


@dataclass
class Completion:
    text: str
    model: str
    demo: bool
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    attempts: int = 1
    pseudonymized: bool = False


@dataclass
class _Prepared:
    model: str
    payload: dict
    pseudo: Pseudonymizer | None = None
    started: float = field(default_factory=time.monotonic)


# ---- 测试钩子：注入模拟的传输层和睡眠函数 ----
_transport: httpx.BaseTransport | None = None
_sleep = time.sleep


def set_transport(transport: httpx.BaseTransport | None) -> None:
    """测试用：让网关请求发到 httpx.MockTransport，而不是真实地址。"""
    global _transport
    _transport = transport
    _health_cache.clear()


def _client() -> httpx.Client:
    s = get_settings()
    timeout = httpx.Timeout(s.llm_timeout_seconds, connect=s.llm_connect_timeout_seconds)
    return httpx.Client(timeout=timeout, transport=_transport)


def _headers() -> dict:
    key = get_settings().llm_api_key
    return {"Authorization": f"Bearer {key}"} if key else {}


def _url(path: str) -> str:
    return get_settings().llm_base_url.rstrip("/") + path


def model_for(level: str) -> str:
    s = get_settings()
    return {"reasoning": s.llm_model_reasoning, "light": s.llm_model_light}.get(level, s.llm_model_standard)


def build_messages(message: str, tier: str, project_code: str, system: str | None = None,
                   history: list[dict] | None = None) -> list[dict]:
    """system 为 Agent 运行时组装的系统提示（公共规范 + 角色 md + 项目卡）；不传时用平台默认提示。
    严格档的占位符要求无论用哪种系统提示都追加一次。history 为之前的 user / assistant 消息。"""
    base = system if system is not None else SYSTEM_BASE + f"当前项目代号：{project_code}。"
    if tier == "strict":
        base += "\n\n" + SYSTEM_STRICT
    msgs = [{"role": "system", "content": base}]
    msgs += [{"role": m["role"], "content": m["content"]} for m in (history or [])]
    msgs.append({"role": "user", "content": message})
    return msgs


def _prepare(message: str, tier: str, project_code: str, level: str, terms: dict[str, str] | None,
             stream: bool, system: str | None = None, history: list[dict] | None = None) -> _Prepared:
    result = check(message)
    if result.blocked:
        raise BlockedError(result)
    pseudo = Pseudonymizer.from_terms(terms) if get_settings().llm_pseudonymize and terms else None
    text = pseudo.apply(message) if pseudo else message
    if pseudo:
        history = [{**m, "content": pseudo.apply(m["content"])} for m in (history or [])]
        system = pseudo.apply(system) if system else system
    model = model_for(level)
    payload = {"model": model, "messages": build_messages(text, tier, project_code, system, history),
               "temperature": 0.3}
    if stream:
        payload["stream"] = True
        payload["stream_options"] = {"include_usage": True}
    return _Prepared(model=model, payload=payload, pseudo=pseudo)


def _backoff(attempt: int, resp: httpx.Response | None = None) -> None:
    delay = get_settings().llm_retry_backoff_seconds * (2 ** (attempt - 1))
    if resp is not None:  # 服务端给了 Retry-After 时照办，但不超过 30 秒
        try:
            delay = min(max(delay, float(resp.headers.get("retry-after", 0))), 30.0)
        except ValueError:
            pass
    _sleep(delay)


def _send(client: httpx.Client, payload: dict, stream: bool) -> tuple[httpx.Response, int]:
    """发请求，可重试的错误按指数退避重试。返回 (响应, 尝试次数)；stream=True 时响应未读取，调用方负责关闭。"""
    max_attempts = get_settings().llm_max_retries + 1
    last_error = "unknown"
    for attempt in range(1, max_attempts + 1):
        try:
            req = client.build_request("POST", _url("/chat/completions"), json=payload, headers=_headers())
            resp = client.send(req, stream=stream)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.RemoteProtocolError) as e:
            last_error = type(e).__name__
            if attempt < max_attempts:
                _backoff(attempt)
                continue
            raise GatewayError(f"模型服务连接失败：{last_error}", attempt) from e
        if resp.status_code in RETRY_STATUS:
            last_error = f"HTTP {resp.status_code}"
            resp.close()
            if attempt < max_attempts:
                _backoff(attempt, resp)
                continue
            raise GatewayError(f"模型服务暂时不可用：{last_error}", attempt)
        if resp.status_code >= 400:
            resp.close()
            raise GatewayError(f"模型服务拒绝请求：HTTP {resp.status_code}", attempt)
        return resp, attempt
    raise GatewayError(f"模型服务暂时不可用：{last_error}", max_attempts)  # pragma: no cover


def _usage(data: dict) -> tuple[int, int]:
    u = data.get("usage") or {}
    return int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0)


def _elapsed_ms(p: _Prepared) -> int:
    return int((time.monotonic() - p.started) * 1000)


def complete(message: str, *, tier: str, project_code: str, level: str = "standard",
             terms: dict[str, str] | None = None, system: str | None = None,
             history: list[dict] | None = None) -> Completion:
    p = _prepare(message, tier, project_code, level, terms, stream=False, system=system, history=history)
    if not get_settings().llm_base_url:
        return Completion(text=DEMO_TEXT, model=p.model, demo=True)
    with _client() as client:
        resp, attempts = _send(client, p.payload, stream=False)
        try:
            data = resp.json()
            text = data["choices"][0]["message"]["content"] or ""
        except (ValueError, KeyError, IndexError, TypeError) as e:
            raise GatewayError("模型返回的结果无法解析", attempts) from e
    prompt_tokens, completion_tokens = _usage(data)
    return Completion(text=p.pseudo.restore(text) if p.pseudo else text, model=data.get("model", p.model),
                      demo=False, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                      latency_ms=_elapsed_ms(p), attempts=attempts, pseudonymized=p.pseudo is not None)


def stream(message: str, *, tier: str, project_code: str, level: str = "standard",
           terms: dict[str, str] | None = None, system: str | None = None,
           history: list[dict] | None = None) -> Iterator[str | Completion]:
    """逐段产出回复文本（str），最后产出一个 Completion（text 为完整回复，带用量）。

    机密级拦截在第一次迭代时就会抛出 BlockedError；开始输出前的连接问题抛出 GatewayError。
    """
    p = _prepare(message, tier, project_code, level, terms, stream=True, system=system, history=history)
    if not get_settings().llm_base_url:
        for i in range(0, len(DEMO_TEXT), 12):
            yield DEMO_TEXT[i:i + 12]
        yield Completion(text=DEMO_TEXT, model=p.model, demo=True)
        return

    restorer = p.pseudo.stream_restorer() if p.pseudo else None
    parts: list[str] = []
    model, prompt_tokens, completion_tokens = p.model, 0, 0
    with _client() as client:
        resp, attempts = _send(client, p.payload, stream=True)
        try:
            for line in resp.iter_lines():
                if not line.startswith("data:"):
                    continue
                body = line[5:].strip()
                if body == "[DONE]":
                    break
                try:
                    chunk = json.loads(body)
                except ValueError:
                    continue
                model = chunk.get("model") or model
                if chunk.get("usage"):
                    prompt_tokens, completion_tokens = _usage(chunk)
                for choice in chunk.get("choices") or []:
                    delta = (choice.get("delta") or {}).get("content") or ""
                    if delta:
                        out = restorer.feed(delta) if restorer else delta
                        if out:
                            parts.append(out)
                            yield out
        except httpx.HTTPError as e:
            raise GatewayError(f"模型输出中断：{type(e).__name__}", attempts) from e
        finally:
            resp.close()
    if restorer:
        tail = restorer.flush()
        if tail:
            parts.append(tail)
            yield tail
    yield Completion(text="".join(parts), model=model, demo=False, prompt_tokens=prompt_tokens,
                     completion_tokens=completion_tokens, latency_ms=_elapsed_ms(p), attempts=attempts,
                     pseudonymized=p.pseudo is not None)


# ---- 可用性检查 ----
_health_cache: dict[str, tuple[float, str]] = {}


def availability() -> str:
    """ok / demo / down: 原因。结果缓存 LLM_HEALTH_TTL_SECONDS 秒，避免健康检查频繁打到模型平台。"""
    s = get_settings()
    if not s.llm_base_url:
        return "demo (LLM_BASE_URL 未配置)"
    cached = _health_cache.get(s.llm_base_url)
    if cached and time.monotonic() - cached[0] < s.llm_health_ttl_seconds:
        return cached[1]
    try:
        with httpx.Client(timeout=httpx.Timeout(3.0, connect=2.0), transport=_transport) as client:
            resp = client.get(_url("/models"), headers=_headers())
        if resp.status_code != 200:
            status = f"down: HTTP {resp.status_code}"
        else:
            ids = {m.get("id") for m in (resp.json().get("data") or [])}
            missing = {model_for(lv) for lv in ("reasoning", "standard", "light")} - ids
            status = "ok" if not missing else f"down: 模型平台上没有 {', '.join(sorted(missing))}"
    except (httpx.HTTPError, ValueError) as e:
        status = f"down: {type(e).__name__}"
    _health_cache[s.llm_base_url] = (time.monotonic(), status)
    return status
