"""测试库独占锁（issue #382）。

两份 pytest 共用同一个测试库时，后起的一份会在启动时清库（SQLite 删 test.db、PostgreSQL DROP SCHEMA），
把先起那份刚建的用户、项目和后台任务清掉，表现为随机的登录 401、评审任务没跑、409 等偶发失败。
conftest 在清库前先拿锁，拿不到就直接退出并说明原因，不再静默互相踩库。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import IO, Any

# PostgreSQL 会话级咨询锁的键（同一数据库内唯一即可）
PG_LOCK_KEY = 382_0001

BUSY_HINT = ("另一份 pytest 正在使用同一个测试库（{where}）。同一测试库同一时刻只能跑一份："
             "等前一份结束，或给这一份换一个库（SQLite 换工作目录，PostgreSQL 换 TEST_DATABASE_URL / TEST_KB_DATABASE_URL）。")


def hold_file_lock(path: Path) -> IO[bytes] | None:
    """对 path 加非阻塞独占锁，成功返回打开的文件（调用方保持引用直到进程结束），被占用返回 None。"""
    f = open(path, "a+b")  # noqa: SIM115 - 锁随文件句柄存活
    try:
        if os.name == "nt":
            import msvcrt

            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def hold_pg_lock(url: str) -> Any | None:
    """在 url 指向的库上取会话级咨询锁，成功返回持锁的连接（调用方保持引用），被占用返回 None。"""
    from sqlalchemy import create_engine, text

    eng = create_engine(url, pool_size=1, max_overflow=0)
    conn = eng.connect()
    ok = conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": PG_LOCK_KEY}).scalar()
    conn.commit()
    if not ok:
        conn.close()
        eng.dispose()
        return None
    return conn
