"""测试库独占锁（issue #382）：第二份 pytest 拿不到锁，不再清掉第一份正在用的库。"""

import os
from pathlib import Path

import pytest

from tests import conftest
from tests.conftest import TEST_DATABASE_URL, on_postgres
from tests.dblock import hold_file_lock, hold_pg_lock


def test_file_lock_is_exclusive(tmp_path: Path):
    lock = tmp_path / "x.lock"
    first = hold_file_lock(lock)
    assert first is not None
    try:
        assert hold_file_lock(lock) is None
    finally:
        first.close()
    again = hold_file_lock(lock)
    assert again is not None
    again.close()


@pytest.mark.skipif(bool(TEST_DATABASE_URL), reason="SQLite 模式才有 test.db 文件锁")
def test_this_run_holds_sqlite_lock():
    assert conftest._db_lock is not None
    assert hold_file_lock(Path(conftest.__file__).parent / "test.db.lock") is None


@on_postgres
def test_this_run_holds_pg_lock():
    assert conftest._db_lock is not None
    assert hold_pg_lock(TEST_DATABASE_URL) is None


@pytest.mark.skipif(not os.environ.get("TEST_KB_DATABASE_URL"), reason="需要知识区分库 PostgreSQL")
def test_this_run_holds_kb_pg_lock():
    assert conftest._kb_db_lock is not None
    assert hold_pg_lock(os.environ["TEST_KB_DATABASE_URL"]) is None
