"""知识编号统一解析（三期 B15 第一步，app/kb/refs.py）：旧格式全部变体、预留的新格式 KB-000123、
4 处调用点（交付件解析、导出脚注、严格档剔除、前端高亮）行为与收口前一致、resolve 批量核对、全仓不留第二个写死的正则。"""
import re
import secrets
from pathlib import Path

import pytest
from sqlalchemy import select

from app.core.db import SessionLocal
from app.kb import refs
from app.kb import review as rv
from app.kb.items import ItemMeta, VersionContent
from app.models import User
from app.project import cards, export

from .conftest import create_user

ROOT = Path(__file__).resolve().parents[2]

# 收口前 4 处写死的正则（原样抄录，作为“行为不变”的对照）
OLD_KB_REF = re.compile(r"[\[｜|]\s*(L[1-5](?:-[A-Z0-9]{2,8}){3,4}-\d{3,5})\s*[\]】]")
OLD_EXPORT_REF = re.compile(r"\[((?:I|D|O|S)-\d{2,4})(-\d{1,3})?\]"
                            r"|[\[｜|]\s*(L[1-5](?:-[A-Z0-9]{2,8}){3,4}-\d{3,5})\s*[\]】]")
OLD_CITATION_RE = re.compile(r"【依据[：:][^】]*】|【依据待核实】|\[(?:S-\d{2,4}|推断|模型推断)\]|"
                             r"(?<![A-Za-z0-9])[DIOS]-\d{2,4}(?:-\d{2,3})?(?![\d-])|"
                             r"[\[｜|]\s*L[1-5](?:-[A-Z0-9]{2,8}){3,4}-\d{3,5}\s*[\]】]")

LEGACY_HITS = [
    ("见 [L2-OPS-SOC-TPL-007]。", ["L2-OPS-SOC-TPL-007"]),
    ("【依据：GB/T 22239-2019 8.1.4｜L4-REG-GBT-22239-00001】", ["L4-REG-GBT-22239-00001"]),
    ("【依据：示范 | L4-CMP-GEN-LAW-0012】", ["L4-CMP-GEN-LAW-0012"]),
    ("[ L3-DAT-DSM-SRC-001 ] 与 [L1-GOV-ISO-MTD-12345]", ["L3-DAT-DSM-SRC-001", "L1-GOV-ISO-MTD-12345"]),
    ("[L5-RES-IRE-X1-BMK-001]", ["L5-RES-IRE-X1-BMK-001"]),              # 中间 4 段
    ("|L2-ARC-NSG-GLS-010】", ["L2-ARC-NSG-GLS-010"]),
    ("重复 [L2-OPS-SOC-TPL-007] 再引 [L2-OPS-SOC-TPL-007]", ["L2-OPS-SOC-TPL-007"]),
]
LEGACY_MISSES = [
    "L2-OPS-SOC-TPL-007 没有括号", "[L6-OPS-SOC-TPL-007]", "[L2-OPS-SOC-007]", "[L2-OPS-SOC-TPL-07]",
    "[l2-ops-soc-tpl-007]", "[L2-OPS-SOC-TPL-123456]", "[L2-O-SOC-TPL-007]", "[L2-OPS-SOC-TPL-007",
    "[L2-OPS-SOCSOCSOC-TPL-007]", "[L2-A-B-C-D-E-007]",
]
CORPUS = [t for t, _ in LEGACY_HITS] + LEGACY_MISSES + [
    "SOC 现有 6 人，全部白班 [I-01-02]，夜间无人值守 [I-01-02]。[D-09] [S-01] [推断] [模型推断]",
    "【人员1】负责 SOC 的 L1 分析，SIEM 告警没有工单闭环 [S-01]。见 D-09-01 和 O-12。",
    "**加粗 [L2-OPS-SOC-TPL-007]** 表格 | 值班 | [L4-CMP-GEN-LAW-0012] |",
    "1. 第一条 [L2-OPS-SOC-TPL-007]\n2. 第二条【依据待核实】",
]


@pytest.mark.parametrize(("text", "want"), LEGACY_HITS)
def test_legacy_variants_extracted(text, want):
    assert refs.extract_refs(text) == want
    assert all(refs.is_ref(r) for r in want)


@pytest.mark.parametrize("text", LEGACY_MISSES)
def test_legacy_non_refs_ignored(text):
    assert refs.extract_refs(text) == []


