"""模型网关：所有大模型调用的唯一出口（数据保护红线第 2 条）。

1. 机密级拦截：发往模型前再检查一次，命中即拒绝。
2. 按能力等级路由到公司白名单模型（推理 / 标准 / 轻量）。OpenAI 兼容接口（/chat/completions、/models）。
3. 按项目档位加系统提示：严格档一律用占位符，不补充摘要卡以外的客户事实。
4. 超时与重试：连接失败、超时、429、5xx 按指数退避重试；流式只在开始输出前重试，已输出的内容不重发。
5. 流式输出：stream() 逐段产出文本，最后产出用量。
6. 可用性检查：GET /models，结果缓存一段时间，供健康检查和界面状态使用。
7. 自动假名化：开关默认关闭（模型为公司私有化部署），见 services/pseudonym.py。
8. 未配置模型地址时返回演示回复，便于先在本地跑通流程。
9. 模型目录（三期 G2，services/model_catalog.py）：配置了 LLM_MODEL_CATALOG 时按角色（role：generate / review /
   arbitrate）取首选和有序备选，首选调用失败（重试后仍失败）自动换下一个；评审、仲裁排除生成方家族
   （exclude_families），目录里没有别的家族时同家族降级运行并在结果里标 degraded。未配置时与原来完全一致。

用量（tokens、耗时、重试次数）随结果返回，由调用方写入 llm_usage 表；网关本身不记提问和回复内容。
"""
from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field

import httpx

from ..core import metrics
from ..core.config import get_settings
from . import model_catalog
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


class ExternalModelForbidden(Exception):
    """当前接的是外部模型，而这次调用不允许发给外部（严格档项目）。"""


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
    external: bool = False      # 是否发给了公司外部的模型服务
    tool_calls: int = 0         # 本次答复中模型发起的工具调用次数（A7）
    family: str = ""            # 模型家族（只在配置了模型目录时填，G2）
    degraded: bool = False      # 评审 / 仲裁找不到不同家族，同家族降级运行（调用方提高抽检）
    fallbacks: int = 0          # 首选不可用、换了几次备选


@dataclass
class ToolUse:
    """stream() 在执行一次工具调用前产出，name 为函数名（调用方据此提示“正在调用…”）。"""
    name: str


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
    _rerank_last.clear()
    _model_down.clear()


def _client() -> httpx.Client:
    s = get_settings()
    timeout = httpx.Timeout(s.llm_timeout_seconds, connect=s.llm_connect_timeout_seconds)
    return httpx.Client(timeout=timeout, transport=_transport)


def _headers() -> dict:
    key = get_settings().llm_api_key
    return {"Authorization": f"Bearer {key}"} if key else {}


def _url(path: str) -> str:
    return get_settings().llm_base_url.rstrip("/") + path


_model_down: dict[str, float] = {}   # 目录里最近调用失败的模型 → 失败时刻；LLM_HEALTH_TTL_SECONDS 内排到最后再试
ROLES = ("generate", "review", "arbitrate")


def _plan(role: str, level: str, exclude_families: Iterable[str]) -> model_catalog.Plan | None:
    """未配置模型目录（或目录里没有一个已评测的模型）返回 None：照旧按等级用单个模型。"""
    if role not in ROLES:
        raise ValueError(f"unknown role {role}")
    try:
        cat = model_catalog.load()
    except model_catalog.CatalogError as e:
        raise GatewayError(f"模型目录配置有误：{e}") from e
    if cat is None:
        return None
    plan = model_catalog.plan(cat, role, level, exclude_families)
    return plan if plan.models else None


def _ordered(models: tuple[model_catalog.ModelEntry, ...]) -> list[model_catalog.ModelEntry]:
    ttl, now = get_settings().llm_health_ttl_seconds, time.monotonic()
    return sorted(models, key=lambda m: m.id in _model_down and now - _model_down[m.id] < ttl)


