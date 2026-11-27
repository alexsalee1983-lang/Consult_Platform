"""机密级内容检测。

两档项目都不收集机密级资料：账号口令、密钥、可利用的漏洞细节、完整网络拓扑与 IP 规划、
业务数据和个人信息样本。本模块在内容进入平台或发往大模型之前做规则检测，命中即拦截。

只依赖标准库，便于单独测试。规则是“宁可多拦”的保守策略，误拦时由顾问改写成问题描述。
"""
from __future__ import annotations

import ipaddress
import re
import string
import unicodedata
from dataclasses import dataclass, field

from ..core import metrics


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
# 不带“口令”“密码”字样的口令（#36 测试发现漏拦）。三条规则，都只认“像口令”的串：
# ① 常见账号 + / 或 : + 口令，例如“admin/Abc@12345”“root:123456”；
_SECRET_CHARS = r"[A-Za-z0-9" + re.escape(_SECRET_SYMBOLS) + r"]"
_ACCOUNTS = (
    r"admin|administrator|root|sa|sys|system|sysadmin|secadmin|auditadmin|guest|test|user|"
    r"oracle|postgres|mysql|ftp|operator|manager|super|superuser|weblogic|tomcat|nacos|"
    r"redis|elastic|grafana|jenkins|support|default"
)
#    账号前面是 / 或 . 的（网址路径、代码）不算；口令后面紧跟 / 的（还是路径）不算。
_PAIR_CHARS = r"[A-Za-z0-9!@#$%^&*_+\-.~]"
_ACCOUNT_PAIR = re.compile(
    r"(?i)(?<![A-Za-z0-9_/.\\])(" + _ACCOUNTS + r")\s*[/:：]\s*(" + _PAIR_CHARS + r"{6,})(?![/\\])"
)
# ② 登录语境里的口令串（三类字符且含 !@#$ 等符号），例如“用 Abc@12345 登录”“login with P@ssw0rd1”；
_LOGIN_CONTEXT = re.compile(
    r"(?i)(?:用|使用|以|输入|凭|通过|with)\s*(" + _SECRET_CHARS + r"{6,})\s*"
    r"(?:登录|登陆|登入|进入|连接|认证|访问|login|log\s?in|sign\s?in)"
)
# ③ 单独出现、同时含大写、小写、数字和特殊符号（@#$%!&* 等）的 8 位以上串，例如“P@ssw0rd”。
#    连字符、点、下划线不算特殊符号，版本号、标准号、产品型号不会命中；邮箱另行排除。
#    串只由字母、数字和 !@#$%^&*_+-.~ 组成，前后紧挨 = / ( 的不算（配置项、网址路径、代码）。
_STRONG_SPECIAL = set("!@#$%^&*")
_TOKEN = re.compile(
    r"(?<![A-Za-z0-9!@#$%^&*_+\-.~=/])(" + _PAIR_CHARS + r"{8,64})(?![A-Za-z0-9!@#$%^&*_+\-.~=/(])"
)
_CLOUD_KEY = re.compile(r"\b(?:AKIA[0-9A-Z]{16}|LTAI[0-9A-Za-z]{12,20}|sk-[A-Za-z0-9_\-]{20,})\b")
_IPV4 = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(/\d{1,2})?(?![\d.])")
# 标准条款号（GB/T 22239-2019 第 8.1.4.1 条）形如 IPv4，不算 IP：紧挨“第”、后面紧跟“条”或“款”，
# 或紧跟在标准号后面（标准号与条款号之间只隔年份、空格、“第”）。带 /掩码 的网段写法照常拦截。测试 #38。
_CLAUSE_BEFORE = re.compile(r"第\s*$")
_CLAUSE_AFTER = re.compile(r"^\s*(?:条(?!目)|款)")   # 不收“节项章”：10.1.20.1 节点、项目网 仍算 IP
_STANDARD_REF = re.compile(r"(?:GB(?:/[TZ])?|[A-Z]{2}/T|ISO(?:/IEC)?|IEC)\s*\d+(?:[.\-]\d+)*(?:[-—:：]\d{4})?\s*(?:第\s*)?$")
_CN_ID = re.compile(r"(?<!\d)(\d{17}[\dXx])(?!\d)")
_BANK_CARD = re.compile(r"(?<!\d)(\d{16,19})(?!\d)")
_MOBILE = re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")
# 分段写法：138-1234-5678、138 1234 5678；银行卡 6222 0212 3456 7890 123（渗透测试 M5 ⑦）
_MOBILE_SPLIT = re.compile(r"(?<![\d-])(1[3-9]\d[- ]\d{4}[- ]\d{4})(?![\d-])")
_BANK_CARD_SPLIT = re.compile(r"(?<![\d-])(\d{4}(?:[- ]\d{4}){3}(?:[- ]\d{1,3})?)(?![\d-])")
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

