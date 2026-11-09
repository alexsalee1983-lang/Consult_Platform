# 构建范围是仓库根目录（docker-compose.yml 里 context: .），这样 agents/ 的 md 能打进镜像：
# 镜像 = 某个版本的代码 + 同一版本的 Agent md，可追溯。根目录的 .dockerignore 限定只带入需要的文件。
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv
COPY backend/requirements.txt .
# 国内网络可在构建时传入镜像源：--build-arg PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
ARG PIP_INDEX_URL=https://pypi.org/simple
RUN pip install -i ${PIP_INDEX_URL} -r requirements.txt

COPY backend/alembic.ini ./
COPY backend/alembic ./alembic
COPY backend/app ./app
# Agent md：运行时从 /srv/agents 读取（测试机可用只读挂载覆盖，改 md 不用重建镜像）
COPY agents ./agents
# 知识条目文件、评测题（命令行 kb-import / kb-eval 用，如 /srv/knowledge/samples）
COPY knowledge ./knowledge
# 代码和 md 归 root 所有，应用用户只读
RUN useradd -r -u 10001 app
USER app

EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --retries=5 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health',timeout=4).status==200 else 1)"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
