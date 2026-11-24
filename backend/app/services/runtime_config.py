"""运行层配置编辑（四期 G3②，docs/4A架构设计.md 5.6）：页面改运行层、填理由、进审计、可回滚，敏感项二次确认。

- 只能改 config_registry.RUNTIME 里登记的键；值按 Settings 字段类型校验。
- 每次改动是一个版本，必须填理由；审计只记键名和新旧值的哈希，不记值本身。
- 含 confirm=True 键（切模型、开自动发布、调高自主度）的版本先进 pending，须另一位系统管理员确认后生效，
  发起人不能确认自己发起的改动（同删除确认的双人规则）；其余版本提交即生效。
- 回滚到版本 N：把生效状态恢复成“版本 N 生效后”的样子（N=0 即全部恢复 .env），生成一个新版本，规则同上。
- 生效：本进程内把覆盖值写到 get_settings() 上；其他进程（另一台后端、worker）在启动、发起运行、
  worker 每轮扫描时调 sync() 追上最新版本。生效配置哈希（config_registry）随之变化。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core import audit
from ..core.config import Settings, get_settings
from ..core.db import utcnow
from ..models.runtime_config import RuntimeConfigItem, RuntimeConfigVersion
from . import config_registry

PENDING, APPLIED, REJECTED = "pending", "applied", "rejected"

_defaults: dict[str, Any] = {}     # 本进程首次覆盖前的 .env 值
_applied: dict[str, Any] = {}      # 本进程当前已写到 settings 上的覆盖
_synced_version = -1


class ConfigError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status, self.message = status, message


def _vsha(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def default_value(key: str) -> Any:
    return _defaults[key] if key in _defaults else getattr(get_settings(), key)


def _validate(key: str, value: Any) -> Any:
    if key not in config_registry.RUNTIME:
        raise ConfigError(422, f"{key} 不是运行层配置，不能在页面修改")
    if value is None:
        return None
    try:
        return TypeAdapter(Settings.model_fields[key].annotation).validate_python(value)
    except ValidationError:
        raise ConfigError(422, f"{key} 的取值类型不对") from None


def _overrides(db: Session, upto: int | None = None) -> dict[str, Any]:
    """按版本号叠加 applied 版本（upto 为止），得到 键 → 覆盖值；值为 None 的键表示已恢复默认，不出现在结果里。"""
    q = (select(RuntimeConfigItem.key, RuntimeConfigItem.value)
         .join(RuntimeConfigVersion, RuntimeConfigVersion.id == RuntimeConfigItem.version_id)
         .where(RuntimeConfigVersion.status == APPLIED)
         .order_by(RuntimeConfigVersion.id, RuntimeConfigItem.id))
    if upto is not None:
        q = q.where(RuntimeConfigVersion.id <= upto)
    state: dict[str, Any] = {}
    for key, value in db.execute(q):
        if value is None:
            state.pop(key, None)
        else:
            state[key] = value
    return state


def _apply(state: dict[str, Any]) -> None:
    s = get_settings()
    for key in set(_applied) | set(state):
        if key not in config_registry.RUNTIME:
            continue
        _defaults.setdefault(key, getattr(s, key))
        setattr(s, key, state[key] if key in state else _defaults[key])
    _applied.clear()
    _applied.update({k: v for k, v in state.items() if k in config_registry.RUNTIME})


def _latest_applied(db: Session) -> int:
    return db.scalar(select(func.max(RuntimeConfigVersion.id)).where(RuntimeConfigVersion.status == APPLIED)) or 0


def sync(db: Session) -> None:
    """把本进程的 settings 追到库里最新的 applied 版本（没有新版本时只查一次最大版本号）。"""
    global _synced_version
    latest = _latest_applied(db)
    if latest == _synced_version:
        return
    _apply(_overrides(db))
    _synced_version = latest


def reset_process_state() -> None:
    """测试用：把覆盖从 settings 上撤掉，恢复 .env 值。"""
    global _synced_version
    _apply({})
    _defaults.clear()
    _synced_version = -1


def _audit(db: Session, actor: str, action: str, v: RuntimeConfigVersion, items: list[RuntimeConfigItem]) -> None:
    detail: dict[str, Any] = {
        "version": v.id, "status": v.status, "keys": [i.key for i in items], "reason_len": len(v.reason),
        "values": [{"key": i.key, "old": _vsha(i.old_value), "new": _vsha(i.value)} for i in items],
        "config_registry_sha": config_registry.effective_sha()}
    if v.rollback_to is not None:
        detail["rollback_to"] = v.rollback_to
    audit.record(db, actor=actor, action=action, result="allowed", **detail)


def _create(db: Session, actor: str, reason: str, changes: dict[str, Any],
            rollback_to: int | None = None) -> RuntimeConfigVersion:
    reason = (reason or "").strip()
    if not reason:
        raise ConfigError(422, "改动运行层配置必须填写理由")
    if not changes:
        raise ConfigError(422, "没有要改动的配置")
    sync(db)
    s = get_settings()
    v = RuntimeConfigVersion(reason=reason[:500], status=PENDING, requested_by=actor, rollback_to=rollback_to,
                             needs_confirm=any(config_registry.RUNTIME[k] for k in changes))
    db.add(v)
    db.flush()
    items = [RuntimeConfigItem(version_id=v.id, key=k, value=val, old_value=getattr(s, k))
             for k, val in sorted(changes.items())]
    db.add_all(items)
    if not v.needs_confirm:
        v.status, v.decided_by, v.decided_at = APPLIED, actor, utcnow()
    db.commit()
    if v.status == APPLIED:
        sync(db)
    _audit(db, actor, "config.runtime.change" if rollback_to is None else "config.runtime.rollback", v, items)
    return v


def propose(db: Session, actor: str, reason: str, values: dict[str, Any]) -> RuntimeConfigVersion:
    """提交一次运行层改动；值为 None 表示恢复 .env 默认值。"""
    changes = {k: _validate(k, val) for k, val in (values or {}).items()}
    return _create(db, actor, reason, changes)


def rollback(db: Session, actor: str, reason: str, to_version: int) -> RuntimeConfigVersion:
    """回滚到版本 to_version 生效后的状态（0 = 全部恢复 .env）。"""
    if to_version:
        target = db.get(RuntimeConfigVersion, to_version)
        if target is None or target.status != APPLIED:
            raise ConfigError(404, "只能回滚到已生效的版本")
    want, now = _overrides(db, to_version), _overrides(db)
    changes = {k: want.get(k) for k in set(want) | set(now) if want.get(k) != now.get(k)}
    if not changes:
        raise ConfigError(409, "当前生效配置已经与该版本一致")
    return _create(db, actor, reason, changes, rollback_to=to_version)


def decide(db: Session, actor: str, version_id: int, approve: bool) -> RuntimeConfigVersion:
    """二次确认（approve=True 生效）或驳回待确认的改动；发起人不能确认或驳回自己的改动。"""
    v = db.get(RuntimeConfigVersion, version_id)
    if v is None:
        raise ConfigError(404, "改动不存在")
    if v.status != PENDING:
        raise ConfigError(409, "该改动已处理")
    if v.requested_by == actor:
        raise ConfigError(403, "发起人不能确认自己发起的改动，请由另一位系统管理员确认")
    items = list(db.scalars(select(RuntimeConfigItem).where(RuntimeConfigItem.version_id == v.id)
                            .order_by(RuntimeConfigItem.id)))
    v.status, v.decided_by, v.decided_at = (APPLIED if approve else REJECTED), actor, utcnow()
    db.commit()
    sync(db)
    _audit(db, actor, "config.runtime.confirm" if approve else "config.runtime.reject", v, items)
    return v


def history(db: Session, limit: int = 100) -> list[dict]:
    versions = list(db.scalars(select(RuntimeConfigVersion).order_by(RuntimeConfigVersion.id.desc()).limit(limit)))
    ids = [v.id for v in versions]
    items: dict[int, list[dict]] = {}
    if ids:
        for i in db.scalars(select(RuntimeConfigItem).where(RuntimeConfigItem.version_id.in_(ids))
                            .order_by(RuntimeConfigItem.id)):
            items.setdefault(i.version_id, []).append({"key": i.key, "value": i.value, "old_value": i.old_value})
    return [{"id": v.id, "reason": v.reason, "status": v.status, "needs_confirm": v.needs_confirm,
             "rollback_to": v.rollback_to, "requested_by": v.requested_by, "decided_by": v.decided_by,
             "created_at": v.created_at, "decided_at": v.decided_at, "items": items.get(v.id, [])} for v in versions]


def overridden_keys() -> set[str]:
    return set(_applied)
