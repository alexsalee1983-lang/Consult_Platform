"""Redis 偶发超时（CI 压测时 Redis 停顿超过 2 秒，会话读取 500）：客户端对超时和连接错误自动重试。"""
import redis

from app.core.sessions import RedisSessionStore


def _retry(client: redis.Redis):
    return client.connection_pool.connection_kwargs["retry"]


def test_session_store_retries_timeouts() -> None:
    r = RedisSessionStore("redis://127.0.0.1:6399/0").r
    retry = _retry(r)
    assert retry._retries >= 2
    assert redis.exceptions.TimeoutError in retry._supported_errors
    assert redis.exceptions.ConnectionError in retry._supported_errors
    assert r.connection_pool.connection_kwargs["socket_timeout"] >= 2
    # 建连握手（CLIENT SETINFO）不在重试范围内，不发
    assert r.connection_pool.connection_kwargs["lib_name"] is None


def test_session_get_survives_one_timeout(monkeypatch) -> None:
    store = RedisSessionStore("redis://127.0.0.1:6399/0")
    calls = {"n": 0}

    def flaky(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise redis.exceptions.TimeoutError("Timeout reading from socket")

    monkeypatch.setattr(redis.connection.AbstractConnection, "connect", lambda self: None)
    monkeypatch.setattr(redis.connection.AbstractConnection, "disconnect", lambda self, *a: None)
    monkeypatch.setattr(redis.connection.AbstractConnection, "send_command", lambda *a, **k: None)
    monkeypatch.setattr(redis.connection.AbstractConnection, "read_response", flaky)
    assert store.get("sess:x") is None
    assert calls["n"] >= 2
