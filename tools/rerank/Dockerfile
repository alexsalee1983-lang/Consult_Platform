# 重排服务（决策记录 2026-11-11）：在 CPU 上运行交叉编码重排模型 bge-reranker-v2-m3，提供 /v1/rerank。
# 知识检索 RRF 融合后的前若干个片段经网关（gateway.rerank）送来重新打分；不可用时检索退回 RRF 结果。
#
# 模型文件在构建时下载一次并打进镜像，运行时不联网（HF_HUB_OFFLINE=1）。写法与 tools/embed/Dockerfile 相同。
# 构建机连不上外网时：HF_ENDPOINT 指向公司内部的模型镜像站。
# 默认 int8 量化版（约 0.6 GB，CPU 上比完整版快约 3 倍，示范评测结果相同；决策记录 11-11 补充）。
# 换完整版（约 2.3 GB）：构建参数 RERANK_MODEL=BAAI/bge-reranker-v2-m3，.env 的 RERANK_MODEL 改成同名。
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /srv
ARG PIP_INDEX_URL=https://pypi.org/simple
ARG HF_ENDPOINT=https://huggingface.co
ARG RERANK_MODEL=BAAI/bge-reranker-v2-m3-int8
# 构建机需要代理时：docker compose build --build-arg HTTP_PROXY=... --build-arg HTTPS_PROXY=...
# HTTP_PROXY 等是 Docker 预定义的构建参数：**不要用 ARG 声明**（会把代理地址留在镜像元数据里），见 tools/embed/Dockerfile。
COPY requirements.txt .
RUN pip install -i ${PIP_INDEX_URL} -r requirements.txt
ENV FASTEMBED_CACHE_PATH=/models RERANK_MODEL=${RERANK_MODEL}
# 下载有超时：连不上时报错退出，不会无声地一直卡住
RUN if [ -n "${HTTPS_PROXY}" ]; then p="已设置"; else p="未设置"; fi && \
    echo "正在下载重排模型 ${RERANK_MODEL}（来源 ${HF_ENDPOINT}，代理：${p}；不打印代理地址）" && \
    export http_proxy="${HTTP_PROXY}" https_proxy="${HTTPS_PROXY}" no_proxy="${NO_PROXY}" \
           HF_HUB_DOWNLOAD_TIMEOUT=60 HF_HUB_ETAG_TIMEOUT=30 HF_ENDPOINT="${HF_ENDPOINT}" && \
    timeout 2400 python -c "import os; from fastembed.rerank.cross_encoder import TextCrossEncoder; \
m = TextCrossEncoder(os.environ['RERANK_MODEL'], cache_dir='/models'); \
s = list(m.rerank('值班交接要写什么', ['交接班记录应包含未闭环事件', '外汇交易头寸上限'])); print('model ready', s)"
COPY app.py .
RUN useradd -r -u 10001 app && chmod -R a+rX /models
USER app
ENV HF_HUB_OFFLINE=1
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --retries=8 --start-period=60s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=4).status==200 else 1)"
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
