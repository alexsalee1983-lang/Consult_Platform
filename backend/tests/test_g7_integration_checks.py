"""四期 G7：集成检查——后台任务处理器签名、compose 里 worker 与 backend 的挂载一致。

背景：#307 处理器签名写错，单元测试模拟了调用没发现；#302 worker 少挂一个目录，页面发起的任务必然失败。
"""
import re
from pathlib import Path

import pytest

from app import worker  # noqa: F401  导入后注册 worker 进程里的全部处理器
from app.main import app  # noqa: F401  导入后注册 backend 进程里的全部处理器
from app.services import jobs

ROOT = Path(__file__).resolve().parents[2]


def test_all_registered_handlers_match_call_convention():
    assert jobs.registered(), "没有注册任何处理器"
    jobs.validate_handlers()   # 不抛错即全部符合 fn(payload, ctx)


@pytest.mark.parametrize("fn", [
    lambda payload: None,                      # 少一个参数（#307 的写法）
    lambda payload, ctx, extra: None,          # 多一个必填参数
    lambda *, payload, ctx: None,              # 只收关键字参数
])
def test_validate_rejects_wrong_signature(fn):
    jobs._handlers["test.g7.bad"] = fn
    try:
        with pytest.raises(RuntimeError, match="test.g7.bad"):
            jobs.validate_handlers()
    finally:
        del jobs._handlers["test.g7.bad"]


def test_validate_rejects_async_handler():
    async def fn(payload, ctx):
        return None

    jobs._handlers["test.g7.async"] = fn
    try:
        with pytest.raises(RuntimeError, match="test.g7.async"):
            jobs.validate_handlers()
    finally:
        del jobs._handlers["test.g7.async"]


def _service_volumes(text: str, service: str) -> set[str]:
    m = re.search(rf"^  {service}:\n(.*?)(?=^  \S|\Z)", text, re.S | re.M)
    assert m, f"docker-compose.yml 里没有 {service} 服务"
    block = m.group(1)
    v = re.search(r"^    volumes:\n((?:      .*\n|\s*\n)+)", block, re.M)
    mounts: set[str] = set()
    for line in (v.group(1) if v else "").splitlines():
        line = line.strip()
        if line.startswith("- "):
            mounts.add(line[2:].split("#")[0].strip())
    return mounts


def test_worker_mounts_cover_backend_mounts():
    text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    backend, worker_mounts = _service_volumes(text, "backend"), _service_volumes(text, "worker")
    assert backend, "没解析到 backend 的挂载，检查解析规则"
    def targets(ms: set[str]) -> dict[str, str]:
        return {m.split(":")[1]: m for m in ms}
    missing = set(targets(backend)) - set(targets(worker_mounts))
    assert not missing, f"worker 缺少 backend 已有的挂载目录：{sorted(missing)}"
    for tgt, src in targets(backend).items():
        assert targets(worker_mounts)[tgt] == src, f"{tgt} 在 backend 与 worker 的挂载定义不一致"
