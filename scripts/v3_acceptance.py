#!/usr/bin/env python3
"""v3.0 验收脚本（三期闭环③，#404）：一条命令跑 v3.0 验收第 2、5、6、7、10、11 条，结果写成 markdown。

每条分两段：
  1. 自动化用例：跑该条已有的 pytest 用例（隔离库，默认经 scripts/pytest_iso.sh）。
  2. 实机核对（给了 --base 才做）：对整套启动的环境用测试账号（虚拟客户 BANK-A）调接口 / CLI。
     不调模型；需要模型的环节（交付链实跑、写稿助手问答）只由自动化用例覆盖，报告里如实写明。

只用标准库。报告只写条目、结论、计数，不写快照内容（快照含项目 id，只留本机）。

用法（测试机，仓库根目录）：
  python3 scripts/v3_acceptance.py --out /tmp/v3-acceptance.md \
      --pytest "bash scripts/pytest_iso.sh --repo $PWD --tag v3acc --" \
      --base http://localhost:8180 --compose "docker compose -p cpa-v3acc"
本机只跑用例：python scripts/v3_acceptance.py --pytest "python -m pytest" --out v3.md
退出码：全部条目已达标为 0，否则 1。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import http.cookiejar
import json
import os
import re
import shlex
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 条目 -> 已有自动化证据（用例 nodeid 相对 backend/）。改名、删用例由 tests/test_v3_acceptance.py 守住。
ITEMS: dict[int, dict] = {
    2: {
        "title": "SOC 试点项目走完交付链（虚拟客户 BANK-A，无真实项目）",
        "tests": [
            "tests/test_runs.py::test_chain_runs_end_to_end",
            "tests/test_runs.py::test_signed_deliverable_refuses_start",
            "tests/test_soc_config.py::test_soc_chain_unchanged",
            "tests/test_soc_config.py::test_soc_deliverables_match_seed",
            "tests/test_five_products_e2e.py::test_product_d1_to_d8",
            "tests/test_quality.py::test_review_submit_sign_then_readonly_and_export_without_draft",
        ],
    },
    5: {
        "title": "异构评审生效，平台强制校验",
        "tests": [
            "tests/test_quality.py::test_review_model_family_differs_from_generator",
            "tests/test_quality.py::test_single_family_review_is_marked_degraded",
            "tests/test_model_catalog.py::test_review_must_differ_from_generator_family",
            "tests/test_model_catalog.py::test_arbitrate_differs_from_both_sides",
            "tests/test_model_catalog.py::test_invalid_catalog_refused_at_startup",
        ],
    },
    6: {
        "title": "指标精简版出数",
        "tests": [
            "tests/test_indicators.py::test_deliverable_metrics",
            "tests/test_indicators.py::test_kb_and_eval_metrics",
            "tests/test_indicators.py::test_api_only_for_project_lead",
        ],
    },
    7: {
        "title": "知识数据结构迁移完成，旧编号仍可引用，已签署交付件不受影响",
        "tests": [
            "tests/test_kb_aliases.py::test_resolve_via_alias_and_canonical",
            "tests/test_kb_aliases.py::test_retired_item_resolves_via_alias_as_retired",
            "tests/test_kb_aliases.py::test_api_item_by_alias_and_search",
            "tests/test_kb_docid.py::test_transfer_import_keeps_refs_via_aliases",
            "tests/test_kb_refcheck.py::test_snapshot_covers_signed_stored_scanned_and_export",
            "tests/test_kb_refcheck.py::test_rename_with_alias_is_not_regression",
            "tests/test_kb_refcheck.py::test_signed_set_change_is_regression",
        ],
    },
    10: {
        "title": "阻断只有封闭五类，增量评审生效",
        "tests": [
            "tests/test_review_issues.py::test_closed_five_categories_downgrade",
            "tests/test_review_issues.py::test_out_of_category_blocker_does_not_close_gate",
            "tests/test_review_incremental.py::test_second_round_sends_only_changed_sections_and_carries_untouched",
            "tests/test_review_incremental.py::test_full_review_when_all_sections_changed",
            "tests/test_review_profiles.py::test_light_profile_review_end_to_end",
        ],
    },
    11: {
        "title": "写稿助手 v1 可用（侧栏问答、插入保留来源，插入内容照样过签署门禁）",
        "tests": [
            "tests/test_assist.py::test_ask_streams_with_auto_context_and_checked_sources",
            "tests/test_assist.py::test_guard_blocks_and_audit_has_no_original_text",
            "tests/test_assist.py::test_inserted_reply_still_blocked_at_signoff",
        ],
    },
}

PROJECT = "BANK-A"


def verdict(pytest_ok: bool, live_ok: bool | None) -> str:
    if not pytest_ok or live_ok is False:
        return "未达标"
    return "已达标" if live_ok else "已达标（仅自动化用例）"


def render(rows: list[dict], commit: str, env: str) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    out = ["# v3.0 验收脚本结果（第 2、5、6、7、10、11 条）", "",
           f"时间 {now}；提交 {commit}；环境 {env}；虚拟客户 {PROJECT}。", "",
           "| 条目 | 内容 | 自动化用例 | 实机核对 | 结论 | 说明 |", "|---|---|---|---|---|---|"]
    mark = {True: "通过", False: "失败", None: "未做"}
    for r in rows:
        out.append(f"| 第 {r['no']} 条 | {r['title']} | {mark[r['pytest']]} | {mark[r['live']]} | "
                   f"{verdict(r['pytest'], r['live'])} | {r['note']} |")
    return "\n".join(out) + "\n"


# ---------- 自动化用例 ----------

def run_pytest(cmd: str, nodeids: list[str], log: Path) -> bool:
    args = shlex.split(cmd) + ["-q", "-p", "no:cacheprovider", *nodeids]
    cwd = ROOT if "pytest_iso" in cmd else ROOT / "backend"
    with log.open("a", encoding="utf-8") as fh:
        fh.write(f"\n$ {' '.join(args)}\n")
        fh.flush()
        rc = subprocess.call(args, cwd=cwd, stdout=fh, stderr=subprocess.STDOUT)
    return rc == 0


# ---------- 实机核对 ----------

def env_val(name: str) -> str:
    if os.environ.get(name):
        return os.environ[name]
    env = ROOT / ".env"
    if env.is_file():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def totp(secret: str, step: int | None = None) -> str:
    key = base64.b32decode(secret.upper() + "=" * (-len(secret) % 8))
    counter = step if step is not None else int(time.time()) // 30
    h = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    o = h[-1] & 0x0F
    return f"{(struct.unpack('>I', h[o:o + 4])[0] & 0x7FFFFFFF) % 1_000_000:06d}"


class Api:
    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.csrf = ""

    def call(self, method: str, path: str, body: dict | None = None) -> tuple[int, object]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if self.csrf:
            req.add_header("X-CSRF-Token", self.csrf)
        try:
            with self.opener.open(req, timeout=30) as resp:
                raw = resp.read()
                code = resp.status
        except urllib.error.HTTPError as e:
            raw, code = e.read(), e.code
        try:
            return code, json.loads(raw or b"null")
        except ValueError:
            return code, None

    def login(self) -> bool:
        user, pw, secret = (env_val(k) for k in ("TEST_ACCOUNT_USERNAME", "TEST_ACCOUNT_PASSWORD",
                                                 "TEST_ACCOUNT_TOTP_SECRET"))
        if not (user and pw and secret):
            return False
        for _ in range(2):
            code, body = self.call("POST", "/api/auth/login", {"username": user, "password": pw, "totp": totp(secret)})
            if code == 200 and isinstance(body, dict):
                self.csrf = body.get("csrf_token", "")
                return True
            time.sleep(31 - int(time.time()) % 30)  # 同一个 TOTP 码不能重用，等下一个
        return False


def live_chain(api: Api) -> tuple[bool | None, str]:
    code, steps = api.call("GET", f"/api/projects/{PROJECT}/chain")
    if code != 200 or not isinstance(steps, list) or not steps:
        return False, f"交付链接口 {code}"
    return True, f"交付链 {len(steps)} 步"


def live_indicators(api: Api) -> tuple[bool | None, str]:
    code, body = api.call("GET", "/api/indicators?days=30")
    if code == 403:
        return None, "测试账号不是项目负责人（接口 403 符合权限设计），实机出数需项目负责人账号"
    if code != 200 or not isinstance(body, dict):
        return False, f"指标接口 {code}"
    items = body.get("items") or body.get("metrics") or []
    n = len(items) if isinstance(items, list) else len(body)
    return True, f"指标接口 200，{n} 项"


def live_refcheck(compose: str, log: Path) -> tuple[bool | None, str]:
    args = shlex.split(compose) + ["exec", "-T", "backend", "python", "-m", "app.cli", "kb-ref-check", "--signed-only"]
    p = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
    with log.open("a", encoding="utf-8") as fh:
        fh.write(f"\n$ {' '.join(args)}\n{p.stdout}{p.stderr}")
    if p.returncode != 0 or "快照：" not in p.stdout:
        return False, f"kb-ref-check 退出码 {p.returncode}"
    c = ref_counts(p.stdout)
    bad = c.get("signed_unresolved", 0)  # 计数为 0 的键不打印
    return bad == 0, f"已签署引用解析到 {c.get('found', 0)} 处，解析不到 {bad} 处"


def ref_counts(stdout: str) -> dict[str, int]:
    line = next((x for x in stdout.splitlines() if x.startswith("快照：")), "")
    body = line.removeprefix("快照：").split("；")[0]
    return {k: int(v) for k, v in re.findall(r"(\w+) (\d+)", body)}


def main() -> int:
    ap = argparse.ArgumentParser(description="v3.0 验收第 2、5、6、7、10、11 条")
    ap.add_argument("--out", required=True, help="结果 markdown 路径")
    ap.add_argument("--log", default="", help="明细日志（默认 <out>.log）")
    ap.add_argument("--pytest", default="python -m pytest", help="pytest 命令前缀")
    ap.add_argument("--base", default="", help="整套环境地址；不给则不做实机核对")
    ap.add_argument("--compose", default="docker compose", help="docker compose 命令前缀（含 -p 项目名）")
    ap.add_argument("--env", default="-", help="环境标识（A/B）")
    a = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # Windows 控制台默认编码打不出中文

    out = Path(a.out)
    log = Path(a.log or str(out) + ".log")
    log.write_text("", encoding="utf-8")
    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True,
                            text=True).stdout.strip() or "-"

    api = None
    if a.base:
        api = Api(a.base)
        if not api.login():
            print("测试账号登录失败，实机核对不做", file=sys.stderr)
            api = None

    rows = []
    for no, item in ITEMS.items():
        ok = run_pytest(a.pytest, item["tests"], log)
        live: bool | None = None
        note = f"{len(item['tests'])} 个用例"
        if api is not None:
            if no == 2:
                live, n = live_chain(api)
                note += f"；{n}；实跑到签署需模型，由用例覆盖"
            elif no == 6:
                live, n = live_indicators(api)
                note += f"；{n}"
            elif no == 7:
                live, n = live_refcheck(a.compose, log)
                note += f"；{n}"
        if no == 11:
            note += "；插入保留来源为前端行为，用例以保存回复正文模拟"
        rows.append({"no": no, "title": item["title"], "pytest": ok, "live": live, "note": note})
        print(f"第 {no} 条：{verdict(ok, live)}")

    out.write_text(render(rows, commit, a.env), encoding="utf-8")
    print(f"结果：{out}；明细：{log}")
    return 0 if all(verdict(r["pytest"], r["live"]) != "未达标" for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