_ID_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
_ID_CHECK = "10X98765432"


# 法文法律术语“secret professionnel”（职业保密义务）不是口令（负责人 2026-11-16 同意，只放行这一个术语）。
_SECRET_TERMS = frozenset({"professionnel", "professionnelle", "professionnels", "professionnelles"})


def _legal_term(keyword: str, value: str) -> bool:
    return keyword.lower() == "secret" and value.rstrip(".,;:!?)").lower() in _SECRET_TERMS


def _looks_like_secret(s: str) -> bool:
    classes = (
        any(c.isascii() and c.isalpha() for c in s),
        any(c.isdigit() for c in s),
        any(c in _SECRET_SYMBOLS for c in s),
    )
    return sum(classes) >= 2


def _char_classes(s: str) -> int:
    return sum((
        any(c.isascii() and c.isupper() for c in s),
        any(c.isascii() and c.islower() for c in s),
        any(c.isdigit() for c in s),
        any(c in _SECRET_SYMBOLS for c in s),
    ))


def _account_secret(s: str) -> bool:
    """账号后面的串是否像口令：6 位以上且带数字，并且满足其一——
    纯数字（“root:123456”，弱口令也算口令）、含大写或 !@#$ 等符号（“admin/Abc@12345”）、
    只有字母和数字（“admin/admin123”）。只有小写、数字和 - . 的不算，
    这样“admin/agent-grants”这类路径、“redis:7-alpine”“mysql:5.7.44”这类镜像标签和版本号不会命中。"""
    if len(s) < 6 or not any(c.isdigit() for c in s):
        return False
    if s.isdigit() or s.isalnum():
        return True
    return any(c.isascii() and c.isupper() for c in s) or any(c in _STRONG_SPECIAL for c in s)


def _strong_token(s: str) -> bool:
    if s.lower().startswith("www.") or _EMAIL.fullmatch(s):
        return False
    return (
        any(c.isascii() and c.isupper() for c in s)
        and any(c.isascii() and c.islower() for c in s)
        and any(c.isdigit() for c in s)
        and any(c in _STRONG_SPECIAL for c in s)
    )


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


def _is_clause_number(text: str, m: re.Match) -> bool:
    before, after = text[max(0, m.start() - 30):m.start()], text[m.end():m.end() + 3]
    return bool(_CLAUSE_BEFORE.search(before) or _CLAUSE_AFTER.match(after) or _STANDARD_REF.search(before))


def _normalize(text: str) -> tuple[str, list[int]]:
    """全角字母数字符号转半角、全角空格转空格，去掉零宽字符等格式字符（Unicode Cf 类）。

    中文输入法打出的全角 IP、身份证号，复制粘贴带进来的零宽字符，都会让规则漏判（渗透测试 M5 ⑦）。
    返回规范化文本和每个字符在原文里的位置，命中位置据此换回原文位置。
    """
    out: list[str] = []
    pos: list[int] = []
    for i, ch in enumerate(text):
        o = ord(ch)
        if 0xFF01 <= o <= 0xFF5E:
            ch = chr(o - 0xFEE0)
        elif o == 0x3000:
            ch = " "
        elif unicodedata.category(ch) == "Cf":
            continue
        out.append(ch)
        pos.append(i)
    return "".join(out), pos


def check(text: str) -> GuardResult:
    if not text:
        return GuardResult(False, [])
    norm, pos = _normalize(text)
    result = _check(norm)
    if norm != text:
        for f in result.findings:
            f.start, f.end = pos[f.start], (pos[f.end - 1] + 1 if f.end > f.start else pos[f.start])
    return result


