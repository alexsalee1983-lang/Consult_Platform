"""G4 采集机侧闸门（tools/collector/collector_gate.py）：白名单外整包拒收并记审计，审计不记原文。"""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from app.kb import acquisition as acq

_GATE = Path(__file__).resolve().parents[2] / "tools" / "collector" / "collector_gate.py"
_spec = importlib.util.spec_from_file_location("collector_gate", _GATE)
assert _spec and _spec.loader
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)

WL = ("https://www.gov.cn/zhengce/", "https://csrc.nist.gov/pubs/")


def _write(tmp: Path, pkg: dict) -> tuple[Path, Path, Path]:
    p = tmp / "dispatch.json"
    p.write_text(json.dumps(pkg, ensure_ascii=False), encoding="utf-8")
    wl = tmp / "whitelist.txt"
    wl.write_text("# 采集机本地白名单\n" + "\n".join(WL) + "\n", encoding="utf-8")
    return p, wl, tmp / "audit.jsonl"


def _audit(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


@pytest.mark.parametrize("url,ok", [
    ("https://www.gov.cn/zhengce/2026/a.html", True),
    ("https://WWW.GOV.CN/zhengce/x", True),
    ("https://www.gov.cn.evil.example/zhengce/x", False),   # 主机名前缀冒充
    ("https://www.gov.cn@evil.example/zhengce/x", False),   # 用户信息冒充
    ("https://www.gov.cn:8443/zhengce/x", False),           # 端口不同
    ("http://www.gov.cn/zhengce/x", False),                 # 协议不同
    ("https://www.gov.cn/zhengcex/x", False),               # 路径前缀不在边界上
    ("https://www.gov.cn/other/", False),
    ("ftp://www.gov.cn/zhengce/", False),
    ("", False),
])
def test_url_rule_strict_and_same_as_platform(url: str, ok: bool) -> None:
    assert gate.url_allowed(url, WL) is ok
    assert acq.in_whitelist(url, WL) is ok  # 平台侧与采集机侧同一口径


def test_gate_accepts_clean_package(tmp_path: Path) -> None:
    pkg = acq.build_dispatch(["https://www.gov.cn/zhengce/a.html"], WL)
    p, wl, audit = _write(tmp_path, pkg)
    out = tmp_path / "tasks.json"
    assert gate.main(["gate", str(p), "--whitelist", str(wl), "--audit", str(audit), "--out", str(out)]) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["tasks"][0]["url"].endswith("a.html")
    rec = _audit(audit)[-1]
    assert rec["action"] == "gate" and rec["result"] == "accepted" and rec["accepted"] == 1
    assert rec["pkg_sha256"] == hashlib.sha256(p.read_bytes()).hexdigest()


def test_gate_refuses_whole_package_on_offlist_and_audits(tmp_path: Path) -> None:
    pkg = {"format": acq.DISPATCH_FORMAT, "tasks": [
        {"url": "https://www.gov.cn/zhengce/a.html", "rule": {"max_files": 5}},
        {"url": "https://www.gov.cn.evil.example/zhengce/secret?k=口令原文", "rule": {"max_files": 5}},
    ]}
    p, wl, audit = _write(tmp_path, pkg)
    out = tmp_path / "tasks.json"
    assert gate.main(["gate", str(p), "--whitelist", str(wl), "--audit", str(audit), "--out", str(out)]) == 2
    assert not out.exists()  # 整包拒收，不交给下载器
    rec = _audit(audit)[-1]
    assert rec["result"] == "refused" and rec["refused"] == 1
    assert rec["refused_hosts"] == ["www.gov.cn.evil.example"]
    text = audit.read_text(encoding="utf-8")
    assert "secret" not in text and "口令原文" not in text  # 审计只记主机名、计数、类别


def test_gate_refuses_smuggled_fields(tmp_path: Path) -> None:
    pkg = {"format": acq.DISPATCH_FORMAT, "note": "正文", "tasks": [
        {"url": "https://www.gov.cn/zhengce/a.html", "rule": {"note": "x" * 500}}]}
    p, wl, audit = _write(tmp_path, pkg)
    assert gate.main(["gate", str(p), "--whitelist", str(wl), "--audit", str(audit),
                      "--out", str(tmp_path / "t.json")]) == 2
    rec = _audit(audit)[-1]
    assert rec["result"] == "refused" and set(rec["problems"]) >= {"top", "rule_long"}
    assert "正文" not in audit.read_text(encoding="utf-8")


def test_gate_refuses_when_local_whitelist_missing_or_empty(tmp_path: Path) -> None:
    pkg = acq.build_dispatch(["https://www.gov.cn/zhengce/a.html"], WL)
    p, wl, audit = _write(tmp_path, pkg)
    wl.write_text("# 空\n", encoding="utf-8")
    assert gate.main(["gate", str(p), "--whitelist", str(wl), "--audit", str(audit),
                      "--out", str(tmp_path / "t.json")]) == 2
    assert _audit(audit)[-1]["problems"] == {"whitelist_empty": 1}


def test_check_return_before_push(tmp_path: Path) -> None:
    batch = tmp_path / "b1"
    batch.mkdir()
    (batch / "a.html").write_bytes(b"<html>ok</html>")
    rows = [{"file": "a.html", "sha256": hashlib.sha256(b"<html>ok</html>").hexdigest(),
             "url": "https://www.gov.cn/zhengce/a.html"}]
    (batch / "manifest.json").write_text(json.dumps({"files": rows}), encoding="utf-8")
    _, wl, audit = _write(tmp_path, {"format": acq.DISPATCH_FORMAT, "tasks": []})
    assert gate.main(["check-return", str(batch), "--whitelist", str(wl), "--audit", str(audit)]) == 0
    assert acq.verify_return(batch, WL).ok  # 采集机放行的批次平台也收
    (batch / "x.pdf").write_bytes(b"%PDF")
    rows.append({"file": "x.pdf", "sha256": hashlib.sha256(b"%PDF").hexdigest(),
                 "url": "https://evil.example/x.pdf"})
    (batch / "manifest.json").write_text(json.dumps({"files": rows}), encoding="utf-8")
    assert gate.main(["check-return", str(batch), "--whitelist", str(wl), "--audit", str(audit)]) == 2
    rec = _audit(audit)[-1]
    assert rec["action"] == "check-return" and rec["result"] == "refused"
    assert rec["problems"] == {"offlist": 1} and rec["refused_hosts"] == ["evil.example"]