def _send_planned(client: httpx.Client, p: _Prepared, plan: model_catalog.Plan | None,
                  stream: bool) -> tuple[httpx.Response, int, model_catalog.ModelEntry | None, int]:
    """按目录的顺序发请求：一个模型重试后仍失败就换下一个。返回 (响应, 总尝试次数, 用上的模型, 换了几次)。"""
    if plan is None:
        resp, attempts = _send(client, p.payload, stream=stream)
        return resp, attempts, None, 0
    total, last = 0, None
    for i, entry in enumerate(_ordered(plan.models)):
        p.model = p.payload["model"] = entry.id
        try:
            resp, attempts = _send(client, p.payload, stream=stream)
        except GatewayError as e:
            total, last = total + e.attempts, e
            _model_down[entry.id] = time.monotonic()
            continue
        _model_down.pop(entry.id, None)
        return resp, total + attempts, entry, i
    assert last is not None
    last.attempts = total
    raise last


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


def uses_external_model() -> bool:
    """当前是否会把请求发给公司外部的模型服务（配置了地址且 LLM_EXTERNAL=true）。"""
    s = get_settings()
    return bool(s.llm_external and s.llm_base_url)


def _prepare(message: str, tier: str, project_code: str, level: str, terms: dict[str, str] | None,
             stream: bool, system: str | None = None, history: list[dict] | None = None) -> _Prepared:
    s = get_settings()
    if tier == "strict" and uses_external_model():
        # 严格档承诺“不发送给任何外部人工智能服务”（客户告知模板），在发出任何请求前拒绝
        raise ExternalModelForbidden("严格档项目不得使用外部模型；当前平台接的是外部测试模型")
    result = check(message)
    if result.blocked:
        raise BlockedError(result)
    # 外部模型强制假名化（决策记录：接入云端模型时标准档必须开启）
    pseudonymize = s.llm_pseudonymize or uses_external_model()
    pseudo = Pseudonymizer.from_terms(terms) if pseudonymize and terms else None
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


def _send(client: httpx.Client, payload: dict, stream: bool, url: str | None = None,
          headers: dict | None = None) -> tuple[httpx.Response, int]:
    """发请求，可重试的错误按指数退避重试。返回 (响应, 尝试次数)；stream=True 时响应未读取，调用方负责关闭。
    url / headers 不传时发到大模型的 /chat/completions；向量化传向量服务的地址。"""
    target = url or _url("/chat/completions")
    service = metrics.upstream_service(target)
    start = time.perf_counter()
    outcome = "error"
    try:
        resp, attempts = _send_with_retry(client, payload, stream, target, headers)
        outcome = "ok"
        return resp, attempts
    finally:
        metrics.UPSTREAM_REQUESTS.labels(service, outcome).inc()
        metrics.UPSTREAM_LATENCY.labels(service).observe(time.perf_counter() - start)


def _send_with_retry(client: httpx.Client, payload: dict, stream: bool, target: str,
                     headers: dict | None) -> tuple[httpx.Response, int]:
    max_attempts = get_settings().llm_max_retries + 1
    last_error = "unknown"
    for attempt in range(1, max_attempts + 1):
        try:
            req = client.build_request("POST", target, json=payload, headers=_headers() if headers is None else headers)
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
             history: list[dict] | None = None, role: str = "generate",
             exclude_families: Iterable[str] = ()) -> Completion:
    """role：generate（生成，默认）/ review（评审、知识审核）/ arbitrate（仲裁）；exclude_families 为生成方
    （仲裁时为生成方和评审方）的模型家族。只在配置了模型目录时起作用。"""
    p = _prepare(message, tier, project_code, level, terms, stream=False, system=system, history=history)
    plan = _plan(role, level, exclude_families)
    if plan is not None:
        p.model = p.payload["model"] = plan.models[0].id
    if not get_settings().llm_base_url:
        return Completion(text=DEMO_TEXT, model=p.model, demo=True, family=plan.models[0].family if plan else "",
                          degraded=plan.degraded if plan else False)
    with _client() as client:
        resp, attempts, entry, fallbacks = _send_planned(client, p, plan, stream=False)
        try:
            data = resp.json()
            text = data["choices"][0]["message"]["content"] or ""
        except (ValueError, KeyError, IndexError, TypeError) as e:
            raise GatewayError("模型返回的结果无法解析", attempts) from e
    prompt_tokens, completion_tokens = _usage(data)
    return Completion(text=p.pseudo.restore(text) if p.pseudo else text, model=data.get("model", p.model),
                      demo=False, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                      latency_ms=_elapsed_ms(p), attempts=attempts, pseudonymized=p.pseudo is not None,
                      external=uses_external_model(), family=entry.family if entry else "",
                      degraded=bool(plan and plan.degraded), fallbacks=fallbacks)


