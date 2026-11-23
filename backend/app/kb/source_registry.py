"""来源登记表规则（三期 B16，知识区迁移 k0014）。接口 app/api/kb_sources.py、命令 kb-source-registry 只调这里。

- 生命周期：candidate 候选 → trial 试用 → active 正式 → demoted 降级 → retired 停用；停用后只能重新登记为候选。
- 许可、权威类别必填；等级由类别推出（B27 最小版三级：1 法律法规与监管文件、2 标准、3 参考资料）。
- 转正式只限知识管理员，且不能是登记人本人（页面批准）；降级、停用必须写理由。
- 采集机只抓试用、正式的来源（export_for_collector）；正式来源属已批准来源，入库审核把关（三期计划第八节）。
- 文字字段过机密级检测；审计只记编号、状态和理由长度，不记理由原文。
"""
from __future__ import annotations

from urllib.parse import urlsplit

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core import audit
from ..core.db import utcnow
from ..core.permissions import KNOWLEDGE_ADMIN, roles_of
from ..models import KbSource, KbSourceEvent, User
from ..services import guard

STATUSES = ("candidate", "trial", "active", "demoted", "retired")
STATUS_LABELS = {"candidate": "候选", "trial": "试用", "active": "正式", "demoted": "降级", "retired": "停用"}
TRANSITIONS: dict[str, frozenset[str]] = {
    "candidate": frozenset({"trial", "retired"}),
    "trial": frozenset({"active", "candidate", "retired"}),
    "active": frozenset({"demoted", "retired"}),
    "demoted": frozenset({"active", "retired"}),
    "retired": frozenset({"candidate"}),
}
NEEDS_REASON = frozenset({"demoted", "retired"})
COLLECT_STATUSES = ("trial", "active")
# B27：类别固定七类，从高到低；等级为最小版三级
AUTHORITY_LEVELS: dict[str, int] = {"法律": 1, "行政法规": 1, "监管通报": 1, "国家标准": 2, "行业标准": 2,
                                    "白皮书": 3, "厂商资料": 3}
TEXT_FIELDS = ("name", "publisher", "license", "note")
EDITABLE = ("name", "url", "license", "authority_kind", "publisher", "note")


class SourceError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _host(url: str) -> str:
    try:
        u = urlsplit(url.strip())
        host = u.hostname
    except ValueError:
        u, host = None, None
    if u is None or u.scheme not in ("http", "https") or not host:
        raise SourceError(422, "来源地址必须是 http 或 https 网址")
    return host.lower()


def _validate(fields: dict) -> None:
    if not str(fields.get("name") or "").strip():
        raise SourceError(422, "来源名称必填")
    if not str(fields.get("license") or "").strip():
        raise SourceError(422, "许可必填：写明能否内部引用、是否须注明出处")
    if fields.get("authority_kind") not in AUTHORITY_LEVELS:
        raise SourceError(422, "权威类别必填，只能是：" + "、".join(AUTHORITY_LEVELS))
    result = guard.check("\n".join(str(fields.get(k) or "") for k in TEXT_FIELDS))
    if result.blocked:
        raise SourceError(422, result.summary() + "，请修改后再保存")


def _next_code(db: Session) -> str:
    n = db.scalar(select(func.count(KbSource.id))) or 0
    while True:
        n += 1
        code = f"SR-{n:04d}"
        if db.scalar(select(KbSource.id).where(KbSource.code == code)) is None:
            return code


def get(db: Session, code: str) -> KbSource:
    src = db.scalar(select(KbSource).where(KbSource.code == code))
    if src is None:
        raise SourceError(404, "来源不存在")
    return src


def _url_taken(db: Session, url: str) -> bool:
    return db.scalar(select(KbSource.id).where(KbSource.url == url)) is not None


