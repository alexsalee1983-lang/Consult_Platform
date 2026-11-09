"""后台任务 worker：python -m app.worker（docker compose 的 worker 服务）。

循环：领取到期任务 → 执行 → 没有任务时在 Redis 上等唤醒信号（最多 POLL_SECONDS 秒，Redis 不可用就睡眠后轮询）。
- 心跳：每轮写 Redis 键 worker:heartbeat:<编号>（30 秒过期，/api/health 据此显示 worker 是否在线），
  并更新本地文件 /tmp/worker.alive 的时间（容器健康检查用，不依赖 Redis）。
- 收到 SIGTERM / SIGINT 时做完当前任务再退出。
- 不做数据库迁移（由 backend 启动时完成），compose 里 worker 在 backend 健康后才启动。
"""
from __future__ import annotations

import logging
import os
import signal
import socket
import time
from pathlib import Path

from .core.config import get_settings
from .core.db import SessionLocal
from .kb import intake  # noqa: F401  注册 kb.parse 等任务处理器
from .services import jobs

POLL_SECONDS = 5
HEARTBEAT_TTL = 30
HEARTBEAT_PREFIX = "worker:heartbeat:"
ALIVE_FILE = Path(os.environ.get("WORKER_ALIVE_FILE", "/tmp/worker.alive"))

log = logging.getLogger("worker")
_stop = False


def worker_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}"


def _redis():
    try:
        import redis

        return redis.Redis.from_url(get_settings().redis_url, socket_connect_timeout=2, socket_timeout=POLL_SECONDS + 2)
    except Exception:  # noqa: BLE001
        return None


def heartbeat(r, wid: str) -> None:
    try:
        ALIVE_FILE.touch()
    except OSError:
        pass
    if r is not None:
        try:
            r.set(HEARTBEAT_PREFIX + wid, int(time.time()), ex=HEARTBEAT_TTL)
        except Exception:  # noqa: BLE001  Redis 暂时不可用：任务照常执行，健康检查会显示 worker 状态未知
            pass


def online_workers() -> int:
    """在线 worker 数（心跳未过期的）。Redis 不可用时抛异常，由调用方处理。"""
    import redis

    r = redis.Redis.from_url(get_settings().redis_url, socket_connect_timeout=1, socket_timeout=1)
    return sum(1 for _ in r.scan_iter(HEARTBEAT_PREFIX + "*", count=100))


def _wait(r) -> None:
    if r is not None:
        try:
            r.brpop([jobs.WAKE_KEY], timeout=POLL_SECONDS)
            return
        except Exception:  # noqa: BLE001
            pass
    time.sleep(POLL_SECONDS)


def _handle_signal(signum, frame) -> None:  # noqa: ARG001
    global _stop
    _stop = True
    log.info("收到停止信号，做完当前任务后退出")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    wid = worker_id()
    r = _redis()
    log.info("worker %s 启动，任务种类：%s", wid, ", ".join(jobs.registered()))
    while not _stop:
        heartbeat(r, wid)
        try:
            with SessionLocal() as db:
                status = jobs.run_once(db, wid)
        except Exception as e:  # noqa: BLE001  数据库暂时不可用等：记类型，稍后重试
            log.warning("领取或执行任务出错：%s", type(e).__name__)
            time.sleep(POLL_SECONDS)
            continue
        if status is None:
            _wait(r)
        else:
            log.info("任务完成，状态 %s", status)
    if r is not None:
        try:
            r.delete(HEARTBEAT_PREFIX + wid)
        except Exception:  # noqa: BLE001
            pass
    log.info("worker %s 已退出", wid)


if __name__ == "__main__":
    main()
