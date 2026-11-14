# 向量服务（开发、测试和一期部署用）：在 CPU 上运行小型中文向量模型，提供 OpenAI 兼容的 /v1/embeddings。
# 决策记录 2026-11-08：公司模型平台的 embedding 接口就绪前用它；就绪后只改 .env 的 EMBED_BASE_URL / EMBED_MODEL，
# 并重新向量化知识库（python -m app.cli kb-reembed）。
#
# 模型文件在构建时下载一次并打进镜像，运行时不联网（HF_HUB_OFFLINE=1）。
# 构建机连不上外网时：HF_ENDPOINT 指向公司内部的模型镜像站。
FROM python:3.12-slim
# 打上基础镜像发布后的系统安全更新（trivy 扫描，docs/安全扫描.md）
RUN apt-get update && apt-get upgrade -y --no-install-recommends && rm -rf /var/lib/apt/lists/*
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /srv
ARG PIP_INDEX_URL=https://pypi.org/simple
ARG HF_ENDPOINT=https://huggingface.co
ARG EMBED_MODEL=BAAI/bge-small-zh-v1.5
# 构建机需要代理时：docker compose build --build-arg HTTP_PROXY=... --build-arg HTTPS_PROXY=...
# HTTP_PROXY 等是 Docker 预定义的构建参数：**不要用 ARG 声明**——不声明时它们在 RUN 里照样可用，
# 且不会写进镜像历史（docker history）；声明了反而会把代理地址（可能含账号口令）留在镜像元数据里。
# 下载步骤里再补上小写写法（不同的下载库读的写法不同）。
COPY requirements.txt .
RUN pip install -i ${PIP_INDEX_URL} -r requirements.txt
ENV FASTEMBED_CACHE_PATH=/models EMBED_MODEL=${EMBED_MODEL}
# 下载有超时：连不上时报错退出，不会无声地一直卡住
RUN if [ -n "${HTTPS_PROXY}" ]; then p="已设置"; else p="未设置"; fi && \
    echo "正在下载向量模型 ${EMBED_MODEL}（来源 ${HF_ENDPOINT}，代理：${p}；不打印代理地址）" && \
    export http_proxy="${HTTP_PROXY}" https_proxy="${HTTPS_PROXY}" no_proxy="${NO_PROXY}" \
           HF_HUB_DOWNLOAD_TIMEOUT=60 HF_HUB_ETAG_TIMEOUT=30 HF_ENDPOINT="${HF_ENDPOINT}" && \
    timeout 1200 python -c "import os; from fastembed import TextEmbedding; \
m = TextEmbedding(os.environ['EMBED_MODEL'], cache_dir='/models'); \
v = list(m.embed(['安全运营中心值班制度']))[0]; print('model ready, dim', len(v))"
COPY app.py .
RUN useradd -r -u 10001 app && chmod -R a+rX /models
USER app
ENV HF_HUB_OFFLINE=1
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --retries=5 --start-period=30s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=4).status==200 else 1)"
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
