"""四期 B41：知识缺口闭环补齐（平台规划刷新设计第七节、第十一节第 1、5 条；知识中心改版方案第七节第 1 个闭环）。

缺口池的落表和每周计划仍在 app/kb/gap_pool.py；本模块只补闭环里缺的几环，全部是程序规则，模型只在 ③ 经网关调用：

① feed：知识健康缺格 → 缺口。四格就绪度设了目标（KB_READINESS_TARGETS）且低于目标的格，对应评测集里没就绪的槽位
   （缺槽、已下线）按条目 key 登记“业务需求”缺口，写明缺哪类资料（need）；槽位补齐后自动关闭（published）。
② feed：交付件引用的知识编号库里没有 → 按编号登记“使用信号”缺口（只取来源标注里 found=false 的知识编号，
   不读章节正文；事件编号是交付件 id 的哈希，知识侧看不到项目）；条目入库后自动关闭。
   外部变化、生命周期两类信号的产生方也在 feed：
   - 外部变化（法规换版）：现行依据写了“被谁替代”而库里没有替代文件的原文 → 按替代文件名登记，入库后自动关闭；
   - 生命周期（条目到期）：依据 90 天内废止、或依据原文（SRC）复审日期已到 → 按条目编号登记，不再到期后自动关闭。
③ recommend_week：来源推荐。来源确认队列里还没有正文网址的缺口交知识库建设 Agent（kb-builder 模式 F，
   app/kb/source_list.py，经 agents/call.py → 网关）列清单；程序硬校验：网址必须在官方域名清单（sources.is_official）
   和来源登记表前缀内、不能是来源登记的栏目页本身。通过的写进缺口 source_url（source_via=agent），来源转正式后
   每周计划直接排进下发包。命中率 = 采到的推荐 ÷ 已有结果的推荐（近 90 天）；样本够且低于 KB_RECOMMEND_MIN_HIT
   自动停用（审计 result=paused），不再调模型，直到命中率回升（旧样本出窗）。
④ classify / expand_listings：栏目页两级取正文，在平台侧做（决策记录 2026-11-22 红线 2 例外条款，采集机规则和代码不动）：
   回传批次里的网页按链接占比判为栏目页，栏目页不入库；从中解析出白名单内的正文链接，登记为外部变化缺口并直接排进
   下一轮下发包（plan=direct, source_via=listing）。已登记过的网址不重复登记。
⑤ 获取失败计次在 gap_pool.note_failure（满 3 次标“暂无可用来源”）；retry_unavailable 每月把它们放回计划再试一次；
   知识健康格里缺槽对应的缺口暂无可用来源时，readiness 标 reason=unavailable。
⑥ check_intake：一批回传导入后，非栏目页文件里正文率低于 KB_BODY_RATE_MIN、或入库数为 0，推入库报警卡（kb_alarms）
   给知识管理员（我的任务“入库报警”）；同类未处理的并成一张；下一批正常入库时平台自动关闭。

不存原文：缺口键是编号、网址或公开法规名称；报警卡只存批次哈希前缀和计数；审计只记数量。
"""
from __future__ import annotations

import hashlib
import re
from collections import Counter
from datetime import date, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urldefrag, urljoin, urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core import audit
from ..core.config import get_settings
from ..core.db import utcnow
from ..models import KbAlarm, KbGap, KbSource
from . import acquisition as acq
from . import gap_pool, parse, refs, sources

NEEDS = ("法规", "标准", "样例", "条目")
LIFECYCLE_DAYS = 90             # 依据多少天内废止算“到期”
RETRY_DAYS = 30                 # 暂无可用来源多少天后再试
LISTING_MIN_LINKS = 5           # 栏目页：白名单内链接至少这么多
LISTING_LINK_SHARE = 0.5        # 且链接文字占页面文字的比例不低于此值
MAX_LINKS_PER_LISTING = 50
MAX_NEXT_ROUND = 200
BODY_MIN_CHARS = 200            # 解析出的文字不少于这么多算正文页
RECOMMEND_WINDOW_DAYS = 90
RECOMMEND_EVERY_DAYS = 7        # 同一缺口一周最多推荐一次
ALARM_KINDS = {"zero_intake": "入库数为 0", "low_body": "正文率低于门槛"}
_STD_CODE = re.compile(r"^(?:GB/T|GB/Z|GB|JR/T|GM/T|DL/T|YD/T|GA/T|GA)\s*\d", re.I)
_SAMPLE_TYPES = ("TPL", "GLD", "CAS", "ENF", "BMK")


