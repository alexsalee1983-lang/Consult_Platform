"""知识库元数据：取值表与校验规则，依据 agents/01_公共规范.md 第 7 节（第一期标准）。

改这里的取值必须同步公共规范第 7 节（公共规范的修改要负责人同意），并更新 tests/test_kb_schema.py。
"""
from __future__ import annotations

import calendar
import re
from datetime import date

# 层级
LAYERS = {
    "L1": "方法论", "L2": "交付包", "L3": "领域知识", "L4": "依据", "L5": "经验",
}
# 一级目录（缩写 → 名称）
DOMAINS = {
    "GOV": "安全战略与治理", "CMP": "合规与认证", "DAT": "数据安全与隐私",
    "OPS": "安全运营", "RES": "应急与韧性", "ARC": "安全架构与新兴技术",
}
# 服务产品（缩写 → 名称）。第一期只有 SOC 运营体系设计（决策记录 2026-11-07）；
# GEN 用于不限服务产品的通用知识（L1 方法论、L3 领域知识、L4 依据等）。新增产品时在这里登记缩写。
PRODUCTS = {
    "SOC": "SOC 运营体系设计",
    "GEN": "通用（不限服务产品）",
}
# 交付阶段
STAGES = ("启动", "调研", "分析", "设计", "落地辅导", "验收", "通用")
# 交付物类型（缩写 → 名称）
DOC_TYPES = {
    "TPL": "模板", "GLD": "黄金样例", "POL": "制度母版", "MTD": "方法说明", "REF": "依据解读",
    "MAP": "映射矩阵", "CAS": "案例", "OBJ": "异议应答", "PIT": "踩坑记录",
}
# 能力域标签（可多选）
CAPABILITIES = ("管理", "运营", "技术支撑")
# 密级：受限条目只对 visible_roles 中的角色可见
CLASSIFICATIONS = {"internal": "内部", "restricted": "受限"}
# 来源；公司内部制度改造须写授权编号（source_detail）
SOURCE_TYPES = {
    "expert": "专家萃取", "project": "项目沉淀", "public": "公开资料", "internal": "公司内部制度改造",
}
# 脱敏状态
DEID_STATUSES = {"pending": "未脱敏", "passed": "已脱敏", "not_needed": "无需脱敏"}
# 可见角色（第一期售前只定义，不开放）
VISIBLE_ROLES = {"consultant": "交付顾问", "presales": "售前", "admin": "管理员"}
# 依据（L4）的生效状态
REF_STATUSES = {"current": "现行", "upcoming": "即将实施", "repealed": "已废止"}

# 版本状态：草稿 → 待审 →（双人审核）→ 已发布；新版本发布后旧版本为“已替代”；下线后为“已下线”
VERSION_STATUSES = {
    "draft": "草稿", "in_review": "待审", "rejected": "已退回", "published": "已发布",
    "superseded": "已替代", "retired": "已下线",
}
# 双人审核的两个角色（决策记录 2026-11-08）：知识专家审内容，知识管理员审合规；两人不能是同一人，提交人不能审核
REVIEW_ROLES = {"content": "knowledge_expert", "compliance": "knowledge_admin"}
REVIEW_DECISIONS = {"approve": "通过", "reject": "退回"}

# 复审周期：默认 12 个月，依据类（L4）6 个月
REVIEW_MONTHS_DEFAULT = 12
REVIEW_MONTHS_REF = 6

DOC_ID_RE = re.compile(r"^(L[1-5])-([A-Z]{3})-([A-Z]{2,8})-([A-Z]{3})-(\d{3,})$")


class MetaError(ValueError):
    pass


def review_months(layer: str) -> int:
    return REVIEW_MONTHS_REF if layer == "L4" else REVIEW_MONTHS_DEFAULT


def add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    year, month = d.year + y, m + 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


def default_review_date(layer: str, effective: date) -> date:
    return add_months(effective, review_months(layer))


def doc_id_prefix(layer: str, domain: str, product: str, doc_type: str) -> str:
    check_classification(layer, domain, product, doc_type)
    return f"{layer}-{domain}-{product}-{doc_type}-"


def format_doc_id(layer: str, domain: str, product: str, doc_type: str, seq: int) -> str:
    return f"{doc_id_prefix(layer, domain, product, doc_type)}{seq:03d}"


def parse_doc_id(doc_id: str) -> tuple[str, str, str, str, int]:
    m = DOC_ID_RE.match(doc_id)
    if not m:
        raise MetaError(f"doc_id 格式不对：{doc_id!r}，应为 层级-目录-产品-类型-序号，例如 L2-OPS-SOC-TPL-007")
    layer, domain, product, doc_type, seq = m.groups()
    check_classification(layer, domain, product, doc_type)
    return layer, domain, product, doc_type, int(seq)


def check_classification(layer: str, domain: str, product: str, doc_type: str) -> None:
    for value, table, name in ((layer, LAYERS, "层级"), (domain, DOMAINS, "一级目录"),
                               (product, PRODUCTS, "服务产品"), (doc_type, DOC_TYPES, "交付物类型")):
        if value not in table:
            raise MetaError(f"{name}取值不对：{value!r}，可选 {', '.join(table)}")


def check_tags(stage: str, capabilities: list[str], industries: list[str]) -> None:
    if stage not in STAGES:
        raise MetaError(f"交付阶段取值不对：{stage!r}，可选 {'、'.join(STAGES)}")
    bad = [c for c in capabilities if c not in CAPABILITIES]
    if bad or not capabilities:
        raise MetaError(f"能力域标签至少选一个，只能是 {'、'.join(CAPABILITIES)}")
    if not industries:
        raise MetaError("适用行业至少填一个；通用的填“通用”")


def check_source(source_type: str, source_detail: str) -> None:
    if source_type not in SOURCE_TYPES:
        raise MetaError(f"来源取值不对：{source_type!r}，可选 {', '.join(SOURCE_TYPES)}")
    if source_type == "internal" and not source_detail.strip():
        raise MetaError("公司内部制度改造必须写明授权编号")


def check_reference(layer: str, ref_document: str, ref_version: str, ref_status: str) -> None:
    """依据（L4）必须写明文件名、版本号和生效状态，Agent 引用时要用（公共规范第 3 节）。"""
    if layer != "L4":
        return
    if not ref_document.strip() or not ref_version.strip():
        raise MetaError("依据类条目必须填写文件名和版本号")
    if ref_status not in REF_STATUSES:
        raise MetaError(f"依据生效状态取值不对：{ref_status!r}，可选 {', '.join(REF_STATUSES)}")


def check_visibility(classification: str, visible_roles: list[str]) -> None:
    if classification not in CLASSIFICATIONS:
        raise MetaError(f"密级取值不对：{classification!r}，可选 {', '.join(CLASSIFICATIONS)}")
    bad = [r for r in visible_roles if r not in VISIBLE_ROLES]
    if bad or not visible_roles:
        raise MetaError(f"可见角色至少选一个，只能是 {', '.join(VISIBLE_ROLES)}")