def stream(message: str, *, tier: str, project_code: str, level: str = "standard",
           terms: dict[str, str] | None = None, system: str | None = None,
           history: list[dict] | None = None, tools: list[dict] | None = None,
           run_tool: Callable[[str, str], dict] | None = None) -> Iterator[str | ToolUse | Completion]:
    """逐段产出回复文本（str），最后产出一个 Completion（text 为完整回复，带用量）。

    机密级拦截在第一次迭代时就会抛出 BlockedError；开始输出前的连接问题抛出 GatewayError。
    传 tools（function calling 的函数定义）和 run_tool(函数名, 参数 JSON) → 结果时（二期 A7）：模型返回
    tool_calls 就先产出 ToolUse、执行、把结果作为 tool 消息交回模型，再请求下一轮；用满 LLM_TOOL_MAX_ROUNDS 轮或
    LLM_TOOL_MAX_CALLS 次后，最后一轮不带工具定义，把工具结果整理成一段文字要求直接答复（外部 deepseek-flash 在
    tool_choice=none 时仍只输出调用标记，#179 测试）；最后仍没有答复时给一句提示，不留空回复。工具结果先过机密级检测（命中的只回错误说明）、截断、假名化；
    参数先把占位符换回真名再交给 run_tool。各轮文本连成一条答复，用量相加。
    """
    p = _prepare(message, tier, project_code, level, terms, stream=True, system=system, history=history)
    use_tools = bool(tools and run_tool)
    s = get_settings()
    plan = _plan("generate", level, ())
    if plan is not None:
        p.model = p.payload["model"] = plan.models[0].id
    if use_tools:
        p.payload["tools"] = tools
        p.payload["tool_choice"] = "auto"
        if s.llm_model_tools:                       # 对话带工具时指定的模型优先于目录
            p.model = p.payload["model"] = s.llm_model_tools
            plan = None
    if not get_settings().llm_base_url:
        for i in range(0, len(DEMO_TEXT), 12):
            yield DEMO_TEXT[i:i + 12]
        yield Completion(text=DEMO_TEXT, model=p.model, demo=True, family=plan.models[0].family if plan else "")
        return

    restorer = p.pseudo.stream_restorer() if p.pseudo else None
    parts: list[str] = []
    model, prompt_tokens, completion_tokens = p.model, 0, 0
    calls, rounds, final = 0, 0, False
    entry: model_catalog.ModelEntry | None = None
    fallbacks, first = 0, True

    def emit(text: str) -> Iterator[str]:
        out = restorer.feed(text) if restorer else text
        if out:
            parts.append(out)
            yield out

    with _client() as client:
        while True:
            if first:                            # 只在开始输出前换备选模型；之后各轮沿用同一个模型
                resp, attempts, entry, fallbacks = _send_planned(client, p, plan, stream=True)
                model, first = p.model, False
            else:
                resp, attempts = _send(client, p.payload, stream=True)
            raw: list[str] = []                  # 本轮模型原样输出（假名化状态），交回模型时用
            pending: dict[int, dict] = {}        # 流式下发的工具调用，按 index 拼接
            markup = _MarkupFilter() if use_tools else None
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
                        pt, ct = _usage(chunk)
                        prompt_tokens, completion_tokens = prompt_tokens + pt, completion_tokens + ct
                    for choice in chunk.get("choices") or []:
                        d = choice.get("delta") or {}
                        delta = d.get("content") or ""
                        if delta:
                            text = markup.feed(delta) if markup else delta
                            raw.append(text)
                            yield from emit(text)
                        for tc in d.get("tool_calls") or []:
                            slot = pending.setdefault(tc.get("index", 0), {"id": "", "name": "", "arguments": ""})
                            fn = tc.get("function") or {}
                            slot["id"] = tc.get("id") or slot["id"]
                            slot["name"] += fn.get("name") or ""
                            slot["arguments"] += fn.get("arguments") or ""
            except httpx.HTTPError as e:
                raise GatewayError(f"模型输出中断：{type(e).__name__}", attempts) from e
            finally:
                resp.close()
            garbled = False
            if markup:
                text, found, garbled = markup.finish()
                raw.append(text)
                yield from emit(text)
                for c in found:            # 写在正文里的调用标记还原成工具调用（外部 DeepSeek 多轮后会这样）
                    pending[len(pending) + 1000] = c
            if restorer:
                tail = restorer.flush()
                if tail:
                    parts.append(tail)
                    yield tail
            if final:
                break
            if garbled and not pending and not "".join(raw).strip():
                # 只有解析不出来的调用标记、没有答复：最后一轮，要求直接答复
                final = True
                p.payload = _final_payload(p.payload)
                continue
            if not (use_tools and pending):
                break
            rounds += 1
            todo = [pending[k] for k in sorted(pending)]
            for i, c in enumerate(todo):
                c["id"] = c["id"] or f"call_{rounds}_{i}"
            p.payload["messages"].append({"role": "assistant", "content": "".join(raw), "tool_calls": [
                {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["arguments"]}}
                for c in todo]})
            for c in todo:
                calls += 1
                if calls > s.llm_tool_max_calls:
                    result: object = {"error": f"本次提问的工具调用已达上限（{s.llm_tool_max_calls} 次），请根据已有结果直接答复"}
                else:
                    yield ToolUse(c["name"])
                    args = p.pseudo.restore(c["arguments"]) if p.pseudo else c["arguments"]
                    result = run_tool(c["name"], args)
                p.payload["messages"].append({"role": "tool", "tool_call_id": c["id"],
                                              "content": _tool_content(result, p.pseudo)})
            if rounds >= s.llm_tool_max_rounds or calls >= s.llm_tool_max_calls:
                final = True
                p.payload = _final_payload(p.payload)
    if use_tools and not "".join(parts).strip():
        parts.append(NO_ANSWER)
        yield NO_ANSWER
    yield Completion(text="".join(parts), model=model, demo=False, prompt_tokens=prompt_tokens,
                     completion_tokens=completion_tokens, latency_ms=_elapsed_ms(p), attempts=attempts,
                     pseudonymized=p.pseudo is not None, external=uses_external_model(), tool_calls=calls,
                     family=entry.family if entry else "", fallbacks=fallbacks)


