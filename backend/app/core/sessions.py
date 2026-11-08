"""服务端会话。

- 会话 ID 是 32 字节随机数，只放在 HttpOnly Cookie 里；Redis 里存它的 SHA-256，
  Redis 数据泄露也拿不到可用的会话 ID。
- 无操作超时：每次请求刷新过期时间（默认 30 分钟）。另有最长有效期（默认 12 小时）。
- 每个会话带一个 CSRF 令牌，修改类请求必须在 X-CSRF-Token 头里带上。
- 生产和测试机用 Redis；单元测试用内存实现（生产环境拒绝内存实现，见 bootstrap.check_settings）。
"""
from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
from dataclasses import asdict, dataclass
from functools import lru_cache
from typing import Protocol

import redis

from .config import get_settings

COOKIE_NAME = "cp_session"
CSRF_HEADER = "X-CSRF-Token"


@dataclass
class SessionData:
    user_id: int
    username: str
    csrf: str
    created_at: float


class SessionStore(Protocol):
    def put(self, key: str, data: SessionData, ttl: int) -> None: ...
    def get(self, key: str) -> SessionData | None: ...
    def touch(self, key: str, ttl: int) -> None: ...
    def delete(self, key: str) -> None: ...
    def delete_user(self, user_id: int) -> None: ...


def _key(token: str) -> str:
    return "sess:" + hashlib.sha256(token.encode()).hexdigest()


class RedisSessionStore:
    def __init__(self, url: str) -> None:
        self.r = redis.Redis.from_url(url, socket_connect_timeout=2, socket_timeout=2)

    def put(self, key: str, data: SessionData, ttl: int) -> None:
        pipe = self.r.pipeline()
        pipe.set(key, json.dumps(asdict(data)), ex=ttl)
        pipe.sadd(f"user_sess:{data.user_id}", key)
        pipe.expire(f"user_sess:{data.user_id}", 86400)
        pipe.execute()

    def get(self, key: str) -> SessionData | None:
        raw = self.r.get(key)
        return SessionData(**json.loads(raw)) if raw else None

    def touch(self, key: str, ttl: int) -> None:
        self.r.expire(key, ttl)

    def delete(self, key: str) -> None:
        self.r.delete(key)

    def delete_user(self, user_id: int) -> None:
        keys = self.r.smembers(f"user_sess:{user_id}")
        if keys:
            self.r.delete(*keys)
        self.r.delete(f"user_sess:{user_id}")


class MemorySessionStore:
    """只用于单元测试。"""

    def __init__(self) -> None:
        self._data: dict[str, tuple[SessionData, float]] = {}
        self._lock = threading.Lock()

    def put(self, key: str, data: SessionData, ttl: int) -> None:
        with self._lock:
            self._data[key] = (data, now() + ttl)

    def get(self, key: str) -> SessionData | None:
        with self._lock:
            item = self._data.get(key)
            if item is None or item[1] <= now():
                self._data.pop(key, None)
                return None
            return item[0]

    def touch(self, key: str, ttl: int) -> None:
        with self._lock:
            if key in self._data:
                self._data[key] = (self._data[key][0], now() + ttl)

    def delete(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)

    def delete_user(self, user_id: int) -> None:
        with self._lock:
            for k in [k for k, (d, _) in self._data.items() if d.user_id == user_id]:
                del self._data[k]


def now() -> float:
    """当前时间，单独成函数便于测试超时。"""
    return time.time()


@lru_cache
def get_store() -> SessionStore:
    s = get_settings()
    if s.session_backend == "memory":
        return MemorySessionStore()
    return RedisSessionStore(s.redis_url)


def _idle_ttl() -> int:
    return get_settings().session_idle_minutes * 60


def create(user_id: int, username: str) -> tuple[str, SessionData]:
    """新建会话，返回 (会话 ID, 会话数据)。登录成功时调用，每次都换新的会话 ID。"""
    token = secrets.token_urlsafe(32)
    data = SessionData(user_id=user_id, username=username, csrf=secrets.token_urlsafe(32), created_at=now())
    get_store().put(_key(token), data, _idle_ttl())
    return token, data


def load(token: str | None) -> SessionData | None:
    """读取并续期会话；不存在、无操作超时或超过最长有效期时返回 None。"""
    if not token:
        return None
    store = get_store()
    key = _key(token)
    data = store.get(key)
    if data is None:
        return None
    if now() - data.created_at > get_settings().session_absolute_hours * 3600:
        store.delete(key)
        return None
    store.touch(key, _idle_ttl())
    return data


def destroy(token: str | None) -> None:
    if token:
        get_store().delete(_key(token))


def destroy_all_for_user(user_id: int) -> None:
    get_store().delete_user(user_id)


def csrf_ok(data: SessionData, header_value: str | None) -> bool:
    return bool(header_value) and secrets.compare_digest(data.csrf, header_value)
