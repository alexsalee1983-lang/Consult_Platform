import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.exceptions import RequestValidationError
from sqlalchemy.exc import DataError

from .api import (admin, agents, assist, audit, auth, cards, chat, conversations, deletions, deliverables, directory, evidence, expert_bench, health, indicators, portfolio,
                  tasks, workbench, kb, kb_batches, kb_feedback, kb_items, kb_sources, projects, quality, quality_incidents, readiness, runs, survey)
from .api import panorama   # 四期 A25①
from .api import agent_activity   # 四期 A29
from .api import revision_mining as revision_mining_api   # 三期 F4
from .api import revision_features as revision_features_api   # 四期 F4②
from .api import extraction as extraction_api   # 四期 F4③
from .api import agent_md as agent_md_api   # 四期 F3②e
from .api import stages as stages_api   # 四期 A27
from .api import customizations as customizations_api   # 四期 A30
from .api import consultant_aids   # 四期 A32
from .core import audit as core_audit
from .core.auth import authenticate
from .core.bootstrap import check_db_accounts, check_settings, encrypt_plaintext_rows, ensure_initial_users, ensure_test_memberships
from .core.config import get_settings
from .core.db import SessionLocal, engine, kb_engine
from .core.errors import out_of_range_handler, reject_control_chars, validation_error_handler
from .core.metrics import MetricsMiddleware, register_state_collector, start_server
from .core.migrate import upgrade_to_head
from .services import jobs, runtime_config, storage
from .project import configs as project_configs
from .seed import seed, seed_signed_sample

_settings = get_settings()
check_settings(_settings)  # 配置不安全时直接拒绝启动


@asynccontextmanager
async def lifespan(app: FastAPI):
    jobs.validate_handlers()   # 处理器签名不一致直接拒绝启动（四期 G7）
    check_db_accounts(_settings, [engine, kb_engine])  # 生产不用超级用户连库，迁移之前检查
    upgrade_to_head()
    with SessionLocal() as db:
        encrypt_plaintext_rows(db)
        ensure_initial_users(db, _settings)
        if _settings.seed_demo_data:
            seed(db)
            seed_signed_sample(db)
        ensure_test_memberships(db, _settings)
        project_configs.backfill(db)
        runtime_config.sync(db)   # 四期 G3②：页面改过的运行层配置覆盖 .env
    if storage.enabled():
        try:
            storage.ensure_buckets()
        except Exception as e:  # noqa: BLE001  对象存储暂时不可用不阻止启动，健康检查会显示 down，首次使用时再建桶
            logging.getLogger(__name__).warning("对象存储建桶失败：%s", type(e).__name__)
    register_state_collector()
    start_server(_settings.metrics_port)
    yield


# 默认所有接口都要登录（authenticate 内有公开接口白名单），各路由再按角色和项目成员检查；
# 生产环境不开放接口文档
app = FastAPI(
    title="安全咨询 Agent 平台", version="0.1.0", lifespan=lifespan,
    dependencies=[Depends(authenticate), Depends(reject_control_chars)],
    docs_url=None if _settings.is_prod else "/docs",
    redoc_url=None,
    openapi_url=None if _settings.is_prod else "/openapi.json",
)
# 422 不回显请求原文（口令、客户原文），见 core/errors.py
app.add_exception_handler(RequestValidationError, validation_error_handler)
# 超出数据库列范围的输入按 422 处理（渗透测试 M5 ⑦）
app.add_exception_handler(OverflowError, out_of_range_handler)
app.add_exception_handler(DataError, out_of_range_handler)
# 审计记录来源地址（core/audit.py）
app.add_middleware(core_audit.ClientIPMiddleware)
app.add_middleware(MetricsMiddleware)  # 只记路由模板、方法、状态码和耗时（core/metrics.py）
app.include_router(health.router)
app.include_router(auth.router)
app.include_router(projects.router)
app.include_router(portfolio.router)
app.include_router(indicators.router)
app.include_router(workbench.router)
app.include_router(panorama.router)
app.include_router(agent_activity.router)
app.include_router(deletions.router)
app.include_router(evidence.router)
app.include_router(cards.router)
app.include_router(deliverables.router)
app.include_router(assist.router)
app.include_router(runs.router)
app.include_router(quality.router)
app.include_router(quality.batch_router)   # 四期 A26 D3 批量签
app.include_router(revision_mining_api.router)
app.include_router(revision_features_api.router)
app.include_router(extraction_api.router)
app.include_router(chat.router)
app.include_router(audit.router)
app.include_router(admin.router)
app.include_router(agents.router)
app.include_router(conversations.router)
app.include_router(directory.router)
app.include_router(kb.router)
app.include_router(kb_items.router)
app.include_router(kb_batches.router)
app.include_router(readiness.router)
app.include_router(kb_feedback.router)
app.include_router(quality_incidents.router)
app.include_router(agent_md_api.router)
app.include_router(kb_sources.router)
app.include_router(tasks.router)
app.include_router(expert_bench.router)
app.include_router(survey.router)
app.include_router(stages_api.router)
app.include_router(customizations_api.router)
app.include_router(consultant_aids.router)