TOOL_RESULT_CHARS = 8000
FINAL_PROMPT = ("（平台提示：本次提问的工具调用已结束。以下是你调用工具得到的结果，请据此直接用中文答复上面的问题；"
                "不要再调用工具，也不要输出任何调用标记。结果里没有的依据按公共规范标注【依据待核实】。）")
NO_ANSWER = "（模型没有给出答复，请重试或换个问法。）"
FINAL_RESULTS_CHARS = 24000


def _final_payload(payload: dict) -> dict:
    """最后一轮：去掉工具定义和工具消息，把各次工具结果（已过检测、假名化）整理成一条用户消息。"""
    names = {c["id"]: c["function"]["name"] for m in payload["messages"] for c in m.get("tool_calls") or []}
    labels = {"kb_search": "知识检索", "basis_lookup": "依据查询", "kb_mapping": "控制映射查询",
              "project_read": "读项目区", "project_evidence": "查证据", "quality_check": "质量检查"}
    msgs, notes = [], []
    for m in payload["messages"]:
        if m["role"] == "tool":
            name = names.get(m.get("tool_call_id"), "")
            notes.append(f"【{labels.get(name, name or '工具')}】{m['content']}")
        elif m["role"] == "assistant" and m.get("tool_calls"):
            if (m.get("content") or "").strip():
                msgs.append({"role": "assistant", "content": m["content"]})
        else:
            msgs.append(m)
    results = "\n\n".join(notes)[:FINAL_RESULTS_CHARS] or "（没有可用的工具结果）"
    msgs.append({"role": "user", "content": f"{FINAL_PROMPT}\n\n{results}"})
    return {k: v for k, v in payload.items() if k not in ("tools", "tool_choice")} | {"messages": msgs}