def _check(text: str) -> GuardResult:
    findings: list[Finding] = []

    for m in _PRIVATE_KEY.finditer(text):
        _add(findings, "private_key", "私钥", m)
    for m in _CREDENTIAL.finditer(text):
        _add(findings, "credential", "账号口令或密钥", m, 2)
    cred_spans = [(f.start, f.end) for f in findings if f.kind == "credential"]
    for m in _CREDENTIAL_BARE.finditer(text):
        if not _looks_like_secret(m.group(2)) or _legal_term(m.group(1), m.group(2)):
            continue
        if any(s < m.end(2) and m.start(2) < e for s, e in cred_spans):
            continue  # 已被带分隔符的规则命中
        _add(findings, "credential", "账号口令或密钥", m, 2)
        cred_spans.append(m.span(2))

    def _bare(m: re.Match, group: int) -> None:
        if any(s < m.end(group) and m.start(group) < e for s, e in cred_spans):
            return  # 同一串已命中，只报一次
        _add(findings, "credential", "账号口令或密钥", m, group)
        cred_spans.append(m.span(group))

    for m in _ACCOUNT_PAIR.finditer(text):
        if _account_secret(m.group(2)):
            _bare(m, 2)
    for m in _LOGIN_CONTEXT.finditer(text):
        v = m.group(1)
        if _char_classes(v) >= 3 and any(c in _STRONG_SPECIAL for c in v):  # “使用 Windows10 登录”不算
            _bare(m, 1)
    for m in _TOKEN.finditer(text):
        if _strong_token(m.group(1)):
            _bare(m, 1)
    for m in _CLOUD_KEY.finditer(text):
        _add(findings, "cloud_key", "云服务访问密钥", m)

    for m in _IPV4.finditer(text):
        try:
            ipaddress.ip_address(m.group(1))
        except ValueError:
            continue  # 例如版本号 1.2.300.4
        if not m.group(2) and _is_clause_number(text, m):
            continue
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
    for m in _BANK_CARD_SPLIT.finditer(text):
        if _luhn(re.sub(r"\D", "", m.group(1))):
            _add(findings, "bank_card", "银行卡号", m, 1)
    for m in _MOBILE.finditer(text):
        _add(findings, "mobile", "手机号", m, 1)
    for m in _MOBILE_SPLIT.finditer(text):
        _add(findings, "mobile", "手机号", m, 1)
    for m in _EMAIL.finditer(text):
        _add(findings, "email", "电子邮箱", m)

    findings.sort(key=lambda f: f.start)
    for kind in {f.kind for f in findings}:
        metrics.GUARD_BLOCKS.labels(kind).inc()
    return GuardResult(bool(findings), findings)


# ---------- 写给 AI 的指令（四期 B42 投毒防护第②层，只用于知识入口） ----------
# 知识条目只当参考资料：正文里出现“忽略之前的指令”“你现在扮演”这类写给模型的话，按疑似投毒处理。
# 与 check() 分开：对话、项目资料里讨论提示词注入是正常内容，不受影响。
_INSTRUCTION_PATTERNS = (
    ("ignore_rules", r"(?:忽略|无视|忘记|忘掉)\s*(?:你|您)?\s*(?:之前|以上|上面|前面|上述|先前|此前|所有|全部|一切)的?\s*"
                     r"(?:所有|全部|一切)?的?\s*(?:指令|指示|提示词|系统设定|设定)"),
    ("ignore_rules", r"\b(?:ignore|disregard|forget)\s+(?:all\s+|any\s+)?(?:the\s+|your\s+)?(?:previous|prior|above|"
                     r"earlier|preceding)\s+(?:instructions?|prompts?|rules?|directions?)\b"),
    ("role_override", r"(?:你|您)(?:现在|从现在起|接下来)\s*(?:是|扮演|充当|假装)\s*(?:一个|一名)?\s*"
                      r"(?:不受限制|没有限制|新的|另一个)"),
    ("role_override", r"(?:作为|身为)\s*(?:AI|人工智能|大模型|语言模型|AI\s*助手|智能体|Agent)\s*[，,]\s*(?:你|您)\s*"
                      r"(?:必须|应当|应该|要|需要)"),
    ("role_override", r"\byou\s+are\s+now\b|\bact\s+as\s+(?:an?\s+)?(?:unrestricted|jailbroken|DAN)\b"),
    ("prompt_marker", r"<\|im_start\|>|<\|im_end\|>|\[/?INST\]|<<\s*SYS\s*>>|<\s*/?\s*(?:system|assistant)\s*>"),
    ("output_control", r"(?:在|对)\s*(?:回答|回复|答复|输出)\s*(?:中|里|时)\s*(?:一律|始终|总是|务必)\s*"
                       r"(?:推荐|声称|写上|加入|插入)"),
    ("output_control", r"(?:不要|切勿)\s*(?:告诉|透露|告知)\s*(?:用户|审核人|知识专家)"),
)
_INSTRUCTION_RES = tuple((kind, re.compile(p, re.I)) for kind, p in _INSTRUCTION_PATTERNS)
INSTRUCTION_LABEL = "写给 AI 的指令"


def instructions(text: str) -> list[Finding]:
    """识别写给 AI 的指令样式内容（零宽、全角绕过先规范化）。只返回类别和打码摘录，不回显原文。"""
    if not text:
        return []
    norm, pos = _normalize(text)
    out: list[Finding] = []
    for kind, rx in _INSTRUCTION_RES:
        for m in rx.finditer(norm):
            out.append(Finding(kind, INSTRUCTION_LABEL, mask(m.group(0)), pos[m.start()], pos[m.end() - 1] + 1))
    out.sort(key=lambda f: f.start)
    return out


def invisible_count(text: str) -> int:
    """零宽、方向控制、标签字符等不可见格式字符（Unicode Cf 类）的个数：入口清洗时去掉并计数。"""
    return sum(1 for ch in text if unicodedata.category(ch) == "Cf")


def strip_invisible(text: str) -> str:
    return "".join(ch for ch in text if unicodedata.category(ch) != "Cf")
