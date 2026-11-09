"""向量服务：OpenAI 兼容的 /v1/embeddings，模型在镜像里，运行时不联网。

只在内部网络开放，由模型网关（backend/app/services/gateway.py 的 embed）调用。不记请求内容，不写访问日志。
"""
import os

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastembed import TextEmbedding
from pydantic import BaseModel

MODEL = os.environ.get("EMBED_MODEL", "BAAI/bge-small-zh-v1.5")
MAX_INPUTS = 256
MAX_CHARS = 2000            # 单条输入上限（模型最长 512 个 token，超出部分截断）

app = FastAPI(title="embed", docs_url=None, redoc_url=None, openapi_url=None)
_model = TextEmbedding(MODEL, cache_dir="/models")
DIM = len(next(iter(_model.embed(["维度检查"]))))


class EmbedIn(BaseModel):
    model: str = MODEL
    input: str | list[str]


@app.get("/health")
def health():
    return {"status": "ok", "model": MODEL, "dim": DIM}


@app.get("/v1/models")
def models():
    return {"object": "list", "data": [{"id": MODEL, "object": "model", "owned_by": "local", "dim": DIM}]}


@app.post("/v1/embeddings")
def embeddings(body: EmbedIn):
    texts = [body.input] if isinstance(body.input, str) else body.input
    if body.model != MODEL:
        return JSONResponse({"error": {"message": f"model {body.model} not found"}}, status_code=404)
    if not texts or len(texts) > MAX_INPUTS:
        return JSONResponse({"error": {"message": f"input must have 1..{MAX_INPUTS} items"}}, status_code=400)
    texts = [t[:MAX_CHARS] for t in texts]
    vectors = list(_model.embed(texts))
    return {"object": "list", "model": MODEL,
            "data": [{"object": "embedding", "index": i, "embedding": [round(float(x), 6) for x in v]}
                     for i, v in enumerate(vectors)],
            "usage": {"prompt_tokens": sum(len(t) for t in texts), "total_tokens": sum(len(t) for t in texts)}}
