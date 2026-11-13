"""模拟的大模型服务（OpenAI 兼容），只用于 CI 和测试机，代替公司模型平台跑通网关的真实 HTTP 路径。

- GET  /v1/models                列出 MOCK_MODELS 里的模型
- POST /v1/chat/completions      普通和流式（stream=true，SSE）两种；流式最后一段带 usage
- 问题里含 [mock-503-once] 时，同一个问题第一次返回 503，用于验证网关重试
- 问题里含 [mock-break] 时，流式输出几段后断开，用于验证“输出中断”的处理
- 交付链各步（问题含“交付链第”）：返回带章节标题、占位符和来源标注的固定草稿，来源取问题里出现的证据编号
  （严格档优先 S-xx），用于跑通严格档“起草 → 导出 → 回填”
- 质量评审（问题含“做质量评审”）：返回符合平台解析格式的固定评审意见（无阻断）

不记录、不回显请求内容；回复只说明收到了多少字。不要部署到生产环境。
"""
import hashlib
import json
import os
import re
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

MODELS = [m.strip() for m in os.environ.get("MOCK_MODELS", "deepseek-v4.1-flash").split(",") if m.strip()]
_failed_once: set[str] = set()

app = FastAPI(title="mock-llm")


@app.get("/v1/models")
def models():
    return {"object": "list", "data": [{"id": m, "object": "model", "owned_by": "mock"} for m in MODELS]}


_CODE = re.compile(r"(?<![A-Za-z0-9-])([SIDO]-\d{2,4})(?![0-9-])")
REVIEW = "# 评审意见\n评审结论：通过\n统计：阻断 0 项，重要 0 项，建议 1 项\n\n| # | 位置 | 维度 | 严重度 | 问题 | 修改建议 |\n|---|---|---|---|---|---|\n| 1 | 第 1 章 | 表达 | 建议 | 模拟模型的固定意见 | 正式评审需接入白名单大模型 |\n\n| 挑战者角色 | 可能的问题 | 当前稿件能否应对 | 建议补充 |\n|---|---|---|---|\n| 项目经理 | 模拟问题 | 能 | 无 |\n"


def _reply(question: str) -> str:
    # 不写“公司”等机构后缀，免得严格档的占位符检查把模拟回复当成机构名称拦下
    return (f"【模拟模型】收到 {len(question)} 字的问题。这是模拟服务的固定回复，用于验证网关的路由、重试、"
            "流式输出和用量计量；正式答复需接入白名单大模型。")


def _draft(question: str) -> str:
    codes = sorted(dict.fromkeys(_CODE.findall(question)), key=lambda c: (not c.startswith("S-"), c))
    a, b = (codes + ["推断", "推断"])[:2] if codes else ("推断", "推断")
    return (f"## 现状概述\n【客户】夜间值守安排不完整，告警处置依赖白天人工复核 [{a}]。\n\n"
            f"## 主要差距\n【系统A】特权账号未定期复核，由【人员1】跟踪整改 [{b}]。\n")


@app.post("/v1/chat/completions")
async def completions(request: Request):
    body = await request.json()
    model = body.get("model", "")
    if model not in MODELS:
        return JSONResponse({"error": {"message": f"model {model} not found"}}, status_code=404)
    question = next((m.get("content", "") for m in reversed(body.get("messages", [])) if m.get("role") == "user"), "")
    key = hashlib.sha256(question.encode()).hexdigest()
    if "[mock-503-once]" in question and key not in _failed_once:
        _failed_once.add(key)
        return JSONResponse({"error": {"message": "temporarily unavailable"}}, status_code=503)

    system = next((m.get("content", "") for m in body.get("messages", []) if m.get("role") == "system"), "")
    # 脱敏审查 Agent 要求只输出 JSON：返回固定的审查结果（不识别任何名称），验证平台解析和报告流程
    if "residual_terms" in system:
        text = json.dumps({"conclusion": "有条件通过", "risk_level": "低", "residual_terms": [],
                           "combination_risks": [], "manual_items": ["模拟模型：没有做真实审查，需人工复核"]},
                          ensure_ascii=False)
    elif "做质量评审" in question:
        text = REVIEW
    elif "交付链第" in question:
        text = _draft(question)
    else:
        text = _reply(question)
    usage = {"prompt_tokens": sum(len(m.get("content", "")) for m in body.get("messages", [])),
             "completion_tokens": len(text)}
    usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
    created = int(time.time())

    if not body.get("stream"):
        return {"id": "mock-" + key[:12], "object": "chat.completion", "created": created, "model": model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
                "usage": usage}

    def events():
        pieces = [text[i:i + 8] for i in range(0, len(text), 8)]
        for n, piece in enumerate(pieces):
            if "[mock-break]" in question and n == 3:
                raise RuntimeError("mock stream break")  # 连接被异常断开
            chunk = {"id": "mock-" + key[:12], "object": "chat.completion.chunk", "created": created, "model": model,
                     "choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}]}
            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
        final = {"id": "mock-" + key[:12], "object": "chat.completion.chunk", "created": created, "model": model,
                 "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
        yield f"data: {json.dumps(final)}\n\n"
        if (body.get("stream_options") or {}).get("include_usage"):
            yield f"data: {json.dumps({'model': model, 'choices': [], 'usage': usage})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")