def need_of(key: str, layer: str = "", doc_type: str = "") -> str:
    """缺口缺哪类资料：标准号 → 标准；依据层（L4）或依据原文 → 法规；模板、样例、案例类 → 样例；其余 → 条目。"""
    if _STD_CODE.match(key or ""):
        return "标准"
    parts = (key or "").split("-")
    if refs.is_legacy(key):
        layer, doc_type = layer or parts[0], doc_type or parts[-2]
    if doc_type in _SAMPLE_TYPES:
        return "样例"
    if layer == "L4" or doc_type == "SRC":
        return "标准" if "STD" in parts else "法规"
    return "条目"


def _set_need(db: Session, keys: dict[str, str]) -> None:
    if not keys:
        return
    for g in db.scalars(select(KbGap).where(KbGap.gap_key.in_(list(keys)), KbGap.status == "open")):
        if not g.need:
            g.need = keys[g.gap_key]
    db.commit()


# ---- ① ② 及外部变化、生命周期信号 ----

def _readiness_signals(db: Session, today: date) -> tuple[list[acq.Signal], dict[str, str], set[str]]:
    """低于目标的四格 → 没就绪的槽位。返回（信号，需要类型，这次看过的槽位 key 及其是否已就绪由调用方再查）。"""
    from ..services import indicators_v4
    from . import readiness

    evalsets = readiness.matrix(db)["evalsets"]
    below = [g for g in indicators_v4.readiness_grids(evalsets) if g["status"] == "below"]
    ref = f"ready-{today:%Y%m}"
    sigs: list[acq.Signal] = []
    need: dict[str, str] = {}
    seen: set[str] = set()
    for g in below:
        for es in evalsets:
            if str(es.get("product", "")).upper() != g["product"] or es.get("industry", "") not in ("", g["industry"]):
                continue
            for ch in es["chapters"]:
                for s in ch["slots"]:
                    seen.add(s["key"])
                    if s["state"] in ("missing", "retired") and s["key"] not in need:
                        need[s["key"]] = need_of(s["key"], s.get("layer", ""), s.get("doc_type", ""))
                        sigs.append(acq.Signal(s["key"], "business", ref, impact=1))
    return sigs, need, seen


def _citation_signals(db: Session) -> tuple[list[acq.Signal], dict[str, str]]:
    """交付件章节当前版本来源标注里没解析到的知识编号（found=false）。只读结构化标注，不读正文。"""
    from . import reg_lifecycle

    hits: dict[str, set[int]] = {}
    for did, _status, src in reg_lifecycle._deliverable_refs(db):
        for x in (src or {}).get("knowledge", []):
            if isinstance(x, dict) and not x.get("found") and refs.is_ref(str(x.get("ref", ""))):
                hits.setdefault(str(x["ref"]), set()).add(did)
    sigs: list[acq.Signal] = []
    for ref, dids in sorted(hits.items()):
        for did in sorted(dids):
            ev = "cite-" + hashlib.sha256(f"deliverable:{did}".encode()).hexdigest()[:12]
            sigs.append(acq.Signal(ref, "usage", ev, impact=len(dids)))
    return sigs, {r: need_of(r) for r in hits}


def _library_has(docs: list[tuple[str, str, str, str]], name: str) -> bool:
    from . import citations

    return citations._match(docs, name) is not None


def _basis_signals(db: Session, today: date) -> tuple[list[acq.Signal], dict[str, str], set[str]]:
    """外部变化（被替代而库里没有替代文件）与生命周期（依据快废止、依据原文复审日期已到）。返回第三项：本次到期的条目编号。"""
    from . import basis_change as bc
    from . import citations

    docs = citations._sources(db)
    sigs: list[acq.Signal] = []
    need: dict[str, str] = {}
    due_ids: set[str] = set()
    soon = today + timedelta(days=LIFECYCLE_DAYS)
    for item, v in bc._basis_versions(db):
        repl = (v.ref_replaced_by or "").strip()[:200]
        if repl and not _library_has(docs, repl):
            sigs.append(acq.Signal(repl, "external", f"repl-{v.id}", impact=1, due=v.repeal_date, authority=1))
            need[repl] = "标准" if _STD_CODE.match(repl) else "法规"
        repealing = v.repeal_date is not None and v.repeal_date <= soon and v.ref_status in ("current", "upcoming")
        review_due = item.doc_type == "SRC" and v.review_date is not None and v.review_date <= today
        if repealing or review_due:
            dues = [d for d in (v.repeal_date if repealing else None, v.review_date if review_due else None) if d]
            sigs.append(acq.Signal(item.doc_id, "lifecycle", f"due-{v.id}", impact=1, due=min(dues), authority=1))
            need[item.doc_id] = need_of(item.doc_id, item.layer, item.doc_type)
            due_ids.add(item.doc_id)
    return sigs, need, due_ids


