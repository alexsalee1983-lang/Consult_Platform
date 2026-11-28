"""对外文本硬规则（四期 A31⑨）：交付件导出（Word / PPT）、周报等发给客户的文本统一过两道平台规则。

- 资质边界：测评 / 认证 / 密评结论用语（规则同质量门禁 quality.qualification_matches，否定说明不算）；
- 机密级：guard.check（口令、密钥、漏洞细节、网络规划、个人信息样本等）。
只返回命中类别，不回显原文；审计只记类别和计数。
"""
from __future__ import annotations

from ..services import guard
from .quality import qualification_matches

QUALIFICATION = "资质边界用语"


def check(text: str) -> list[str]:
    """命中类别（去重、有序）；空列表 = 可以对外。"""
    hits = [QUALIFICATION] if qualification_matches(text) else []
    r = guard.check(text)
    if r.blocked:
        hits += sorted({f.label for f in r.findings})
    return hits
