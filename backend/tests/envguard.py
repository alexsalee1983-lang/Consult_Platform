"""测试环境隔离（见 tests/test_env_isolation.py）：清掉与 Settings 字段同名（不分大小写）的环境变量。"""
from __future__ import annotations

from collections.abc import MutableMapping

from app.core.config import Settings


def clean_settings_env(environ: MutableMapping[str, str]) -> list[str]:
    fields = {name.upper() for name in Settings.model_fields}
    removed = [k for k in list(environ) if k.upper() in fields]
    for k in removed:
        del environ[k]
    return removed
