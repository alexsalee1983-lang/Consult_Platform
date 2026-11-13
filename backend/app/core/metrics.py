"""运行指标（M5 ⑤，docs/监控.md）：Prometheus 格式，供内网自建的 Prometheus 抓取。

只记计数、耗时和状态，不记任何业务内容：标签只有接口路由模板（如 /api/projects/{code}，不是实际路径）、
请求方法、状态码、任务种类、上游服务种类和结果。不带项目代号、用户名、文件名。

指标单独开一个端口（METRICS_PORT，容器里 9100），只在 docker 内部网络上，不映射到宿主机；
业务端口上没有 /metrics，登录检查和权限矩阵不受影响。METRICS_PORT=0（默认，单元测试）不开端口。
"""
from __future__ import annotations

import logging
import time
from collections.abc import Iterator

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, PlatformCollector, ProcessCollector
from prometheus_client import start_http_server as _start
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import Collector

log = logging.getLogger(__name__)

REGISTRY = CollectorRegistry()
ProcessCollector(registry=REGISTRY)
PlatformCollector(registry=REGISTRY)

_LATENCY_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 120)

HTTP_REQUESTS = Counter("cp_http_requests_total", "接口请求数", ["method", "route", "status"], registry=REGISTRY)
HTTP_LATENCY = Histogram("cp_http_request_duration_seconds", "接口耗时（流式接口含整个输出过程）",
                         ["method", "route"], buckets=_LATENCY_BUCKETS, registry=REGISTRY)
UPSTREAM_REQUESTS = Counter("cp_upstream_requests_total", "经网关的模型、向量、重排调用（含重试后的最终结果）",
                            ["service", "outcome"], registry=REGISTRY)
UPSTREAM_LATENCY = Histogram("cp_upstream_request_duration_seconds", "经网关的上游调用耗时（到响应头，含重试）",
                             ["service"], buckets=_LATENCY_BUCKETS, registry=REGISTRY)
JOBS = Counter("cp_jobs_total", "后台任务执行次数", ["kind", "status"], registry=REGISTRY)
JOB_LATENCY = Histogram("cp_job_duration_seconds", "后台任务执行耗时", ["kind"],
                        buckets=(0.1, 0.5, 1, 5, 15, 30, 60, 120, 300, 600, 1800), registry=REGISTRY)
AUDIT_WRITE_FAILURES = Counter("cp_audit_write_failures_total", "写审计失败次数（业务改动已生效、审计没写上）",
                               registry=REGISTRY)
GUARD_BLOCKS = Counter("cp_guard_blocks_total", "机密级检测命中次数（按规则编号，不含命中内容）", ["kind"],
                       registry=REGISTRY)
AUDIT_CHAIN_OK = Gauge("cp_audit_chain_ok", "worker 最近一次校验审计哈希链的结果（1 完整，0 有问题）",
                       registry=REGISTRY)
AUDIT_CHAIN_VERIFIED = Gauge("cp_audit_chain_verified_timestamp_seconds", "worker 最近一次校验审计哈希链的时间",
                             registry=REGISTRY)
LOGINS = Counter("cp_auth_logins_total", "登录结果（allowed 成功 / blocked 失败 / locked 失败并锁定）",
                 ["result"], registry=REGISTRY)


def upstream_service(url: str) -> str:
    """按地址末段判断上游服务种类，不把地址写进标签。"""
    tail = url.rstrip("/").rsplit("/", 1)[-1]
    return {"completions": "llm", "embeddings": "embed", "rerank": "rerank"}.get(tail, "other")


class _StateCollector(Collector):
    """抓取时才查的状态量：任务队列积压、在线 worker 数。只在后端进程注册。"""

    def collect(self) -> Iterator[GaugeMetricFamily]:
        from ..services import jobs
        from ..worker import online_workers
        from .db import SessionLocal

        queue = GaugeMetricFamily("cp_jobs_by_status", "各状态的后台任务数", labels=["kind", "status"])
        up = GaugeMetricFamily("cp_state_up", "状态量查询是否成功（1 成功）", labels=["source"])
        try:
            with SessionLocal() as db:
                for kind, by_status in jobs.summary(db).items():
                    for status, n in by_status.items():
                        queue.add_metric([kind, status], n)
            up.add_metric(["database"], 1)
        except Exception as e:  # noqa: BLE001  数据库不可用时照样输出其他指标
            log.warning("指标查询任务队列失败：%s", type(e).__name__)
            up.add_metric(["database"], 0)
        yield queue
        workers = GaugeMetricFamily("cp_workers_online", "心跳未过期的 worker 数")
        try:
            workers.add_metric([], online_workers())
            up.add_metric(["redis"], 1)
            yield workers
        except Exception as e:  # noqa: BLE001
            log.warning("指标查询 worker 心跳失败：%s", type(e).__name__)
            up.add_metric(["redis"], 0)
        yield up


_state_registered = False


def register_state_collector() -> None:
    global _state_registered
    if not _state_registered:
        REGISTRY.register(_StateCollector())
        _state_registered = True


def start_server(port: int) -> None:
    """开指标端口（0 不开）。端口被占用只记日志，不影响业务。"""
    if port <= 0:
        return
    try:
        _start(port, addr="0.0.0.0", registry=REGISTRY)  # nosec B104  只在 docker 内部网络，compose 不映射此端口
        log.info("指标端口 %s 已开启", port)
    except OSError as e:
        log.warning("指标端口 %s 开启失败：%s", port, type(e).__name__)


class MetricsMiddleware:
    """ASGI 中间件：按路由模板记请求数和耗时。没匹配到路由的统一记 unmatched，避免把任意路径写进标签。"""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        start = time.perf_counter()
        status = 500

        async def _send(message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, _send)
        finally:
            route = getattr(scope.get("route"), "path", None) or "unmatched"
            method = scope.get("method", "")
            HTTP_REQUESTS.labels(method, route, str(status)).inc()
            HTTP_LATENCY.labels(method, route).observe(time.perf_counter() - start)