def _ready_keys(db: Session, keys: list[str]) -> set[str]:
    """哪些键已经有可用的现行条目：按条目 key（import_key）或知识编号（doc_id / 别名）。"""
    from . import readiness

    if not keys:
        return set()
    states = readiness._slot_states(db, set(keys))
    ok = {k for k, s in states.items() if s["state"] == "ready"}
    ids = [k for k in keys if refs.is_ref(k)]
    if ids:
        ok |= {k for k, r in refs.resolve(db, ids).items() if r.usable}
    return ok


def feed(db: Session, today: date, *, actor: str = "platform") -> dict[str, int]:
    """每周任务第一步：①②与外部变化、生命周期信号进缺口池；已补齐的自动关闭。返回各路数量。"""
    r_sigs, r_need, _ = _readiness_signals(db, today)
    c_sigs, c_need = _citation_signals(db)
    b_sigs, b_need, due_ids = _basis_signals(db, today)
    ready = _ready_keys(db, sorted({s.key for s in r_sigs + c_sigs}))
    r_sigs = [s for s in r_sigs if s.key not in ready]
    c_sigs = [s for s in c_sigs if s.key not in ready]
    sigs = r_sigs + c_sigs + b_sigs
    if sigs:
        gap_pool.record(db, sigs)
        _set_need(db, {**r_need, **c_need, **b_need})
    closed = _close_filled(db, due_ids, actor=actor)
    out = {"readiness": len({s.key for s in r_sigs}), "citation": len({s.key for s in c_sigs}),
           "external": len({s.key for s in b_sigs if s.kind == "external"}),
           "lifecycle": len({s.key for s in b_sigs if s.kind == "lifecycle"}), "closed": closed}
    if any(out.values()):
        audit.record(db, actor=actor, action="kb.gap.feed", result="allowed", **out)
    return out


def _close_filled(db: Session, due_ids: set[str], *, actor: str) -> int:
    """缺口补上了就关（published）：业务需求 / 使用信号缺口的键已有现行条目；外部变化缺口的替代文件已入库；
    生命周期缺口的条目不再到期。有定时巡检信号（来源网址）的缺口归采集关闭，不在这里关。"""
    from . import citations

    rows = list(db.scalars(select(KbGap).where(KbGap.status == "open")))
    need_kinds = {"business", "usage"}
    cand = [r for r in rows if set(r.kinds or {}) and set(r.kinds or {}) <= need_kinds]
    ready = _ready_keys(db, [r.gap_key for r in cand])
    docs = None
    done: list[KbGap] = [r for r in cand if r.gap_key in ready]
    for r in rows:
        kinds = set(r.kinds or {})
        if kinds == {"external"} and not acq._url_parts(r.gap_key):
            docs = docs if docs is not None else citations._sources(db)
            if _library_has(docs, r.gap_key):
                done.append(r)
        elif kinds == {"lifecycle"} and r.gap_key not in due_ids:
            done.append(r)
    for r in done:
        gap_pool.close(db, r.gap_key, "published", actor=actor)
    return len(done)


# ---- ⑤ 暂无可用来源：每月再试 ----

def retry_unavailable(db: Session, today: date, *, actor: str = "platform") -> int:
    """暂无可用来源满 RETRY_DAYS 天的缺口放回计划；再失败一次即重新标暂无可用来源（attempts 留在 MAX-1）。"""
    cutoff = today - timedelta(days=RETRY_DAYS)
    rows = [r for r in db.scalars(select(KbGap).where(KbGap.status == "open", KbGap.plan == gap_pool.UNAVAILABLE))
            if r.unavailable_at is not None and gap_pool._aware(r.unavailable_at).date() <= cutoff]  # type: ignore[union-attr]
    now = utcnow()
    for r in rows:
        r.plan, r.unavailable_at, r.manual_reason = "", None, ""
        r.attempts = gap_pool.MAX_ATTEMPTS - 1
        r.updated_at = now
    db.commit()
    if rows:
        audit.record(db, actor=actor, action="kb.gap.retry", result="allowed", count=len(rows))
    return len(rows)


