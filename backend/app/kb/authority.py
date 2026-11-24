"""来源权威分级（三期 B27 最小版，平台侧）。

三级：1 法律法规与监管文件（法律、行政法规、监管通报），2 标准（国家标准、行业标准），3 参考资料（白皮书、厂商资料）。
内部、专家、项目沉淀的条目不分级（返回 None），检索排序时排在分级条目之后的同分位置。
#282（B16 来源登记表）合并后改为从来源登记表继承等级，这里的推断作兜底。只看依据字段，不读正文、不调模型。
"""

from __future__ import annotations

import re

LABELS: dict[int, str] = {1: "法律法规与监管文件", 2: "标准", 3: "参考资料"}
REFERENCE_LEVEL = 3   # 只有三级来源支撑的结论标“参考资料”，不写成合规要求

_STANDARD_CODE = re.compile(
    r"^\s*(GB(/[TZ])?|[A-Z]{2}/[TZ]|ISO(/IEC)?|IEC|NIST\s*SP|ITU-T|T/[A-Z]+)\s*[\d.\-—]", re.I)
_REFERENCE = re.compile(r"白皮书|报告|蓝皮书|厂商|产品手册|最佳实践|案例集")
_LAW = re.compile(r"(法|条例|办法|规定|决定|细则|通知|通报|意见|指引|令|公告)$")
_NOT_LAW = re.compile(r"方法$|做法$|算法$")


def _clean(s: str) -> str:
    s = re.sub(r"[《》“”\"'\s]", "", s or "")
    return re.sub(r"[（(][^）)]*[）)]$", "", s)   # 去掉末尾括注，如“（2021 年修订）”


def infer_level(*, ref_document: str = "", ref_code: str = "", source_type: str = "", layer: str = "") -> int | None:
    """按依据字段推等级；内部 / 专家 / 项目条目返回 None。"""
    doc, code = _clean(ref_document), (ref_code or "").strip()
    if not doc and not code:
        return REFERENCE_LEVEL if source_type == "public" else None
    if _REFERENCE.search(doc):
        return REFERENCE_LEVEL
    if _STANDARD_CODE.match(code) or _STANDARD_CODE.match(ref_document or "") or doc.endswith("标准"):
        return 2
    if _LAW.search(doc) and not _NOT_LAW.search(doc):
        return 1
    if code and re.search(r"〔\d{4}〕|\[\d{4}\]|令第?\d+号|第\d+号", code):
        return 1   # 文号（国办发〔2021〕xx 号、令第 xx 号）
    return REFERENCE_LEVEL if source_type == "public" or layer == "L4" else None


def label(level: int | None) -> str:
    return LABELS.get(level or 0, "")


def sort_rank(level: int | None) -> int:
    """检索同分时的排序键：高等级在前，不分级的最后。"""
    return level if level in LABELS else len(LABELS) + 1
