"""知识条目文件：一条一个 Markdown 文件，开头是 YAML 元数据头，后面是正文。

    ---
    key: soc-night-duty              # 固定标识：小写字母、数字、连字符；重复导入据此修订；评测题按它对应条目
    title: 无夜班客户的夜间值守做法
    layer: L2                        # 以下取值见 app/kb/meta.py（公共规范第 7 节）
    domain: OPS
    product: SOC                     # 多个产品写列表 [SOC, ISO]，第一个是主产品；GEN 不和具体产品同时出现
    doc_type: MTD
    stage: 设计
    capabilities: [运营]
    industries: [通用]
    tags: [等保]                     # 可选：检索加权标签，对应模块配置的 kb_tags（二期 B2）
    control_ids: [SOC-MON-01]        # 可选：公司控制项编号（4A 埋点，控制项中枢之前只记不用）
    source_type: public              # 批量导入只收公开资料；其他来源走投料箱 + 去标识
    source_detail: 公开资料：……       # 文件名、标准编号、发布机关
    summary: ……                      # 可选
    applicability: ……                # 可选
    ref_document: 中华人民共和国网络安全法   # L4 依据必填：文件名、版本号、生效状态
    ref_version: 2016
    ref_status: current              # current / upcoming / repealed / superseded
    ref_code: GB/T 22239-2019        # 可选（二期 B7）：文号或标准号
    ref_replaced_by: ……              # 可选：已被替代时写替代文件名和版本
    effective_date: 2017-06-01       # 可选：施行日期（即将实施的到日自动转现行，三期 B26）
    repeal_date: 2031-01-01          # 可选：废止日（到日自动转已废止 / 已被替代）
    ref_replaces: ……                 # 可选：本文件替代的旧文件名称或文号（本文件施行时旧文件自动转已被替代）
    ref_amends: ……                   # 可选：本文件部分修订的文件名称或文号（施行时只提示复核，不改状态）
    model: deepseek-v4.1-flash       # Agent 起草时平台写入的起草模型（三期 G2③），知识审核据此换家族；专家修改不用动
    ---
    ## 章节标题（依据类用条款号，如“第二十一条 网络安全等级保护”）

    正文……

doc_id 由平台入库时按序号分配（items.next_doc_id，KB-000123，B15 起不带分类含义），文件名或正文里写的编号不算数；同一条目靠 key 对应。
Agent 起草（app/kb/drafting.py）输出的也是这个格式，专家改好后再用 kb-import 导入。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

import yaml

from . import mapping, meta
from .items import ItemMeta, VersionContent

KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$")
META_KEYS = ("layer", "domain", "product", "doc_type", "stage", "capabilities", "industries", "classification",
             "visible_roles", "owner", "tags", "control_ids",
             "language", "jurisdiction", "translation_of", "translation_notice")   # 后四项：四期 B28①
CONTENT_KEYS = ("title", "source_type", "source_detail", "summary", "applicability", "change_note", "ref_document",
                "ref_version", "ref_status", "ref_code", "ref_replaced_by",
                "ref_replaces", "ref_amends", "model")


class EntryError(ValueError):
    pass


@dataclass
class Entry:
    key: str
    meta: ItemMeta
    content: VersionContent

    @property
    def title(self) -> str:
        return self.content.title


def parse_entry(text: str) -> Entry:
    m = re.match(r"^﻿?---\s*\n(.*?)\n---\s*\n(.*)$", text, re.S)
    if not m:
        raise EntryError("文件开头缺少 --- 包起来的元数据头")
    try:
        head = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError as e:
        raise EntryError(f"元数据头不是合法的 YAML：{str(e)[:100]}") from e
    if not isinstance(head, dict):
        raise EntryError("元数据头必须是“字段: 值”的形式")
    key = str(head.get("key", "")).strip()
    if not KEY_RE.match(key):
        raise EntryError(f"key 不合法：{key!r}（小写字母、数字、连字符，3～64 位）")
    body = m.group(2).strip()
    if not body:
        raise EntryError("正文为空")
    def as_list(v) -> list:
        return v if isinstance(v, list) else [x.strip() for x in str(v).split(",") if x.strip()]

    meta_kw = {k: head[k] for k in META_KEYS if k in head}
    if isinstance(meta_kw.get("product"), list):    # 多个产品（B15）：第一个是主产品
        meta_kw["products"] = [str(x).strip() for x in meta_kw["product"] if str(x).strip()]
        meta_kw["product"] = meta_kw["products"][0] if meta_kw["products"] else ""
    for k in ("capabilities", "industries", "visible_roles", "tags", "control_ids"):
        if k in meta_kw:
            meta_kw[k] = [str(x) for x in as_list(meta_kw[k])]
    try:
        item_meta = ItemMeta(**meta_kw)
    except TypeError as e:
        raise EntryError(f"元数据缺少必填字段：{e}") from e
    content_kw = {k: str(head[k]) for k in CONTENT_KEYS if k in head and head[k] is not None}
    if "title" not in content_kw or "source_type" not in content_kw:
        raise EntryError("元数据缺少 title 或 source_type")
    content = VersionContent(body=body, **content_kw)
    for k in ("effective_date", "repeal_date"):        # repeal_date：废止日（三期 B26）
        if head.get(k) not in (None, ""):
            try:
                setattr(content, k, date.fromisoformat(str(head[k])))
            except ValueError as e:
                raise EntryError(f"{k} 不是 YYYY-MM-DD 日期：{head[k]!r}") from e
    try:
        item_meta.check()
        content.check(item_meta.layer)
        if item_meta.doc_type == "MAP":
            mapping.parse_rows(body)          # 映射矩阵：正文表格格式见 app/kb/mapping.py（B3）
    except meta.MetaError as e:
        raise EntryError(str(e)) from e
    return Entry(key=key, meta=item_meta, content=content)


def format_entry(e: Entry) -> str:
    head: dict = {"key": e.key, "title": e.content.title}
    for k in META_KEYS:
        v = getattr(e.meta, k)
        if k == "product" and len(e.meta.products) > 1:
            v = list(e.meta.products)
        if v not in ("", None, []) and not (k == "classification" and v == "internal") \
                and not (k == "visible_roles" and v == ["consultant", "admin"])                 and not (k == "language" and v == "zh") and not (k == "jurisdiction" and v == "CN"):
            head[k] = v
    for k in CONTENT_KEYS:
        v = getattr(e.content, k)
        if k != "title" and v:
            head[k] = v
    for k in ("effective_date", "repeal_date"):
        if getattr(e.content, k):
            head[k] = getattr(e.content, k).isoformat()
    return "---\n" + yaml.safe_dump(head, allow_unicode=True, sort_keys=False).strip() + "\n---\n\n" + \
        e.content.body.strip() + "\n"
