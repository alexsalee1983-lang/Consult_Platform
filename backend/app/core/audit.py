"""审计日志：只追加的哈希链。

- 每条记录的 hash = SHA-256(上一条的 hash + "\\n" + 本条内容的规范 JSON)，prev_hash 存上一条的 hash。
  改动、删除、插入任何一条，从那一条起链就对不上，verify_chain 能指出第一处断点。
- 写入必须串行，否则两条记录会接在同一个前驱上、链会分叉：PostgreSQL 上用事务级 advisory lock
  （多个进程、多个容器副本都有效），进程内再加一把线程锁（SQLite 测试环境）。
- 数据库触发器禁止 UPDATE / DELETE（见迁移 0004_audit_chain）。
- 只记元数据（长度、摘要哈希、命中类别），不记客户原文。

已知限制：只删掉最末尾的若干条，链本身仍然自洽。对策是审计员定期记下 verify 返回的链头哈希（head_hash），
下次核对旧链头仍在链上；S5 再考虑把链头定期写到外部只读存储。
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import threading
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from ..models import AuditLog
from . import metrics
from .db import utcnow

GENESIS_ACTION = "audit.genesis"
GENESIS_PREV = "0" * 64
_LOCK_ID = 774_201_002  # 审计链专用的 advisory lock 编号（迁移用 774_201_001）
_thread_lock = threading.Lock()

# 当前请求的来源地址（等保自查 ②c）：由 ClientIPMiddleware 设置，record() 自动写进 detail 的 ip 字段。
# 后台任务、命令行没有来源地址，不写。
_client_ip: ContextVar[str] = ContextVar("audit_client_ip", default="")


def client_ip_from_scope(scope: dict) -> str:
    """取请求来源地址：前端 nginx 用 X-Real-IP 覆盖为真实对端地址（客户端自带的同名头会被覆盖）；
    没有经过 nginx（本机调试端口）时用直连地址。不是合法 IP 的一律丢弃，免得把任意文本写进审计。"""
    raw = ""
    for k, v in scope.get("headers") or []:
        if k == b"x-real-ip":
            raw = v.decode("latin-1").strip()
            break
    if not raw and scope.get("client"):
        raw = str(scope["client"][0])
    try:
        return str(ipaddress.ip_address(raw))
    except ValueError:
        return ""


class ClientIPMiddleware:
    """纯 ASGI 中间件：把来源地址放进上下文变量，同步接口在线程池里也能读到（anyio 复制上下文）。"""

    def __init__(self, app) -> None:  # noqa: ANN001
        self.app = app

    async def __call__(self, scope, receive, send) -> None:  # noqa: ANN001
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        token = _client_ip.set(client_ip_from_scope(scope))
        try:
            await self.app(scope, receive, send)
        finally:
            _client_ip.reset(token)


def digest(text: str) -> str:
    """内容摘要（16 位），用于在审计里标识一段内容而不记原文。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _at_text(at: datetime) -> str:
    # SQLite 读回来的时间不带时区，按 UTC 处理；PostgreSQL 带时区，统一换算成 UTC
    at = at.replace(tzinfo=timezone.utc) if at.tzinfo is None else at.astimezone(timezone.utc)
    return at.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def chain_hash(prev_hash: str, at: datetime, actor: str, project_code: str, action: str, result: str,
               detail: str) -> str:
    """与迁移 0004_audit_chain 中的 _chain_hash 必须完全一致。"""
    payload = json.dumps([_at_text(at), actor, project_code, action, result, detail], ensure_ascii=False,
                         separators=(",", ":"))
    return hashlib.sha256((prev_hash + "\n" + payload).encode("utf-8")).hexdigest()


def _row_hash(row: AuditLog) -> str:
    return chain_hash(row.prev_hash or "", row.at, row.actor, row.project_code, row.action, row.result, row.detail)


def record(db: Session, *, actor: str, action: str, result: str, project_code: str = "", **detail) -> None:
    """追加一条审计记录并提交。调用前应已提交业务改动（本函数会提交当前事务）。"""
    actor = actor[:64]  # 操作人取自服务端会话中的用户名，不信任客户端传来的值
    ip = _client_ip.get()
    if ip and "ip" not in detail:
        detail["ip"] = ip
    detail_text = json.dumps(detail, ensure_ascii=False)
    with _thread_lock:
        try:
            if db.get_bind().dialect.name == "postgresql":
                # 事务级锁：提交或回滚时自动释放
                db.execute(text("SELECT pg_advisory_xact_lock(:id)"), {"id": _LOCK_ID})
            last = db.scalar(select(AuditLog.hash).where(AuditLog.hash.is_not(None))
                             .order_by(AuditLog.id.desc()).limit(1))
            prev = last or GENESIS_PREV
            row = AuditLog(at=utcnow(), actor=actor, project_code=project_code, action=action, result=result,
                           detail=detail_text, prev_hash=prev)
            row.hash = _row_hash(row)
            db.add(row)
            db.commit()
        except Exception:
            db.rollback()
            metrics.AUDIT_WRITE_FAILURES.inc()
            raise


@dataclass
class VerifyResult:
    ok: bool
    checked: int          # 校验过的链上记录数（含创世记录）
    legacy: int           # 链前记录数（哈希链启用前的旧记录，不在链上）
    head_id: int | None   # 链头（最后一条）的 id 和哈希：审计员可记下，下次核对是否仍在链上
    head_hash: str | None
    error_id: int | None = None
    error: str = ""


def verify_chain(db: Session, expect_head: str | None = None) -> VerifyResult:
    """从创世记录起逐条校验。expect_head：之前记下的链头哈希，校验它仍在链上（防止末尾被删）。"""
    genesis_id = db.scalar(select(AuditLog.id).where(AuditLog.action == GENESIS_ACTION,
                                                     AuditLog.prev_hash == GENESIS_PREV)
                           .order_by(AuditLog.id).limit(1))
    if genesis_id is None:
        return VerifyResult(False, 0, 0, None, None, None, "找不到创世记录（audit.genesis），哈希链未初始化或已被破坏")

    legacy = db.scalar(select(func.count(AuditLog.id)).where(AuditLog.id < genesis_id)) or 0
    prev = GENESIS_PREV
    checked = 0
    head_id = head_hash = None
    seen_expected = expect_head is None
    for row in db.scalars(select(AuditLog).where(AuditLog.id >= genesis_id).order_by(AuditLog.id)
                          .execution_options(yield_per=1000)):
        if row.hash is None:
            return VerifyResult(False, checked, legacy, head_id, head_hash, row.id, "链启用后出现了不在链上的记录（可能被直接插入）")
        if row.prev_hash != prev:
            return VerifyResult(False, checked, legacy, head_id, head_hash, row.id,
                                "与上一条的哈希对不上（上一条被删除、改动，或本条被插入）")
        if _row_hash(row) != row.hash:
            return VerifyResult(False, checked, legacy, head_id, head_hash, row.id, "本条内容与其哈希不符（内容被改动）")
        prev = row.hash
        checked += 1
        head_id, head_hash = row.id, row.hash
        if row.hash == expect_head:
            seen_expected = True
    if not seen_expected:
        return VerifyResult(False, checked, legacy, head_id, head_hash, None,
                            "之前记下的链头不在当前链上（末尾的记录可能被删除）")
    return VerifyResult(True, checked, legacy, head_id, head_hash)
