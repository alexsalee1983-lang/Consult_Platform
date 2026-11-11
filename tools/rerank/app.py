"""重排服务：交叉编码模型给“查询 + 候选片段”逐对打分，模型在镜像里，运行时不联网。

接口（与常见的 /v1/rerank 写法一致）：
  POST /v1/rerank {"model": ..., "query": "...", "documents": ["...", ...]}
  → {"model": ..., "results": [{"index": 0, "relevance_score": 0.93}, ...]}   按输入顺序返回
relevance_score 是模型原始分数经 sigmoid 换算到 0～1，越高越相关；依据查询门槛（KB_BASIS_MIN_RERANK）按它定。

只在内部网络开放，由模型网关（backend/app/services/gateway.py 的 rerank）调用。不记请求内容，不写访问日志。
"""
import math
import os

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastembed.rerank.cross_encoder import TextCrossEncoder
from pydantic import BaseModel

MODEL = os.environ.get("RERANK_MODEL", "BAAI/bge-reranker-v2-m3-int8")
MAX_DOCS = 100
MAX_CHARS = 1000            # 单条候选上限；网关已按 KB_RERANK_MAX_CHARS 截断，这里兜底
BATCH = int(os.environ.get("RERANK_BATCH", "8"))

app = FastAPI(title="rerank", docs_url=None, redoc_url=None, openapi_url=None)
_model = TextCrossEncoder(MODEL, cache_dir=os.environ.get("FASTEMBED_CACHE_PATH", "/models"))
list(_model.rerank("检查", ["加载检查"]))       # 启动时跑一次，确认模型可用


class RerankIn(BaseModel):
    model: str = MODEL
    query: str
    documents: list[str]


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


@app.get("/health")
def health():
    return {"status": "ok", "model": MODEL}


@app.get("/v1/models")
def models():
    return {"object": "list", "data": [{"id": MODEL, "object": "model", "owned_by": "local"}]}


@app.post("/v1/rerank")
def rerank(body: RerankIn):
    if body.model != MODEL:
        return JSONResponse({"error": {"message": f"model {body.model} not found"}}, status_code=404)
    if not body.query.strip() or not body.documents or len(body.documents) > MAX_DOCS:
        return JSONResponse({"error": {"message": f"query required, documents must have 1..{MAX_DOCS} items"}},
                            status_code=400)
    docs = [d[:MAX_CHARS] for d in body.documents]
    scores = list(_model.rerank(body.query[:MAX_CHARS], docs, batch_size=BATCH))
    return {"model": MODEL,
            "results": [{"index": i, "relevance_score": round(_sigmoid(float(s)), 6)} for i, s in enumerate(scores)]}
