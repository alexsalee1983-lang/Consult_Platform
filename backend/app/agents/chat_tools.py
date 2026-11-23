"""对话里的 Agent 自主调用工具（二期 A7，function calling）：取代“提问前检索注入”（knowledge.py）。

- 只把 Agent 已授权的**读取类**工具交给模型（tools 表 access=read 且在 SPECS 里）；写入类（doc.generate、
  confirm.create、agent.handoff）只由平台运行时调用，对话里不出现（同调试接口的限制，渗透测试 P-02）。
- 模型发起的每次调用都经 tools.invoke：授权、硬规则、项目成员检查、审计照旧；没授权的照样拒绝并记审计。
- 参数不是 JSON 对象、函数名不认识、工具报错，都把错误说明作为工具结果交回模型，不中断对话。
- 工具结果过机密级检测、截断、假名化在网关里做（gateway.stream 的 run_tool）。
- 检索类工具（kb.search、basis.lookup）的命中记为回复的引用：只有 doc_id、标题、章节、层级、引用格式。
- 开关 LLM_TOOL_CALLING（默认开）：公司模型工具调用效果不好时关掉，退回提问前检索注入。
"""
from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core.db import SessionLocal
from ..models import Tool, User
from . import tools
from .registry import TOOL_NAMES

DISPLAY = {code: name for name, code in TOOL_NAMES.items()}
_LIMIT = {"type": "integer", "description": "最多返回几条", "minimum": 1, "maximum": 20}

# 工具代号 → （说明，参数 JSON Schema）。函数名把点换成下划线（function calling 的名字不允许点）。
SPECS: dict[str, tuple[str, dict[str, Any]]] = {
    "kb.search": ("检索公司知识库（方法、案例、模板、异议应答、依据解读等，已发布且你可见的条目），"
                  "在项目里默认按本项目产品排序。回答涉及公司做法、经验时先查；使用其中内容注明 [doc_id]。",
                  {"type": "object", "properties": {
                      "query": {"type": "string", "description": "检索词，写关键问题或术语"},
                      "layer": {"type": "string", "description": "可选：L1～L4"},
                      "doc_type": {"type": "string", "description": "可选：交付物类型代号，如 MTD、CAS、OBJ"},
                      "limit": _LIMIT}, "required": ["query"]}),
    "basis.lookup": ("查 L4 依据库（法律、法规、国家和行业标准的条款）。写标准号或“第 X 条”时精确匹配。"
                     "引用法规标准只能用这里返回的引用格式；查不到的标【依据待核实】，不凭记忆写条款号。",
                     {"type": "object", "properties": {
                         "query": {"type": "string", "description": "要求要点、标准号或条款号"},
                         "ref_document": {"type": "string", "description": "可选：限定文件，如 GB/T 22239"},
                         "limit": _LIMIT}, "required": ["query"]}),
    "kb.mapping": ("查控制映射矩阵：一个标准的条款对应另一个标准的哪些条款（等同 / 部分覆盖 / 相关）。",
                   {"type": "object", "properties": {
                       "source": {"type": "string", "description": "源标准号，如 ISO/IEC 27001"},
                       "target": {"type": "string", "description": "可选：目标标准号"},
                       "control": {"type": "string", "description": "可选：控制号或条款号"},
                       "query": {"type": "string", "description": "可选：按说明里的词过滤"},
                       "limit": _LIMIT}, "required": ["source"]}),
    "project.read": ("读当前项目的画像、交付件进度和待确认事项。", {"type": "object", "properties": {}}),
    "project.evidence": ("读当前项目的证据：不带 code 返回证据清单；带 code（如 I-01、D-03、O-02，严格档为 S-01）"
                         "返回该证据正文，长的分段，用 offset 续读。引用事实时注明证据编号。",
                         {"type": "object", "properties": {
                             "code": {"type": "string", "description": "可选：证据编号"},
                             "offset": {"type": "integer", "description": "可选：从第几个字开始读"}}}),
    "quality.check": ("检查一段文字是否含机密级内容（口令、密钥、IP 规划、漏洞细节、个人信息样本）。",
                      {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}),
}
FUNC = {code: code.replace(".", "_") for code in SPECS}
BY_FUNC = {f: code for code, f in FUNC.items()}
REF_TOOLS = ("kb.search", "basis.lookup")
RETRY_HINT = "这是临时性失败，可以稍后用相同参数重试一次；仍失败就如实告诉用户，不要编造结果。"


def available(db: Session, agent_code: str) -> list[str]:
    """本 Agent 在对话里能用的工具代号：已授权、读取类、在 SPECS 里。"""
    granted = set(tools.granted_tools(db, agent_code))
    read = set(db.scalars(select(Tool.code).where(Tool.access == "read")))
    return [c for c in SPECS if c in granted and c in read]


def schemas(codes: list[str]) -> list[dict[str, Any]]:
    return [{"type": "function", "function": {"name": FUNC[c], "description": SPECS[c][0], "parameters": SPECS[c][1]}}
            for c in codes]


def runner(user_id: int, agent_code: str, project_code: str, refs: list[dict],
           used: list[str], conversation_id: int | None = None) -> Callable[[str, str], dict]:
    """返回给网关的 run_tool(函数名, 参数 JSON) → 结果 dict。每次调用新开会话（流式输出时请求的会话可能已关）。
    检索命中追加到 refs（去重），调用过的工具代号追加到 used。"""
    seen = {r["doc_id"] for r in refs}

    def run(name: str, raw: str) -> dict:
        code = BY_FUNC.get(name)
        if code is None:
            return {"error": f"没有工具 {name}"}
        try:
            args = json.loads(raw or "{}")
        except ValueError:
            return {"error": "参数不是合法的 JSON"}
        if not isinstance(args, dict):
            return {"error": "参数应为 JSON 对象"}
        used.append(code)
        with SessionLocal() as db:
            user = db.get(User, user_id)
            try:
                result = tools.invoke(db, user, agent_code, code, args, project_code, conversation_id)
            except tools.ToolError as e:
                if e.retryable:
                    return {"error": e.detail, "retryable": True, "hint": RETRY_HINT}
                return {"error": e.detail}
        if code in REF_TOOLS:
            for h in result.get("items", []):
                if h.get("doc_id") and h["doc_id"] not in seen:
                    seen.add(h["doc_id"])
                    refs.append({"doc_id": h["doc_id"], "title": h.get("title", ""), "heading": h.get("heading", ""),
                                 "layer": h.get("layer", ""), "citation": h.get("citation", "")})
        return result
    return run