_MARK = "<\uff5c"                                    # “<｜”：DeepSeek 内部调用标记的开头，正常答复里几乎不会出现
_INVOKE = re.compile(r'invoke\s+name="([\w.]+)"\s*>(.*?)(?=<[^>]*invoke|$)', re.S)
_PARAM = re.compile(r'parameter\s+name="(\w+)"([^>]*)>(.*?)</', re.S)


class _MarkupFilter:
    """从流式正文里截下模型写成文本的工具调用标记（DeepSeek 的 DSML），不外发、不存。

    看到“<｜”起截留到本轮结束；本轮结束时，含 DSML 的解析成工具调用，解析不出来的整段丢弃（garbled），
    不含 DSML 的原样补发。末尾单独的“<”先留着，看下一段再定。"""

    def __init__(self) -> None:
        self.held, self.capturing = "", False

    def feed(self, delta: str) -> str:
        if self.capturing:
            self.held += delta
            return ""
        s, self.held = self.held + delta, ""
        i = s.find(_MARK)
        if i >= 0:
            self.capturing, self.held = True, s[i:]
            return s[:i]
        if s.endswith("<"):
            self.held = "<"
            return s[:-1]
        return s

    def finish(self) -> tuple[str, list[dict], bool]:
        """返回 (要补发的正文, 解析出的调用 [{id, name, arguments}], 是否丢弃了解析不出的标记)。"""
        held, self.held, self.capturing = self.held, "", False
        if "DSML" not in held:
            return held, [], False
        calls = []
        for name, body in _INVOKE.findall(held):
            args: dict = {}
            for key, attrs, value in _PARAM.findall(body):
                value = value.strip()
                if 'string="false"' in attrs:
                    try:
                        value = json.loads(value)
                    except ValueError:
                        pass
                args[key] = value
            calls.append({"id": "", "name": name, "arguments": json.dumps(args, ensure_ascii=False)})
        return "", calls, not calls


def _tool_content(result: object, pseudo: Pseudonymizer | None) -> str:
    """工具结果 → 交回模型的 tool 消息正文：命中机密级的只回错误说明（类别），过长的截断，再假名化。"""
    text = json.dumps(result, ensure_ascii=False, default=str)
    hit = check(text)
    if hit.blocked:
        cats = "、".join(sorted({f.label for f in hit.findings}))
        return json.dumps({"error": f"工具结果含机密级内容（{cats}），未发给模型"}, ensure_ascii=False)
    if len(text) > TOOL_RESULT_CHARS:
        text = text[:TOOL_RESULT_CHARS] + "…（结果过长已截断，请缩小范围或用 limit、offset 分次读取）"
    return pseudo.apply(text) if pseudo else text


# ---- 可用性检查 ----
_health_cache: dict[str, tuple[float, str]] = {}
# 最近一次真实重排调用的结果（地址 -> 失败原因，成功为 None）：欠费（402）、密钥失效时
# 模型列表照样能取到，只有实际调用才看得出来；健康检查据此报异常。
_rerank_last: dict[str, str | None] = {}