def test_new_format_reserved():
    assert refs.extract_refs("见 [KB-000123] 和【依据：示范｜KB-004567】") == ["KB-000123", "KB-004567"]
    assert refs.is_ref("KB-000123") and refs.is_legacy("L2-OPS-SOC-TPL-007") and not refs.is_legacy("KB-000123")
    for bad in ("[KB-12345]", "[KB-0001234]", "[kb-000123]", "[KB000123]"):
        assert refs.extract_refs(bad) == []


@pytest.mark.parametrize("text", CORPUS)
def test_call_sites_unchanged_on_legacy_text(text):
    """交付件解析、导出脚注、严格档剔除：旧格式正文的结果与收口前逐字一致。"""
    assert refs.extract_refs(text) == list(dict.fromkeys(m.group(1) for m in OLD_KB_REF.finditer(text)))
    assert [m.groups() for m in export._REF.finditer(text)] == [m.groups() for m in OLD_EXPORT_REF.finditer(text)]
    assert cards._CITATION_RE.sub(" ", text) == OLD_CITATION_RE.sub(" ", text)


def test_strict_strip_covers_new_format():
    assert cards.placeholder_problems("【客户】建议建立值守 [KB-000123]。") == []


def _js_pattern() -> str:
    src = (ROOT / "frontend/src/kbRef.js").read_text(encoding="utf-8")
    m = re.search(r"KB_REF_PATTERN\s*=\s*String\.raw`([^`]*)`", src)
    assert m, "frontend/src/kbRef.js 里找不到 KB_REF_PATTERN"
    return m.group(1)


def test_frontend_pattern_matches_backend():
    assert _js_pattern() == refs.REF_PATTERN


def test_frontend_docbody_uses_shared_pattern():
    src = (ROOT / "frontend/src/components/DocBody.jsx").read_text(encoding="utf-8")
    assert "KB_REF_PATTERN" in src


def test_no_second_hardcoded_ref_regex():
    """全仓只能在 app/kb/refs.py 和 frontend/src/kbRef.js 里写知识编号正则。"""
    allowed = {ROOT / "backend/app/kb/refs.py", ROOT / "frontend/src/kbRef.js"}
    needle = re.compile(r"L\[1-5\]\(\?:-|KB-\\d\{6\}")
    bad = []
    for base, globs in ((ROOT / "backend/app", ("*.py",)), (ROOT / "frontend/src", ("*.js", "*.jsx"))):
        for g in globs:
            for f in base.rglob(g):
                if f not in allowed and needle.search(f.read_text(encoding="utf-8")):
                    bad.append(str(f.relative_to(ROOT)))
    assert bad == []


# ---- resolve ----

def _publish(title: str, retire: bool = False) -> str:
    author, *_ = create_user(("knowledge_expert",))
    expert, *_ = create_user(("knowledge_expert",))
    admin, *_ = create_user(("knowledge_admin",))
    with SessionLocal() as db:
        u = {n: db.scalar(select(User).where(User.username == n)) for n in (author, expert, admin)}
        m = ItemMeta(layer="L2", domain="OPS", product="SOC", doc_type="TPL", stage="设计", capabilities=["管理"])
        v = rv.new_item(db, u[author], m, VersionContent(title=title, body=f"## {title}\n\n示范做法。",
                                                         source_type="public", source_detail="公开资料：示范"))
        rv.submit(db, u[author], v)
        rv.review(db, u[expert], v, "content", "approve")
        rv.review(db, u[admin], v, "compliance", "approve")
        doc_id = v.item.doc_id
        if retire:
            rv.retire(db, u[admin], v.item, "解析测试下线")
        return doc_id


def test_resolve_by_doc_id():
    live, gone = _publish(f"解析测试-现行{secrets.token_hex(2)}"), _publish("解析测试-下线", retire=True)
    with SessionLocal() as db:
        r = refs.resolve(db, [live, gone, "L2-OPS-SOC-TPL-99999", "KB-000001", live])
    assert set(r) == {live, gone}
    assert r[live].doc_id == live and r[live].via == "doc_id" and not r[live].retired
    assert r[live].current_version_id is not None and r[live].layer == "L2" and r[live].usable
    assert r[gone].retired and not r[gone].usable
    with SessionLocal() as db:
        assert refs.resolve(db, []) == {}
        many = refs.resolve(db, [live] + [f"L2-OPS-SOC-TPL-{n:05d}" for n in range(90000, 91200)])
    assert set(many) == {live}