def unavailable_keys(db: Session, keys: set[str]) -> set[str]:
    """知识健康格注明用：这些键里哪些缺口暂无可用来源。"""
    if not keys:
        return set()
    return set(db.scalars(select(KbGap.gap_key).where(KbGap.gap_key.in_(list(keys)), KbGap.status == "open",
                                                      KbGap.plan == gap_pool.UNAVAILABLE)))


# ---- ④ 栏目页两级取正文（平台侧） ----

class _Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.skip = 0
        self.href: str | None = None
        self.links: list[tuple[str, str]] = []
        self.text = 0
        self.link_text = 0
        self._buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style", "noscript", "template"):
            self.skip += 1
        elif tag == "a":
            self.href, self._buf = dict(attrs).get("href") or "", []

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "noscript", "template"):
            self.skip = max(0, self.skip - 1)
        elif tag == "a" and self.href is not None:
            self.links.append((self.href, "".join(self._buf).strip()))
            self.href = None

    def handle_data(self, data: str) -> None:
        if self.skip:
            return
        n = len(data.strip())
        self.text += n
        if self.href is not None:
            self.link_text += n
            self._buf.append(data)


def _html_text(data: bytes) -> str | None:
    if not re.search(rb"<[a-zA-Z!]", data[:65536]):
        return None
    if m := parse._HTML_CHARSET.search(data[:4096]):
        try:
            return data.decode(m.group(1).decode("ascii"))
        except (LookupError, UnicodeDecodeError):
            pass
    try:
        return parse._decode_text(data)
    except parse.ParseError:
        return None


def page_links(data: bytes, page_url: str, whitelist: tuple[str, ...]) -> tuple[list[str], bool]:
    """网页里白名单内的正文链接（去锚点、去重、不含本页和栏目目录），以及这一页是不是栏目页。"""
    text = _html_text(data)
    if text is None:
        return [], False
    p = _Links()
    try:
        p.feed(text)
        p.close()
    except (AssertionError, ValueError):
        return [], False
    base = urldefrag(page_url)[0]
    out: list[str] = []
    for href, _label in p.links:
        h = (href or "").strip()
        if not h or h.startswith(("javascript:", "mailto:", "#")):
            continue
        url = urldefrag(urljoin(base, h))[0]
        if url == base or url in out or not acq.in_whitelist(url, whitelist):
            continue
        if urlsplit(url).path.endswith("/"):            # 目录（另一个栏目），不当正文
            continue
        out.append(url)
    listing = len(out) >= LISTING_MIN_LINKS and p.text > 0 and p.link_text / p.text >= LISTING_LINK_SHARE
    return out, listing


_TITLE = re.compile(r"<(h1|title)\b[^>]*>(.*?)</\1\s*>", re.I | re.S)


def page_title(data: bytes) -> str:
    """网页正文的文件名称：先取第一个 h1，没有再取 title；去标签、压空白，最多 200 字；不是网页返回空串。"""
    text = _html_text(data)
    if text is None:
        return ""
    found = {m.group(1).lower(): m.group(2) for m in reversed(list(_TITLE.finditer(text)))}
    raw = found.get("h1") or found.get("title") or ""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", raw)).strip()[:200]


def classify(batch: Path, rows: list[dict[str, Any]], whitelist: tuple[str, ...]) -> tuple[set[str], int]:
    """回传批次（已核对通过）里哪些文件是栏目页，以及非栏目页里解析得出正文的文件数。"""
    listing: set[str] = set()
    body = 0
    for r in rows:
        name, url = str(r.get("file", "")), str(r.get("url", ""))
        data = (batch / name).read_bytes()
        if name.lower().endswith((".html", ".htm", ".shtml")) or data.lstrip()[:1] == b"<":
            if page_links(data, url, whitelist)[1]:
                listing.add(name)
                continue
        try:
            fname = name if "." in name else name + ".html"
            if fname.lower().endswith(".shtml"):
                fname = fname[:-6] + ".html"
            if parse.parse(fname, data).chars >= BODY_MIN_CHARS:
                body += 1
        except parse.ParseError:
            pass
    return listing, body


