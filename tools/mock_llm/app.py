"""模拟的大模型服务（OpenAI 兼容），只用于 CI 和测试机，代替公司模型平台跑通网关的真实 HTTP 路径。

- GET  /v1/models                列出 MOCK_MODELS 里的模型
- POST /v1/chat/completions      普通和流式（stream=true，SSE）两种；流式最后一段带 usage
- 问题里含 [mock-503-once] 时，同一个问题第一次返回 503，用于验证网关重试
- 问题里含 [mock-break] 时，流式输出几段后断开，用于验证“输出中断”的处理

不记录、不回显请求内容；回复只说明收到了多少字。不要部署到生产环境。
"""
import hashlib
import json
import os
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

MODELS = [m.strip() for m in os.environ.get("MOCK_MODELS", "deepseek-v4.1-flash").split(",") if m.strip()]
_failed_once: set[str] = set()

app = FastAPI(title="mock-llm")


@app.get("/v1/models")
def models():
    return {"object": "list", "data": [{"id": m, "object": "model", "owned_by": "mock"} for m in MODELS]}


def _reply(question: str) -> str:
    return (f"【模拟模型】收到 {len(question)} 字的问题。这是模拟服务的固定回复，用于验证网关的路由、重试、"
            "流式输出和用量计量；正式答复需接入公司白名单大模型。")


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