def create(db: Session, user: User, *, name: str, url: str, license: str, authority_kind: str,
           publisher: str = "", note: str = "") -> KbSource:
    fields = {"name": name, "url": url, "license": license, "authority_kind": authority_kind,
              "publisher": publisher, "note": note}
    host = _host(url)
    _validate(fields)
    url = url.strip()
    if _url_taken(db, url):
        raise SourceError(409, "该地址已登记")
    src = KbSource(code=_next_code(db), name=name.strip(), url=url, host=host, publisher=publisher.strip(),
                   license=license.strip(), authority_kind=authority_kind,
                   authority_level=AUTHORITY_LEVELS[authority_kind], status="candidate", note=note.strip(),
                   created_by=user.username)
    db.add(src)
    db.flush()
    db.add(KbSourceEvent(source_id=src.id, from_status="", to_status="candidate", actor=user.username))
    db.commit()
    audit.record(db, actor=user.username, action="kb.source.create", result="allowed", code=src.code,
                 level=src.authority_level)
    return src


def update(db: Session, user: User, code: str, changes: dict) -> KbSource:
    src = get(db, code)
    changed = {k: v for k, v in changes.items() if k in EDITABLE and v is not None}
    fields = {k: getattr(src, k) for k in EDITABLE} | changed
    host = _host(fields["url"])
    _validate(fields)
    url = str(fields["url"]).strip()
    if url != src.url and _url_taken(db, url):
        raise SourceError(409, "该地址已登记")
    for k in ("name", "license", "publisher", "note"):
        setattr(src, k, str(fields[k]).strip())
    src.url, src.host = url, host
    src.authority_kind, src.authority_level = fields["authority_kind"], AUTHORITY_LEVELS[fields["authority_kind"]]
    src.updated_at = utcnow()
    db.commit()
    audit.record(db, actor=user.username, action="kb.source.update", result="allowed", code=src.code,
                 fields=sorted(changed))
    return src


def transition(db: Session, user: User, code: str, to: str, reason: str = "") -> KbSource:
    src = get(db, code)
    reason = (reason or "").strip()
    if to not in STATUSES:
        raise SourceError(422, "状态只能是：" + "、".join(STATUSES))
    if to not in TRANSITIONS[src.status]:
        raise SourceError(409, f"{STATUS_LABELS[src.status]}不能直接转{STATUS_LABELS[to]}")
    if to == "active":
        if KNOWLEDGE_ADMIN not in roles_of(db, user.id):
            raise SourceError(403, "转正式只限知识管理员批准")
        if user.username == src.created_by:
            raise SourceError(403, "登记人本人不能批准，须另一位知识管理员")
    if to in NEEDS_REASON and not reason:
        raise SourceError(422, f"转{STATUS_LABELS[to]}必须写理由")
    if reason and guard.check(reason).blocked:
        raise SourceError(422, "理由含机密级内容，请修改")
    before = src.status
    src.status, src.updated_at = to, utcnow()
    if to == "active":
        src.approved_by, src.approved_at = user.username, utcnow()
    db.add(KbSourceEvent(source_id=src.id, from_status=before, to_status=to, reason=reason[:300],
                         actor=user.username))
    db.commit()
    audit.record(db, actor=user.username, action="kb.source.status", result="allowed", code=src.code,
                 from_status=before, to_status=to, reason_len=len(reason))
    return src


def events(db: Session, code: str) -> list[KbSourceEvent]:
    src = get(db, code)
    return list(db.scalars(select(KbSourceEvent).where(KbSourceEvent.source_id == src.id)
                           .order_by(KbSourceEvent.id)))


def list_sources(db: Session, status: str = "") -> list[KbSource]:
    q = select(KbSource).order_by(KbSource.authority_level, KbSource.code)
    if status:
        q = q.where(KbSource.status == status)
    return list(db.scalars(q))


def export_for_collector(db: Session) -> list[dict]:
    """下发采集机的清单：只含试用、正式的来源，等级高的在前。"""
    rows = db.scalars(select(KbSource).where(KbSource.status.in_(COLLECT_STATUSES))
                      .order_by(KbSource.authority_level, KbSource.code))
    return [{"code": s.code, "name": s.name, "url": s.url, "host": s.host, "status": s.status,
             "license": s.license, "authority_kind": s.authority_kind, "authority_level": s.authority_level}
            for s in rows]