def batch_ref(batch: Path) -> str:
    return hashlib.sha256((batch / acq.RETURN_MANIFEST).read_bytes()).hexdigest()[:12]


def expand_listings(db: Session, batch: Path, rows: list[dict[str, Any]], listing: set[str],
                    whitelist: tuple[str, ...], *, actor: str = "platform") -> int:
    """栏目页里白名单内、还没登记过的正文链接 → 外部变化缺口，直接排进下一轮下发包。返回登记数。"""
    if not listing:
        return 0
    known = set(db.scalars(select(KbGap.gap_key)))
    urls: list[str] = []
    for r in rows:
        if str(r.get("file", "")) not in listing:
            continue
        links, _ = page_links((batch / str(r["file"])).read_bytes(), str(r.get("url", "")), whitelist)
        for u in links[:MAX_LINKS_PER_LISTING]:
            if u not in known and u not in urls and len(urls) < MAX_NEXT_ROUND:
                urls.append(u)
    if not urls:
        return 0
    ref = f"listing-{batch_ref(batch)}"
    gap_pool.record(db, [acq.Signal(u, "external", ref, impact=1) for u in urls])
    now = utcnow()
    for g in db.scalars(select(KbGap).where(KbGap.gap_key.in_(urls), KbGap.status == "open")):
        g.plan, g.source_via, g.planned_at, g.updated_at = "direct", "listing", now, now
    db.commit()
    audit.record(db, actor=actor, action="kb.gap.listing", result="allowed", listing=len(listing), next_round=len(urls))
    return len(urls)


# ---- ③ 来源推荐 ----

def recommend_stats(db: Session) -> dict[str, Any]:
    """近 RECOMMEND_WINDOW_DAYS 天的推荐命中率：命中 = 推荐的网址采到了；待定 = 推荐的网址还在等采集。"""
    since = utcnow() - timedelta(days=RECOMMEND_WINDOW_DAYS)
    rows = [r for r in db.scalars(select(KbGap).where(KbGap.rec_total > 0))
            if r.rec_at is not None and gap_pool._aware(r.rec_at) >= since]  # type: ignore[operator]
    total = sum(r.rec_total for r in rows)
    hits = sum(1 for r in rows if r.status == "closed" and r.source_via == "agent"
               and r.close_reason in ("collected", "published"))
    pending = sum(1 for r in rows if r.status == "open" and r.source_via == "agent" and r.source_url)
    decided = max(total - pending, 0)
    s = get_settings()
    rate = round(hits / decided, 4) if decided else None
    paused = decided >= s.kb_recommend_min_samples and rate is not None and rate < s.kb_recommend_min_hit
    return {"total": total, "hits": hits, "pending": pending, "decided": decided, "hit_rate": rate,
            "threshold": s.kb_recommend_min_hit, "paused": paused}


def _acceptable(url: str, prefixes: set[str], whitelist: tuple[str, ...]) -> bool:
    """推荐网址的程序硬校验：官方域名、在正式来源前缀内、不是来源登记的栏目页本身或目录。"""
    norm = url.strip()
    return (sources.is_official(norm) and acq.in_whitelist(norm, whitelist)
            and norm.rstrip("/") not in prefixes and not urlsplit(norm).path.endswith("/"))


