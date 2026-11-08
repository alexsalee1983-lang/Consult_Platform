"""机密级内容检测。

两档项目都不收集机密级资料：账号口令、密钥、可利用的漏洞细节、完整网络拓扑与 IP 规划、
业务数据和个人信息样本。本模块在内容进入平台或发往大模型之前做规则检测，命中即拦截。

只依赖标准库，便于单独测试。规则是“宁可多拦”的保守策略，误拦时由顾问改写成问题描述。
"""
from __future__ import annotations

import ipaddress
import re
import string
from dataclasses import dataclass, field


@dataclass
class Finding:
    kind: str      # 规则编号
    label: str     # 给顾问看的中文说明
    excerpt: str   # 打码后的片段，不回显原文
    start: int
    end: int


@dataclass
class GuardResult:
    blocked: bool
    findings: list[Finding] = field(default_factory=list)

    def summary(self) -> str:
        if not self.blocked:
            return "未发现机密级内容"
        kinds = sorted({f.label for f in self.findings})
        return "发现机密级内容：" + "、".join(kinds)


def mask(text: str) -> str:
    """只保留首尾各 2 个字符，中间打码。"""
    text = text.strip()
    if len(text) <= 4:
        return "*" * len(text)
    return text[:2] + "*" * min(len(text) - 4, 12) + text[-2:]


# ---------- 各类检测 ----------

_PRIVATE_KEY = re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----")
_CRED_KEYWORDS = (
    r"(password|passwd|pwd|passphrase|secret|token|api[_-]?key|access[_-]?key|口令|密码|密钥)"
)
# 关键词 + 分隔符 + 值，例如“密码：Abc@12345”“password=xxx”
_CREDENTIAL = re.compile(
    r"(?i)" + _CRED_KEYWORDS + r"\s*(?:[:=：]|是|为)\s*[\"']?([^\s\"'，。；;,]{4,})"
)
# 关键词 + 空白 + 像口令的串，例如“管理口令 Abc@12345”“password P@ssw0rd”。
# 值只取 ASCII 字母、数字、符号（不含引号、逗号、分号），至少 6 位且至少含两类字符，
# 这样“口令策略需要整改”“密码复杂度要求为 8 位以上”这类问题描述不会被拦。
_SECRET_SYMBOLS = "".join(c for c in string.punctuation if c not in "\"',;")
_CREDENTIAL_BARE = re.compile(
    r"(?i)" + _CRED_KEYWORDS + r"\s+([A-Za-z0-9" + re.escape(_SECRET_SYMBOLS) + r"]{6,})"
)
_CLOUD_KEY = re.compile(r"\b(?:AKIA[0-9A-Z]{16}|LTAI[0-9A-Za-z]{12,20}|sk-[A-Za-z0-9_\-]{20,})\b")
_IPV4 = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(/\d{1,2})?(?![\d.])")
_CN_ID = re.compile(r"(?<!\d)(\d{17}[\dXx])(?!\d)")
_BANK_CARD = re.compile(r"(?<!\d)(\d{16,19})(?!\d)")
_MOBILE = re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

_ID_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
_ID_CHECK = "10X98765432"


def _looks_like_secret(s: str) -> bool:
    classes = (
        any(c.isascii() and c.isalpha() for c in s),
        any(c.isdigit() for c in s),
        any(c in _SECRET_SYMBOLS for c in s),
    )
    return sum(classes) >= 2


def _valid_cn_id(s: str) -> bool:
    total = sum(int(c) * w for c, w in zip(s[:17], _ID_WEIGHTS))
    return _ID_CHECK[total % 11] == s[17].upper()


def _luhn(s: str) -> bool:
    digits = [int(c) for c in s][::-1]
    total = 0
    for i, d in enumerate(digits):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _add(out: list[Finding], kind: str, label: str, m: re.Match, group: int = 0) -> None:
    out.append(Finding(kind, label, mask(m.group(group)), m.start(group), m.end(group)))


def check(text: str) -> GuardResult:
    findings: list[Finding] = []
    if not text:
        return GuardResult(False, findings)

    for m in _PRIVATE_KEY.finditer(text):
        _add(findings, "private_key", "私钥", m)
    for m in _CREDENTIAL.finditer(text):
        _add(findings, "credential", "账号口令或密钥", m, 2)
    cred_spans = [(f.start, f.end) for f in findings if f.kind == "credential"]
    for m in _CREDENTIAL_BARE.finditer(text):
        if not _looks_like_secret(m.group(2)):
            continue
        if any(s < m.end(2) and m.start(2) < e for s, e in cred_spans):
            continue  # 已被带分隔符的规则命中
        _add(findings, "credential", "账号口令或密钥", m, 2)
    for m in _CLOUD_KEY.finditer(text):
        _add(findings, "cloud_key", "云服务访问密钥", m)

    for m in _IPV4.finditer(text):
        try:
            ipaddress.ip_address(m.group(1))
        except ValueError:
            continue  # 例如版本号 1.2.300.4
        label = "IP 网段规划" if m.group(2) else "IP 地址"
        _add(findings, "ip", label, m)

    id_spans: list[tuple[int, int]] = []
    for m in _CN_ID.finditer(text):
        if _valid_cn_id(m.group(1)):
            _add(findings, "cn_id", "身份证号", m, 1)
            id_spans.append(m.span(1))
    for m in _BANK_CARD.finditer(text):
        if any(s <= m.start(1) < e for s, e in id_spans):
            continue
        if _luhn(m.group(1)):
            _add(findings, "bank_card", "银行卡号", m, 1)
    for m in _MOBILE.finditer(text):
        _add(findings, "mobile", "手机号", m, 1)
    for m in _EMAIL.finditer(text):
        _add(findings, "email", "电子邮箱", m)

    findings.sort(key=lambda f: f.start)
    return GuardResult(bool(findings), findings)
