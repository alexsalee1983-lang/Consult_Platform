"""Agent md 注册表：读取 agents/01_公共规范.md 和 agents/roles/*.md，组装系统提示。

- md 正文就是系统提示，frontmatter 只读取 name、display_name、version、model_tier、tools、service_modules。
- 版本：公共规范 + 角色 md 内容的 SHA-256（前 12 位），每条对话和消息都记下，结果可追溯到提示词版本。
- 热加载：每次取用时比较文件修改时间，md 改了自动重新读取（测试机挂载 agents/ 时不用重启）。
- 就绪：数据库 agents 表里启用、有对应 md、md 的 name 与代号一致，三者都满足才可用。
- md 的 tools 字段只作说明，实际授权以 agent_tool_grants 为准；两者不一致时只记日志提示，不放开权限。
"""
from __future__ import annotations

import hashlib
import logging
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from ..core.config import get_settings

log = logging.getLogger(__name__)

COMMON_FILE = "01_公共规范.md"
TIER_LEVEL = {"推理": "reasoning", "标准": "standard", "轻量": "light"}
TOOL_NAMES = {  # md 里工具的中文名 → 工具代号
    "知识检索": "kb.search", "依据查询": "basis.lookup", "读项目区": "project.read", "查证据": "project.evidence",
    "文档生成": "doc.generate", "质量检查": "quality.check", "写待入库区": "kb.stage_write",
    "发确认卡": "confirm.create", "转交 Agent": "agent.handoff",
    "控制映射查询": "kb.mapping", "去标识检查": "deid.check",
}

# md 写了左边的工具，就视为也写了右边的（只影响不一致提示）：“查证据”是“读项目区”的细分，授权与之同列（迁移 0017）；
# “发确认卡”授权给全部交付侧 Agent，与“读项目区”同列（迁移 0019，决策记录 2026-11-10），md 不必逐个改写
IMPLIED = {"project.read": ("project.evidence", "confirm.create")}


@dataclass
class AgentDoc:
    code: str
    display_name: str
    md_version: str            # md 自己写的 version
    level: str                 # 模型能力档：reasoning / standard / light
    body: str                  # 角色 md 正文（去掉 frontmatter）
    common: str                # 公共规范全文
    version: str               # 公共规范 + 角色 md 的内容哈希（前 12 位）
    md_tools: list[str] = field(default_factory=list)
    service_modules: list[str] = field(default_factory=list)
    level_set: bool = False    # md 写了 model_tier（没写时调用方可指定默认档，app/agents/call.py）
    role_text: str = ""        # 角色 md 全文（含 frontmatter），存版本库用（四期 F3②e）


def agents_dir() -> Path:
    configured = get_settings().agents_dir
    if configured:
        return Path(configured)
    for candidate in (Path("/srv/agents"), Path(__file__).resolve().parents[3] / "agents"):
        if (candidate / COMMON_FILE).exists():
            return candidate
    return Path("/srv/agents")


def _split_frontmatter(text: str) -> tuple[dict, str]:
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            meta = yaml.safe_load(text[3:end]) or {}
            return (meta if isinstance(meta, dict) else {}), text[end + 4:].lstrip("\n")
    return {}, text


_lock = threading.Lock()
_cache: dict[str, tuple[tuple, AgentDoc | None]] = {}


def _stamp(*paths: Path) -> tuple:
    return tuple((str(p), p.stat().st_mtime_ns if p.exists() else None) for p in paths)


def load(code: str) -> AgentDoc | None:
    """读取某个 Agent 的 md；没有 md、或 md 的 name 与代号不一致时返回 None。文件变了会自动重新读取。"""
    base = agents_dir()
    common_path, role_path = base / COMMON_FILE, base / "roles" / f"{code}.md"
    stamp = _stamp(common_path, role_path)
    with _lock:
        cached = _cache.get(code)
        if cached and cached[0] == stamp:
            return cached[1]
    doc = _read(code, common_path, role_path)
    with _lock:
        _cache[code] = (stamp, doc)
    return doc