# ---- 向量化 ----

EMBED_DIM = 512   # 与数据库向量列一致（迁移 0014_kb_embedding）；换模型时连同迁移一起改


class EmbeddingDisabled(GatewayError):
    pass


@dataclass
class Embedding:
    vectors: list[list[float]]
    model: str
    attempts: int = 1


def embedding_enabled() -> bool:
    return bool(get_settings().embed_base_url)


def _embed_headers() -> dict:
    key = get_settings().embed_api_key
    return {"Authorization": f"Bearer {key}"} if key else {}


def embed(texts: list[str], *, base_url: str = "", model: str = "", dim: int = 0, api_key: str = "") -> Embedding:
    """把文本转成向量。每条先过机密级检测（命中抛 BlockedError，不发出）；返回维度必须是 EMBED_DIM。
    base_url / model / dim / api_key 只给向量模型对比评测（app/kb/embed_compare.py）换候选模型用，不传照旧按配置。"""
    s = get_settings()
    url = base_url or s.embed_base_url
    if not url:
        raise EmbeddingDisabled("向量服务未配置（EMBED_BASE_URL）")
    model, want = model or s.embed_model, dim or EMBED_DIM
    headers = ({"Authorization": f"Bearer {api_key}"} if api_key else {}) if base_url else _embed_headers()
    for t in texts:
        result = check(t)
        if result.blocked:
            raise BlockedError(result)
    payload = {"model": model, "input": texts}
    with _client() as client:
        resp, attempts = _send(client, payload, stream=False, url=url.rstrip("/") + "/embeddings", headers=headers)
        try:
            data = sorted(resp.json()["data"], key=lambda d: d["index"])
            vectors = [[float(x) for x in d["embedding"]] for d in data]
        except (ValueError, KeyError, TypeError) as e:
            raise GatewayError("向量服务返回的结果无法解析", attempts) from e
    if len(vectors) != len(texts) or any(len(v) != want for v in vectors):
        raise GatewayError(f"向量维度或条数不对（期望 {want} 维），需检查 EMBED_MODEL 或做迁移", attempts)
    return Embedding(vectors=vectors, model=model, attempts=attempts)


def embed_availability() -> str:
    s = get_settings()
    if not s.embed_base_url:
        return "not enabled"
    try:
        with httpx.Client(timeout=httpx.Timeout(3.0, connect=2.0), transport=_transport) as client:
            resp = client.get(s.embed_base_url.rstrip("/") + "/models", headers=_embed_headers())
        if resp.status_code != 200:
            return f"down: HTTP {resp.status_code}"
        ids = {m.get("id") for m in (resp.json().get("data") or [])}
        return "ok" if s.embed_model in ids else f"down: 向量服务上没有 {s.embed_model}"
    except (httpx.HTTPError, ValueError) as e:
        return f"down: {type(e).__name__}"


# ---- 重排 ----

class RerankDisabled(GatewayError):
    pass


@dataclass
class Rerank:
    scores: list[float]          # 与输入候选一一对应（按输入顺序），0～1，越高越相关
    model: str
    attempts: int = 1


def rerank_enabled() -> bool:
    return bool(get_settings().rerank_base_url)


def _rerank_headers() -> dict:
    key = get_settings().rerank_api_key
    return {"Authorization": f"Bearer {key}"} if key else {}


