# 模拟大模型服务：只用于 CI 和测试机（docker compose --profile mock-llm），不要部署到生产。
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /srv
ARG PIP_INDEX_URL=https://pypi.org/simple
COPY requirements.txt .
RUN pip install -i ${PIP_INDEX_URL} -r requirements.txt
COPY app.py .
RUN useradd -r -u 10001 app
USER app
EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