def _read(code: str, common_path: Path, role_path: Path) -> AgentDoc | None:
    if not (common_path.exists() and role_path.exists()):
        return None
    return parse(code, common_path.read_text(encoding="utf-8"), role_path.read_text(encoding="utf-8"), role_path.name)


def parse(code: str, common: str, role_text: str, name: str = "") -> AgentDoc | None:
    """由公共规范全文和角色 md 全文组装 AgentDoc（四期 F3②e：回滚时由版本库里的原文组装）。"""
    meta, body = _split_frontmatter(role_text)
    if meta.get("name") != code:
        log.warning("Agent md %s 的 name 为 %r，与代号不一致，视为未就绪", name or code, meta.get("name"))
        return None
    version = hashlib.sha256((common + "\n\x00\n" + role_text).encode()).hexdigest()[:12]
    return AgentDoc(
        code=code, display_name=str(meta.get("display_name") or code), md_version=str(meta.get("version") or ""),
        level=TIER_LEVEL.get(str(meta.get("model_tier") or "").strip(), "standard"), body=body, common=common,
        version=version, md_tools=[str(t) for t in (meta.get("tools") or [])],
        service_modules=[str(m) for m in (meta.get("service_modules") or [])],
        level_set=str(meta.get("model_tier") or "").strip() in TIER_LEVEL, role_text=role_text,
    )


_PRODUCT = re.compile(r"^## 产品：\s*(\S+)")
_MODULE = re.compile(r"^### 模块：\s*(\S+)")
NOT_READY = "未就绪"


def select_body(body: str, product: str | None, modules: list[str] | tuple[str, ...] = ()) -> tuple[str, list[str]]:
    """二期 C1：按项目裁剪 md 正文，返回 (正文, 拼入的段)。

    “## 产品：代号”段只保留本项目的产品；本项目的产品没有产品段时保留“## 产品：未就绪”；
    保留下来的产品段里“### 模块：代号”只保留所选模块。其余内容（通用骨架）原样保留。
    product 为 None（不在项目里）或 md 没有产品段时不裁剪。"""
    lines = body.split("\n")
    codes = {m.group(1) for line in lines if (m := _PRODUCT.match(line))}
    if product is None or not codes:
        return body, []
    want = product if product in codes and product != NOT_READY else NOT_READY
    picked, out, keep_product, keep_module, fenced = [], [], True, True, False
    for line in lines:
        if line.startswith("```"):
            fenced = not fenced
        elif fenced:
            pass
        elif line.startswith("# ") or line.startswith("## "):
            m = _PRODUCT.match(line)
            keep_product = (m is None) or m.group(1) == want
            keep_module = True
            if m and keep_product:
                picked.append(f"产品：{want}")
        elif line.startswith("### ") and keep_product:
            m = _MODULE.match(line)
            keep_module = (m is None) or m.group(1) in modules
            if m and keep_module:
                picked.append(f"模块：{m.group(1)}")
        if keep_product and keep_module:
            out.append(line)
    return "\n".join(out), picked


def tools_mismatch(doc: AgentDoc, granted: list[str]) -> list[str]:
    """md 的 tools 字段与授权表的差异说明（只用于提示，不影响授权）。"""
    notes = []
    named = {TOOL_NAMES[t] for t in doc.md_tools if t in TOOL_NAMES}
    named |= {i for t in named for i in IMPLIED.get(t, ())}
    unknown = [t for t in doc.md_tools if t not in TOOL_NAMES]
    if unknown:
        notes.append(f"md 中的工具不在平台工具清单里：{'、'.join(unknown)}")
    if named - set(granted):
        notes.append(f"md 写了但未授权：{'、'.join(sorted(named - set(granted)))}")
    if set(granted) - named:
        notes.append(f"已授权但 md 未写：{'、'.join(sorted(set(granted) - named))}")
    return notes


def clear_cache() -> None:
    with _lock:
        _cache.clear()
