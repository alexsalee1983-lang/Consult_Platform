#!/usr/bin/env python3
"""采集机侧闸门（三期 G4 / B25②④）。部署在外网采集机上，只用标准库，不含平台任何密钥。

  gate <下发包.json> --whitelist <本地白名单> --audit <审计.jsonl> --out <交给下载器的任务.json>
      下发包只能有 format/tasks、每项只能有 url/rule、规则字段受限且是短值；
      任一网址不在本机白名单内即整包拒收（不写 --out，退出码 2）。
  check-return <回传批次目录> --whitelist <本地白名单> --audit <审计.jsonl>
      推送资料仓前核对 manifest.json：网址都在白名单内、哈希一致、没有清单外文件；不过退出码 2。

本机白名单由管理员经 SSH 维护（每行一个网址前缀，# 开头为注释），不随下发包更新，
所以下发包被改也扩不了采集范围。审计每次追加一行 JSON：时间、动作、结果、包哈希、计数、
被拒主机名、问题类别计数；不记网址路径、查询串、规则内容和文件内容。
口径与平台 app/kb/acquisition.py 的 in_whitelist / check_dispatch 一致（tests/test_collector_gate.py 校验）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Tuple
from urllib.parse import urlsplit

DISPATCH_FORMAT = "kb-acquire/1"
RETURN_MANIFEST = "manifest.json"
RULE_KEYS = {"interval_days", "file_types", "selector", "max_files", "note"}
RULE_STR_MAX = 200
URL_MAX = 500


def _parts(url: str) -> Optional[Tuple[str, str, Optional[int], str]]:
    try:
        s = urlsplit(url.strip())
        port = s.port
    except ValueError:
        return None
    if s.scheme not in ("https", "http") or not s.hostname or s.username is not None or s.password is not None:
        return None
    return s.scheme, s.hostname.lower(), port, s.path or "/"


def url_allowed(url: Any, whitelist: tuple[str, ...] | list[str]) -> bool:
    """协议、主机名、端口完全相同，路径在白名单前缀之下（按“/”边界）。"""
    if not isinstance(url, str) or not url.strip() or len(url) > URL_MAX:
        return False
    u = _parts(url)
    if u is None:
        return False
    for entry in whitelist:
        w = _parts(entry) if entry else None
        if w is None or u[:3] != w[:3]:
            continue
        prefix = w[3]
        if u[3] == prefix or u[3].startswith(prefix if prefix.endswith("/") else prefix + "/"):
            return True
    return False


def host_of(url: Any) -> str:
    try:
        h = urlsplit(str(url)).hostname or ""
    except ValueError:
        h = ""
    return h.lower()[:100] or "?"


def load_whitelist(path: Path) -> tuple[str, ...]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ()
    items = (x.strip() for x in lines)
    return tuple(x for x in items if x and not x.startswith("#") and _parts(x))


def _short(v: Any) -> bool:
    if isinstance(v, str):
        return len(v) <= RULE_STR_MAX
    if isinstance(v, (bool, int)):
        return True
    if isinstance(v, list):
        return len(v) <= 20 and all(isinstance(x, str) and len(x) <= 20 for x in v)
    return False


def check_package(pkg: Any, whitelist: tuple[str, ...]) -> tuple[Counter[str], list[str], int]:
    """返回（问题类别计数、被拒主机名、任务数）。"""
    problems: Counter[str] = Counter()
    hosts: set[str] = set()
    if not whitelist:
        problems["whitelist_empty"] += 1
        return problems, [], 0
    if not isinstance(pkg, dict) or set(pkg) != {"format", "tasks"} or pkg.get("format") != DISPATCH_FORMAT:
        problems["top"] += 1
    tasks = pkg.get("tasks") if isinstance(pkg, dict) else None
    if not isinstance(tasks, list):
        problems["tasks"] += 1
        return problems, [], 0
    for t in tasks:
        if not isinstance(t, dict) or set(t) != {"url", "rule"}:
            problems["task_keys"] += 1
            continue
        if not url_allowed(t["url"], whitelist):
            problems["offlist"] += 1
            hosts.add(host_of(t["url"]))
        rule = t["rule"]
        if not isinstance(rule, dict) or not set(rule) <= RULE_KEYS:
            problems["rule_keys"] += 1
        elif not all(_short(v) for v in rule.values()):
            problems["rule_long"] += 1
    return problems, sorted(hosts), len(tasks)


def check_batch(batch: Path, whitelist: tuple[str, ...]) -> tuple[Counter[str], list[str], int]:
    problems: Counter[str] = Counter()
    hosts: set[str] = set()
    if not whitelist:
        problems["whitelist_empty"] += 1
        return problems, [], 0
    try:
        rows = json.loads((batch / RETURN_MANIFEST).read_text(encoding="utf-8"))["files"]
        if not isinstance(rows, list):
            raise TypeError
    except (OSError, ValueError, KeyError, TypeError):
        problems["manifest"] += 1
        return problems, [], 0
    root = batch.resolve()
    listed: set[str] = set()
    ok = 0
    for row in rows:
        name = str(row.get("file", "")) if isinstance(row, dict) else ""
        p = (batch / name).resolve()
        if not name or p.parent != root:
            problems["path"] += 1
            continue
        listed.add(name)
        if not url_allowed(str(row.get("url", "")), whitelist):
            problems["offlist"] += 1
            hosts.add(host_of(row.get("url", "")))
        if not p.is_file():
            problems["missing"] += 1
        elif hashlib.sha256(p.read_bytes()).hexdigest() != str(row.get("sha256", "")).lower():
            problems["sha256"] += 1
        else:
            ok += 1
    extra = sum(1 for f in batch.iterdir() if f.is_file() and f.name != RETURN_MANIFEST and f.name not in listed)
    if extra:
        problems["extra"] += extra
    return problems, sorted(hosts), ok


def _audit(path: Path, rec: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), **rec}
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="采集机侧闸门：白名单外拒收并记审计")
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("gate")
    g.add_argument("package")
    g.add_argument("--out", required=True)
    c = sub.add_parser("check-return")
    c.add_argument("batch")
    for p in (g, c):
        p.add_argument("--whitelist", required=True)
        p.add_argument("--audit", required=True)
    a = ap.parse_args(argv)
    wl = load_whitelist(Path(a.whitelist))
    audit = Path(a.audit)
    if a.cmd == "gate":
        raw = b""
        pkg: Any = None
        try:
            raw = Path(a.package).read_bytes()
            pkg = json.loads(raw.decode("utf-8"))
            problems, hosts, total = check_package(pkg, wl)
        except (OSError, ValueError):
            problems, hosts, total = Counter({"unreadable": 1}), [], 0
        rec = {"action": "gate", "pkg_sha256": hashlib.sha256(raw).hexdigest(), "tasks": total,
               "refused": problems.get("offlist", 0), "refused_hosts": hosts, "problems": dict(problems)}
        if problems:
            _audit(audit, {**rec, "result": "refused", "accepted": 0})
            print(f"拒收：{dict(problems)}", file=sys.stderr)
            return 2
        Path(a.out).write_text(json.dumps(pkg, ensure_ascii=False), encoding="utf-8")
        _audit(audit, {**rec, "result": "accepted", "accepted": total})
        print(f"放行 {total} 项")
        return 0
    problems, hosts, ok = check_batch(Path(a.batch), wl)
    _audit(audit, {"action": "check-return", "files_ok": ok, "refused_hosts": hosts, "problems": dict(problems),
                   "result": "refused" if problems else "accepted"})
    if problems:
        print(f"不推送：{dict(problems)}", file=sys.stderr)
        return 2
    print(f"可推送 {ok} 个文件")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
