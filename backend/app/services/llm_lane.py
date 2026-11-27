"""网关优先级（四期 A31⑪）：顾问对话 ＞ 交付链 ＞ 知识后台；知识后台日用量上限为日用量的 80%。

- 通道（lane）：chat（请求里的对话、写稿助手，默认）/ chain（worker 里的交付链、质量评审、一键修订等项目任务）/
  background（worker 里 kb.* 知识后台任务）。worker 按任务种类设置（app/services/jobs.py），其余默认 chat。
- 让行：发往模型前，低优先级通道看高优先级通道有没有正在进行的调用，有就等一会儿（最多 LLM_LANE_WAIT_SECONDS 秒，
  每 POLL 秒看一次），等不到也照常发出，不因让行失败。进行中的调用数记在 Redis（跨 backend 与 worker 进程），
  Redis 不可用时只看本进程内的计数。
- 后台日上限：配置了 LLM_DAILY_TOKENS（0 = 不限）时，知识后台当天（北京时间 0 点起）用量达到
  LLM_DAILY_TOKENS × LLM_BACKGROUND_SHARE（默认 0.8）就拒绝，后台任务按重试规则明天再跑；对话和交付链不受这条限制。
  后台用量按 llm_usage 里不属于任何项目的非流式调用计（知识后台调用不带项目代号）。
不记任何内容，只记计数。
"""
from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone

CHAT, CHAIN, BACKGROUND = "chat", "chain", "background"
ORDER = (CHAT, CHAIN, BACKGROUND)            # 优先级从高到低
POLL = 0.25
KEY = "llm:inflight:"
TTL = 300                                    # 计数键的过期秒数：进程崩溃时不让计数永远挂着
TZ = timezone(timedelta(hours=8))

_current: ContextVar[str] = ContextVar("llm_lane", default=CHAT)
_local: dict[str, int] = {lane: 0 for lane in ORDER}
_mu = threading.Lock()


def current() -> str:
    return _current.get()


@contextmanager
def lane(name: str) -> Iterator[None]:
    token = _current.set(name if name in ORDER else CHAT)
    try:
        yield
    finally:
        _current.reset(token)


def for_job(kind: str) -> str:
    return BACKGROUND if kind.startswith("kb.") else CHAIN


_down_until = 0.0


def _redis():
    """会话存 Redis 时才用 Redis 计数（单元测试用内存会话，只看本进程）；连不上时 60 秒内不再试。"""
    from ..core.config import get_settings

    s = get_settings()
    if s.session_backend != "redis" or time.monotonic() < _down_until:
        return None
    try:
        import redis

        return redis.Redis.from_url(s.redis_url, socket_connect_timeout=0.2, socket_timeout=0.2)
    except Exception:  # noqa: BLE001
        return None


def _shared(op: str, name: str) -> int | None:
    r = _redis()
    if r is None:
        return None
    try:
        if op == "get":
            return int(r.get(KEY + name) or 0)
        n = int(r.incr(KEY + name) if op == "incr" else r.decr(KEY + name))
        r.expire(KEY + name, TTL)
        return n
    except Exception:  # noqa: BLE001  Redis 暂时不可用：只用本进程计数
        global _down_until
        _down_until = time.monotonic() + 60
        return None


def inflight(name: str) -> int:
    shared = _shared("get", name)
    with _mu:
        return max(shared or 0, _local[name])


@contextmanager
def track(name: str | None = None) -> Iterator[None]:
    """标记一次进行中的模型调用（跨进程可见）。"""
    name = name or current()
    with _mu:
        _local[name] += 1
    _shared("incr", name)
    try:
        yield
    finally:
        with _mu:
            _local[name] = max(0, _local[name] - 1)
        _shared("decr", name)


def higher_busy(name: str) -> bool:
    return any(inflight(x) > 0 for x in ORDER[:ORDER.index(name)]) if name in ORDER else False


def wait_turn(name: str | None = None, max_wait: float | None = None) -> float:
    """低优先级通道让行：高优先级有进行中的调用就等，最多 max_wait 秒。返回等了多少秒。"""
    from ..core.config import get_settings

    name = name or current()
    limit = get_settings().llm_lane_wait_seconds if max_wait is None else max_wait
    waited = 0.0
    while limit > 0 and waited < limit and higher_busy(name):
        time.sleep(POLL)
        waited += POLL
    return waited


def day_start(now: datetime | None = None) -> datetime:
    now = (now or datetime.now(TZ)).astimezone(TZ)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def background_cap() -> int:
    from ..core.config import get_settings

    s = get_settings()
    return int(s.llm_daily_tokens * s.llm_background_share) if s.llm_daily_tokens > 0 else 0


def background_used(db, now: datetime | None = None) -> int:
    from sqlalchemy import func, select

    from ..models import LlmUsage

    return int(db.scalar(select(func.coalesce(func.sum(LlmUsage.prompt_tokens + LlmUsage.completion_tokens), 0))
                         .where(LlmUsage.at >= day_start(now), LlmUsage.project_code == "",
                                LlmUsage.stream.is_(False))) or 0)


def background_over(db, now: datetime | None = None) -> bool:
    """知识后台今天的用量是否已到上限（只在当前通道是 background 时有意义）。"""
    cap = background_cap()
    return cap > 0 and background_used(db, now) >= cap