def recommend_week(db: Session, *, actor: str = "platform") -> dict[str, Any]:
    """来源确认队列里还没有正文网址、本周没推荐过的缺口交来源推荐；没接模型或命中率低于门槛时不调。"""
    from . import source_list

    st = recommend_stats(db)
    if st["paused"]:
        audit.record(db, actor=actor, action="kb.gap.recommend", result="paused", hit_rate=st["hit_rate"],
                     decided=st["decided"])
        return {**st, "asked": 0, "accepted": 0}
    cutoff = utcnow() - timedelta(days=RECOMMEND_EVERY_DAYS)
    gaps = [g for g in db.scalars(select(KbGap).where(KbGap.status == "open", KbGap.plan == "confirm",
                                                       KbGap.source_url == "").order_by(KbGap.score.desc(), KbGap.id))
            if g.rec_at is None or gap_pool._aware(g.rec_at) < cutoff][:source_list.MAX_GAPS]  # type: ignore[operator]
    if not gaps or not get_settings().llm_base_url:
        return {**st, "asked": 0, "accepted": 0}
    try:
        rep = source_list.suggest(db, actor=actor, gap_ids=[g.id for g in gaps])
    except source_list.SourceListError:
        return {**st, "asked": 0, "accepted": 0}
    if rep.demo:
        return {**st, "asked": 0, "accepted": 0}
    wl = gap_pool.whitelist(db)
    prefixes = {s.url.rstrip("/") for s in db.scalars(select(KbSource))}
    by_gap: dict[int, str] = {}
    dropped: Counter[str] = Counter(rep.problems)
    for row in rep.rows:
        if row.gap_id in by_gap:
            continue
        if _acceptable(row.url, prefixes, wl):
            by_gap[row.gap_id] = row.url.strip()
        else:
            dropped["not_body_or_official"] += 1
    now = utcnow()
    for g in gaps:
        g.rec_total = (g.rec_total or 0) + 1
        g.rec_at = now
        if g.id in by_gap:
            g.source_url, g.source_via = by_gap[g.id], "agent"
        g.updated_at = now
    db.commit()
    audit.record(db, actor=actor, action="kb.gap.recommend", result="allowed", asked=len(gaps),
                 accepted=len(by_gap), dropped=dict(dropped))
    return {**st, "asked": len(gaps), "accepted": len(by_gap)}


# ---- ⑥ 入库报警 ----

def check_intake(db: Session, batch: Path, report: acq.ReturnReport, stored: int | None, *,
                 actor: str = "platform") -> KbAlarm | None:
    """非栏目页文件的正文率低于门槛、或这批入库数为 0 → 推入库报警卡；正常入库则关闭未处理的报警。
    stored=None 表示这次没走入库（只核对回传），只看正文率。全是栏目页（已转下一轮下发包）的批次不报警。"""
    files = report.count - len(report.listing)
    if files <= 0:
        return None
    rate = round(report.body / files, 4)
    kind = ""
    if stored == 0:
        kind = "zero_intake"
    elif rate < get_settings().kb_body_rate_min:
        kind = "low_body"
    counts = {"files": report.count, "listing": len(report.listing), "body": report.body,
              "stored": -1 if stored is None else stored, "body_rate": rate}
    now = utcnow()
    if not kind:
        opened = list(db.scalars(select(KbAlarm).where(KbAlarm.status == "open")))
        if stored is not None:
            for a in opened:
                a.status, a.resolved_by, a.resolved_at, a.updated_at = "resolved", "platform", now, now
            db.commit()
        return None
    ref = batch_ref(batch)
    a = db.scalar(select(KbAlarm).where(KbAlarm.status == "open", KbAlarm.kind == kind))
    if a is None:
        a = KbAlarm(kind=kind, status="open", count=1, batches=[ref], counts=counts, created_at=now, updated_at=now)
        db.add(a)
    elif ref not in (a.batches or []):
        a.count = (a.count or 0) + 1
        a.batches = [*(a.batches or []), ref][-20:]
        a.counts, a.updated_at = counts, now
    db.commit()
    audit.record(db, actor=actor, action="kb.gap.alarm", result="allowed", kind=kind, alarm_id=a.id,
                 **{k: v for k, v in counts.items()})
    return a


def alarms(db: Session, *, status: str = "open") -> list[dict[str, Any]]:
    rows = db.scalars(select(KbAlarm).where(KbAlarm.status == status).order_by(KbAlarm.id.desc()))
    return [{"id": a.id, "kind": a.kind, "label": ALARM_KINDS.get(a.kind, a.kind), "count": a.count,
             "counts": dict(a.counts or {}), "batches": list(a.batches or []), "created_at": a.created_at,
             "updated_at": a.updated_at} for a in rows]


def resolve_alarm(db: Session, alarm_id: int, actor: str) -> KbAlarm:
    a = db.get(KbAlarm, alarm_id)
    if a is None or a.status != "open":
        raise LookupError("没有这张未处理的报警卡")
    now = utcnow()
    a.status, a.resolved_by, a.resolved_at, a.updated_at = "resolved", actor, now, now
    db.commit()
    audit.record(db, actor=actor, action="kb.gap.alarm_resolve", result="allowed", alarm_id=a.id, kind=a.kind)
    return a