def rerank(query: str, documents: list[str]) -> Rerank:
    """给“查询 + 候选片段”逐对打分（交叉编码重排）。候选按 KB_RERANK_MAX_CHARS 截断。
    机密级检测：查询命中抛 BlockedError（不发出）；某个候选命中则不发出、该候选记 0 分（视为不相关），其余照常。
    服务不可用或返回不完整时抛 GatewayError，由调用方退回原排序。"""
    s = get_settings()
    if not s.rerank_base_url:
        raise RerankDisabled("重排服务未配置（RERANK_BASE_URL）")
    result = check(query)
    if result.blocked:
        raise BlockedError(result)
    docs = [d[:max(1, s.kb_rerank_max_chars)] for d in documents]
    send = [i for i, d in enumerate(docs) if not check(d).blocked]
    scores = [0.0] * len(docs)
    if not send:
        return Rerank(scores=scores, model=s.rerank_model, attempts=0)
    payload = {"model": s.rerank_model, "query": query, "documents": [docs[i] for i in send]}
    try:
        with _client() as client:
            resp, attempts = _send(client, payload, stream=False, url=s.rerank_base_url.rstrip("/") + "/rerank",
                                   headers=_rerank_headers())
            try:
                by_index = {int(r["index"]): float(r["relevance_score"]) for r in resp.json()["results"]}
            except (ValueError, KeyError, TypeError) as e:
                raise GatewayError("重排服务返回的结果无法解析", attempts) from e
        if sorted(by_index) != list(range(len(send))):
            raise GatewayError("重排服务返回的条数不对", attempts)
    except GatewayError as e:
        _rerank_last[s.rerank_base_url] = str(e)
        raise
    _rerank_last[s.rerank_base_url] = None
    for j, i in enumerate(send):
        scores[i] = by_index[j]
    return Rerank(scores=scores, model=s.rerank_model, attempts=attempts)


def rerank_availability() -> str:
    """ok / not enabled / down: 原因。最近一次真实重排调用失败（如欠费 HTTP 402）直接报 down，下一次调用成功后恢复。
    外部平台的模型列表可能不列重排模型、欠费时也照样返回，所以外部模式再发一次最小的重排请求探测，
    结果缓存 LLM_HEALTH_TTL_SECONDS 秒，避免健康检查频繁打到收费接口。"""
    s = get_settings()
    if not s.rerank_base_url:
        return "not enabled"
    base = s.rerank_base_url.rstrip("/")
    failed = _rerank_last.get(s.rerank_base_url)
    if failed:
        return f"down: 最近一次重排失败（{failed}）"
    key = "rerank:" + s.rerank_base_url
    cached = _health_cache.get(key)
    if cached and time.monotonic() - cached[0] < s.llm_health_ttl_seconds:
        return cached[1]
    try:
        with httpx.Client(timeout=httpx.Timeout(3.0, connect=2.0), transport=_transport) as client:
            resp = client.get(base + "/models", headers=_rerank_headers())
            if resp.status_code != 200:
                status = f"down: HTTP {resp.status_code}"
            elif s.rerank_model in {m.get("id") for m in (resp.json().get("data") or [])}:
                status = "ok"
            elif not s.rerank_external:
                status = f"down: 重排服务上没有 {s.rerank_model}"
            else:
                probe = client.post(base + "/rerank", headers=_rerank_headers(),
                                    json={"model": s.rerank_model, "query": "健康检查", "documents": ["健康检查"]})
                status = "ok" if probe.status_code == 200 else f"down: 重排请求被拒绝 HTTP {probe.status_code}"
    except (httpx.HTTPError, ValueError) as e:
        status = f"down: {type(e).__name__}"
    _health_cache[key] = (time.monotonic(), status)
    return status


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
            cat = model_catalog.load()
            if cat is None:
                missing = {model_for(lv) for lv in ("reasoning", "standard", "light")} - ids
            else:   # 目录：每个生成档位至少有一个已评测的模型在平台上
                missing = {c[0].id for c in (model_catalog.candidates(cat, "generate", lv)
                                             for lv in model_catalog.LEVELS)
                           if c and not any(m.id in ids for m in c)}
            status = "ok" if not missing else f"down: 模型平台上没有 {', '.join(sorted(missing))}"
    except model_catalog.CatalogError:
        status = "down: 模型目录配置有误"
    except (httpx.HTTPError, ValueError) as e:
        status = f"down: {type(e).__name__}"
    _health_cache[s.llm_base_url] = (time.monotonic(), status)
    return status
