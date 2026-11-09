"""知识条目文件：一条一个 Markdown 文件，开头是 YAML 元数据头，后面是正文。

    ---
    key: soc-night-duty              # 固定标识：小写字母、数字、连字符；重复导入据此修订；评测题按它对应条目
    title: 无夜班客户的夜间值守做法
    layer: L2                        # 以下取值见 app/kb/meta.py（公共规范第 7 节）
    domain: OPS
    product: SOC
    doc_type: MTD
    stage: 设计
    capabilities: [运营]
    industries: [通用]
    source_type: public              # 批量导入只收公开资料；其他来源走投料箱 + 去标识
    source_detail: 公开资料：……       # 文件名、标准编号、发布机关
    summary: ……                      # 可选
    applicability: ……                # 可选
    ref_document: 中华人民共和国网络安全法   # L4 依据必填：文件名、版本号、生效状态
    ref_version: 2016
    ref_status: current
    ---
    ## 章节标题（依据类用条款号，如“第二十一条 网络安全等级保护”）

    正文……

Agent 起草（app/kb/drafting.py）输出的也是这个格式，专家改好后再用 kb-import 导入。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import yaml

from . import meta
from .items import ItemMeta, VersionContent

KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$")
META_KEYS = ("layer", "domain", "product", "doc_type", "stage", "capabilities", "industries", "classification",
             "visible_roles", "owner")
CONTENT_KEYS = ("title", "source_type", "source_detail", "summary", "applicability", "change_note", "ref_document",
                "ref_version", "ref_status")


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
    for k in ("capabilities", "industries", "visible_roles"):
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
    try:
        item_meta.check()
        content.check(item_meta.layer)
    except meta.MetaError as e:
        raise EntryError(str(e)) from e
    return Entry(key=key, meta=item_meta, content=content)


def format_entry(e: Entry) -> str:
    head: dict = {"key": e.key, "title": e.content.title}
    for k in META_KEYS:
        v = getattr(e.meta, k)
        if v not in ("", None, []) and not (k == "classification" and v == "internal") \
                and not (k == "visible_roles" and v == ["consultant", "admin"]):
            head[k] = v
    for k in CONTENT_KEYS:
        v = getattr(e.content, k)
        if k != "title" and v:
            head[k] = v
    return "---\n" + yaml.safe_dump(head, allow_unicode=True, sort_keys=False).strip() + "\n---\n\n" + \
        e.content.body.strip() + "\n"
