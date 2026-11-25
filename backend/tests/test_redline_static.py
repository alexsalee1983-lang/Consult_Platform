"""数据保护红线 2 静态检查：模型调用只经 app/services/gateway.py。

除网关外不得 import HTTP 客户端库；任何模块不得 import 外部 AI SDK。只做 AST 检查，不联网。
"""
from __future__ import annotations

import ast
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
GATEWAY = APP / "services" / "gateway.py"
HTTP_CLIENTS = ("httpx", "requests", "aiohttp", "urllib.request", "http.client", "urllib3")
AI_SDKS = ("openai", "anthropic", "langchain", "litellm", "llama_index", "cohere", "google.generativeai",
           "google.genai", "dashscope", "zhipuai", "qianfan", "mistralai", "ollama", "huggingface_hub")


def _imports() -> list[tuple[Path, int, str]]:
    out = []
    for f in sorted(APP.rglob("*.py")):
        for n in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            if isinstance(n, ast.Import):
                out += [(f, n.lineno, a.name) for a in n.names]
            elif isinstance(n, ast.ImportFrom) and n.module and not n.level:
                out += [(f, n.lineno, n.module)] + [(f, n.lineno, f"{n.module}.{a.name}") for a in n.names]
    return out


def _hit(name: str, prefixes: tuple[str, ...]) -> bool:
    return any(name == p or name.startswith(p + ".") for p in prefixes)


def test_http_clients_only_in_gateway() -> None:
    bad = [f"{f.relative_to(APP)}:{ln} {m}" for f, ln, m in _imports() if f != GATEWAY and _hit(m, HTTP_CLIENTS)]
    assert not bad, "红线 2：HTTP 客户端只能在 gateway.py 使用：\n" + "\n".join(bad)


def test_no_external_ai_sdk() -> None:
    bad = [f"{f.relative_to(APP)}:{ln} {m}" for f, ln, m in _imports() if _hit(m, AI_SDKS)]
    assert not bad, "红线 2：不得引入外部 AI SDK：\n" + "\n".join(bad)
