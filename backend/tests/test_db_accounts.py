"""生产环境不用数据库超级用户连库（M5 ⑧）。CI 的测试库账号 consult 是超级用户，正好用来验证拒绝启动。"""
import pytest

from app.core.bootstrap import BootstrapError, check_db_accounts
from app.core.config import Settings
from app.core.db import engine, kb_engine


def _settings(env: str) -> Settings:
    return Settings(_env_file=None, app_env=env)


def test_non_prod_skips_check():
    check_db_accounts(_settings("test"), [engine, kb_engine])


def test_prod_refuses_superuser():
    if engine.dialect.name != "postgresql":
        check_db_accounts(_settings("prod"), [engine])   # SQLite 没有数据库账号，不检查
        pytest.skip("只在 PostgreSQL 上检查账号")
    with pytest.raises(BootstrapError, match="超级用户"):
        check_db_accounts(_settings("prod"), [engine])
