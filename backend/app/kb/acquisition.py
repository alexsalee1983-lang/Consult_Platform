"""三期 B25①：知识持续获取的纯逻辑（缺口池、下发包、回传批次核对）。

本模块不建表、不调模型、不联网、不写审计，只做程序规则，供后续的后台任务、知识审批队列“来源确认”类和
采集机导入复用（三期开发计划第九节“获取触发模型”）：

- 缺口池：五类触发源的信号按 key（来源或依据编号、或网址）去重合并，同一事件只算一次；
  信号只带事件编号（event_ref），不带原文。
- 打分：影响面、紧急度（生效日 / 截止日 90 天、30 天两档）、权威等级（B27 一二三级）、信号次数，权重可配。
- 本周计划：每类触发源的阈值（信号次数）未达到的只累积；达到的按得分排序，
  已在白名单（B16 正式来源）的外部变化直接出采集任务，其余进“来源确认”；超出周上限的顺延，不丢。
- 下发包：只含网址与采集规则，程序检查不含条目正文、网址都在白名单内。
- 回传批次：按哈希清单核对文件（改动、缺失、多出、白名单外、路径越界），问题只给文件名。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

KINDS = ("schedule", "external", "usage", "business", "lifecycle")
KIND_LABELS = {"schedule": "定时巡检", "external": "外部变化", "usage": "使用信号",
               "business": "业务需求", "lifecycle": "生命周期"}
EVENT_REF = re.compile(r"[A-Za-z0-9._:#/-]{1,64}")  # 事件编号：只许编号字符，防止夹带原文
DISPATCH_FORMAT = "kb-acquire/1"
RETURN_MANIFEST = "manifest.json"
RULE_KEYS = {"interval_days", "file_types", "selector", "max_files", "note"}
RULE_STR_MAX = 200  # 规则里单个字符串上限，防止夹带正文
URL_MAX = 500


@dataclass
class Signal:
    key: str
    kind: str
    event_ref: str
    impact: int = 0
    due: date | None = None
    authority: int = 3


@dataclass
class Gap:
    key: str
    kinds: dict[str, int]
    events: list[str]
    impact: int = 0
    due: date | None = None
    authority: int = 3

    @property
    def signal_count(self) -> int:
        return len(self.events)


@dataclass
class Weights:
    impact: float = 1.0
    urgency: float = 1.0
    authority: float = 1.0
    signals: float = 1.0


@dataclass
class WeekPlan:
    direct: list[Gap] = field(default_factory=list)
    confirm: list[Gap] = field(default_factory=list)
    deferred: list[Gap] = field(default_factory=list)
    waiting: list[Gap] = field(default_factory=list)


def merge_signals(signals: list[Signal]) -> list[Gap]:
    """同一 key 合为一个缺口；同一 key 下同一事件编号只算一次。按首次出现顺序返回。"""
    gaps: dict[str, Gap] = {}
    for s in signals:
        if s.kind not in KINDS:
            raise ValueError(f"未知触发源：{s.kind}")
        if not EVENT_REF.fullmatch(s.event_ref):
            raise ValueError("event_ref 只能是事件编号")
        g = gaps.get(s.key)
        if g is None:
            g = gaps[s.key] = Gap(key=s.key, kinds={}, events=[], impact=s.impact, due=s.due,
                                  authority=s.authority)
        elif s.event_ref in g.events:
            continue
        else:
            g.impact = max(g.impact, s.impact)
            if s.due and (g.due is None or s.due < g.due):
                g.due = s.due
            g.authority = min(g.authority, s.authority)
        g.events.append(s.event_ref)
        g.kinds[s.kind] = g.kinds.get(s.kind, 0) + 1
    return list(gaps.values())


def urgency(due: date | None, today: date) -> float:
    """临近生效日 / 截止日：30 天内（含已过）1，90 天内 0.5，更远或没有日期 0。"""
    if due is None:
        return 0.0
    days = (due - today).days
    if days <= 30:
        return 1.0
    if days <= 90:
        return 0.5
    return 0.0


def score(g: Gap, today: date, w: Weights | None = None) -> float:
    w = w or Weights()
    impact = min(math.log1p(max(g.impact, 0)) / math.log1p(20), 1.0)
    authority = {1: 1.0, 2: 0.6}.get(g.authority, 0.3)
    signals = min(g.signal_count / 5, 1.0)
    return round(w.impact * impact + w.urgency * urgency(g.due, today) + w.authority * authority
                 + w.signals * signals, 6)


def _url_parts(url: str) -> tuple[str, str, int | None, str] | None:
    try:
        s = urlsplit(url.strip())
        port = s.port
    except ValueError:
        return None
    if s.scheme not in ("https", "http") or not s.hostname or s.username is not None or s.password is not None:
        return None
    return s.scheme, s.hostname.lower(), port, s.path or "/"


def in_whitelist(url: str, whitelist: tuple[str, ...] | list[str]) -> bool:
    """协议、主机名、端口完全相同，路径在白名单前缀之下（按“/”边界），防主机名前缀、用户信息冒充。
    与采集机侧 tools/collector/collector_gate.py 的 url_allowed 同一口径。"""
    if not isinstance(url, str) or not url.strip() or len(url) > URL_MAX:
        return False
    u = _url_parts(url)
    if u is None:
        return False
    for entry in whitelist:
        w = _url_parts(entry) if entry else None
        if w is None or u[:3] != w[:3]:
            continue
        prefix = w[3]
        if u[3] == prefix or u[3].startswith(prefix if prefix.endswith("/") else prefix + "/"):
            return True
    return False


def plan_week(gaps: list[Gap], today: date, *, thresholds: dict[str, int], whitelist: tuple[str, ...],
              cap: int, weights: Weights | None = None) -> WeekPlan:
    """阈值缺省 1。白名单内外部变化直接采集，其余进来源确认；两路合计不超过 cap，超出的顺延。"""
    plan = WeekPlan()
    ready: list[Gap] = []
    for g in gaps:
        if any(n >= thresholds.get(k, 1) for k, n in g.kinds.items()):
            ready.append(g)
        else:
            plan.waiting.append(g)
    ready.sort(key=lambda g: (-score(g, today, weights), g.key))
    for g in ready:
        if len(plan.direct) + len(plan.confirm) >= cap:
            plan.deferred.append(g)
        elif "external" in g.kinds and in_whitelist(g.key, whitelist):
            plan.direct.append(g)
        else:
            plan.confirm.append(g)
    return plan


def build_dispatch(urls: list[str], whitelist: tuple[str, ...], rule: dict[str, Any] | None = None) -> dict[str, Any]:
    """下发包：只有网址与采集规则。白名单外网址直接拒绝。"""
    bad = [u for u in urls if not in_whitelist(u, whitelist)]
    if bad:
        raise ValueError(f"{len(bad)} 个网址不在白名单内")
    r = {"interval_days": 7, "file_types": ["html", "pdf"], "max_files": 20, **(rule or {})}
    pkg = {"format": DISPATCH_FORMAT, "tasks": [{"url": u, "rule": dict(r)} for u in urls]}
    problems = check_dispatch(pkg, whitelist)
    if problems:
        raise ValueError("下发包不合规：" + "；".join(problems))
    return pkg


def _short(v: Any) -> bool:
    if isinstance(v, str):
        return len(v) <= RULE_STR_MAX
    if isinstance(v, bool) or isinstance(v, int):
        return True
    if isinstance(v, list):
        return len(v) <= 20 and all(isinstance(x, str) and len(x) <= 20 for x in v)
    return False


def check_dispatch(pkg: dict[str, Any], whitelist: tuple[str, ...]) -> list[str]:
    """程序检查下发包：顶层只有 format、tasks；每项只有 url、rule；rule 字段受限且都是短值；网址在白名单内。"""
    problems: list[str] = []
    if set(pkg) != {"format", "tasks"} or pkg.get("format") != DISPATCH_FORMAT:
        problems.append("顶层字段只能是 format、tasks")
    tasks = pkg.get("tasks")
    if not isinstance(tasks, list):
        return problems + ["tasks 不是列表"]
    for i, t in enumerate(tasks, 1):
        if not isinstance(t, dict) or set(t) != {"url", "rule"}:
            problems.append(f"第 {i} 项只能有 url、rule")
            continue
        url = t["url"]
        if not isinstance(url, str) or len(url) > URL_MAX or not in_whitelist(url, whitelist):
            problems.append(f"第 {i} 项网址不在白名单内")
        rule = t["rule"]
        if not isinstance(rule, dict) or not set(rule) <= RULE_KEYS:
            problems.append(f"第 {i} 项规则字段不合规")
        elif not all(_short(v) for v in rule.values()):
            problems.append(f"第 {i} 项规则里有过长内容")
    return problems


@dataclass
class ReturnReport:
    count: int = 0
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def verify_return(batch: Path, whitelist: tuple[str, ...]) -> ReturnReport:
    """回传批次核对：manifest.json 列 file / sha256 / url；问题形如 “类别:文件名”。"""
    r = ReturnReport()
    try:
        manifest = json.loads((batch / RETURN_MANIFEST).read_text(encoding="utf-8"))
        rows = manifest["files"]
    except (OSError, ValueError, KeyError, TypeError):
        r.problems.append(f"manifest:{RETURN_MANIFEST}")
        return r
    root = batch.resolve()
    listed: set[str] = set()
    for row in rows:
        name = str(row.get("file", "")) if isinstance(row, dict) else ""
        p = (batch / name).resolve()
        if not name or p.parent != root:
            r.problems.append(f"path:{name[:80]}")
            continue
        listed.add(name)
        if not in_whitelist(str(row.get("url", "")), whitelist):
            r.problems.append(f"offlist:{name}")
        if not p.is_file():
            r.problems.append(f"missing:{name}")
            continue
        if hashlib.sha256(p.read_bytes()).hexdigest() != str(row.get("sha256", "")).lower():
            r.problems.append(f"sha256:{name}")
            continue
        r.count += 1
    for f in sorted(batch.iterdir()):
        if f.is_file() and f.name != RETURN_MANIFEST and f.name not in listed:
            r.problems.append(f"extra:{f.name}")
    return r
