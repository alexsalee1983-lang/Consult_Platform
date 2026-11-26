"""后台任务 worker：python -m app.worker（docker compose 的 worker 服务）。

循环：领取到期任务 → 执行 → 没有任务时在 Redis 上等唤醒信号（最多 POLL_SECONDS 秒，Redis 不可用就睡眠后轮询）。
- 心跳：每轮写 Redis 键 worker:heartbeat:<编号>（30 秒过期，/api/health 据此显示 worker 是否在线），
  并更新本地文件 /tmp/worker.alive 的时间（容器健康检查用，不依赖 Redis）。
- 收到 SIGTERM / SIGINT 时做完当前任务再退出。
- 每 SCAN_SECONDS 秒扫描一次到期项目，自动发起删除申请（app/project/deletion.py）。
- 每 DAILY_SECONDS 秒（启动时先做一次）排每日自检、停用长期未登录的账号（app/services/ops.py，二期 C5）、
  排候选影子期晋级任务 kb.candidate_promote（app/kb/candidate.py，三期 B24①）；
  按日期切换依据现行状态（app/kb/reg_lifecycle.py，三期 B26）。
- 每 RECHECK_SECONDS 秒扫描一次交付件里的知识引用，标记使用反馈待复查（app/kb/recheck.py，二期 B13）。
- 每 WEEKLY_SECONDS 秒（启动时先做一次）排知识缺口池每周计划 kb.gap_week（app/kb/gap_pool.py，三期 B25②）。
- 不做数据库迁移（由 backend 启动时完成），compose 里 worker 在 backend 健康后才启动。
"""
from __future__ import annotations

import logging
import os
import signal
import socket
import time
from pathlib import Path

from .core import audit, metrics
from .core.config import get_settings
from .core.db import SessionLocal
from .kb import agent_review, batches, candidate, deid_flow, gap_manual, gap_pool, intake, reconfirm_agent, search  # noqa: F401  注册 kb.parse、kb.deid、kb.embed、kb.agent_review、kb.candidate_promote、kb.batch_* 等任务处理器
from .project import autoflow, deletion, evidence, my_tasks, quality, runs  # noqa: F401  注册 project.parse、project.delete 等
from .services import jobs, ops, runtime_config

POLL_SECONDS = 5
SCAN_SECONDS = 3600        # 到期删除扫描间隔（平台规则自动发起到期删除，M4 ②）
DAILY_SECONDS = 86400      # 每日自检、长期未登录停用（二期 C5 O5、O7）
RECHECK_SECONDS = 86400    # 使用反馈待复查扫描间隔（二期 B13，app/kb/recheck.py）
WEEKLY_SECONDS = 7 * 86400  # 知识缺口池每周计划（三期 B25②）
HEARTBEAT_TTL = 30
HEARTBEAT_PREFIX = "worker:heartbeat:"
ALIVE_FILE = Path(os.environ.get("WORKER_ALIVE_FILE", "/tmp/worker.alive"))  # nosec B108  容器内存活标记，不含数据

log = logging.getLogger("worker")
_stop = False


def worker_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}"


def _redis():
    try:
        import redis

        return redis.Redis.from_url(get_settings().redis_url, socket_connect_timeout=2, socket_timeout=POLL_SECONDS + 2)
    except Exception:  # noqa: BLE001
        return None


def heartbeat(r, wid: str) -> None:
    try:
        ALIVE_FILE.touch()
    except OSError:
        pass
    if r is not None:
        try:
            r.set(HEARTBEAT_PREFIX + wid, int(time.time()), ex=HEARTBEAT_TTL)
        except Exception:  # noqa: BLE001  Redis 暂时不可用：任务照常执行，健康检查会显示 worker 状态未知
            pass


def online_workers() -> int:
    """在线 worker 数（心跳未过期的）。Redis 不可用时抛异常，由调用方处理。"""
    import redis

    r = redis.Redis.from_url(get_settings().redis_url, socket_connect_timeout=1, socket_timeout=1)
    return sum(1 for _ in r.scan_iter(HEARTBEAT_PREFIX + "*", count=100))


def _wait(r) -> None:
    if r is not None:
        try:
            r.brpop([jobs.WAKE_KEY], timeout=POLL_SECONDS)
            return
        except Exception:  # noqa: BLE001
            pass
    time.sleep(POLL_SECONDS)


def _handle_signal(signum, frame) -> None:  # noqa: ARG001
    global _stop
    _stop = True
    log.info("收到停止信号，做完当前任务后退出")


def _scan_due() -> None:
    """平台规则：为保留期已到期的项目发起删除申请，等项目负责人确认（可重复执行，多个 worker 同时跑也不会重复发起）。"""
    try:
        with SessionLocal() as db:
            made = deletion.scan_due(db)
        if made:
            log.info("发起到期删除申请 %d 个", len(made))
    except Exception as e:  # noqa: BLE001
        log.warning("到期删除扫描出错：%s", type(e).__name__)


def _scan_reconfirm() -> None:
    """平台规则（人工环节复查 K7）：复审到期的公开知识条目排知识审核 Agent 复查（可重复执行，本周期查过的不再排）。"""
    try:
        with SessionLocal() as db:
            made = reconfirm_agent.scan_due(db)
        if made:
            log.info("复审到期，排知识审核 Agent 复查 %d 条", len(made))
    except Exception as e:  # noqa: BLE001
        log.warning("复审到期扫描出错：%s", type(e).__name__)


def _auto_flow() -> None:
    """平台规则（C5）：证据齐了自动发起交付链、写好的交付件自动评审（app/project/autoflow.py；可重复执行）。"""
    try:
        with SessionLocal() as db:
            started = autoflow.scan_runs(db)
            reviewed = autoflow.scan_reviews(db)
        if started or reviewed:
            log.info("自动发起交付链 %d 个、质量评审 %d 个", len(started), len(reviewed))
    except Exception as e:  # noqa: BLE001
        log.warning("交付流程自动推进出错：%s", type(e).__name__)


def _scan_recheck() -> None:
    """平台规则：顾问常删掉 Agent 引用的知识条目标为待复查，进抽检队列（可重复执行）。"""
    from .kb import recheck

    try:
        with SessionLocal() as db:
            made = recheck.scan(db)
        if made:
            log.info("标记待复查的知识条目 %d 条", len(made))
    except Exception as e:  # noqa: BLE001
        log.warning("待复查扫描出错：%s", type(e).__name__)


def _verify_audit() -> None:
    """每小时校验一次审计哈希链，结果只进指标（cp_audit_chain_ok），失败时告警（docs/监控.md）；不写审计，免得每小时一条。"""
    try:
        with SessionLocal() as db:
            ok = audit.verify_chain(db).ok
    except Exception as e:  # noqa: BLE001
        log.warning("审计链校验出错：%s", type(e).__name__)
        return
    metrics.AUDIT_CHAIN_OK.set(1 if ok else 0)
    metrics.AUDIT_CHAIN_VERIFIED.set_to_current_time()
    if not ok:
        log.error("审计哈希链校验未通过，请运行 python -m app.cli verify-audit 查看")


def _daily() -> None:
    """O5：排一个自检任务（已有没做完的不重复排）；O7：停用长期未登录的账号；B24①：排候选晋级任务。"""
    from sqlalchemy import select

    from .models import Job

    try:
        with SessionLocal() as db:
            pending = db.scalar(select(Job.id).where(Job.kind == "system.selfcheck",
                                                     Job.status.in_((jobs.QUEUED, jobs.RUNNING))).limit(1))
            if pending is None:
                jobs.enqueue(db, "system.selfcheck", created_by="platform", max_attempts=1)
            made = ops.disable_inactive(db)
            candidate.enqueue(db)          # 候选影子期晋级（三期 B24①），已有没做完的不重复排
        if made:
            log.info("停用长期未登录的账号 %d 个", len(made))
    except Exception as e:  # noqa: BLE001
        log.warning("每日运维任务出错：%s", type(e).__name__)
    _reg_lifecycle()


def _reg_lifecycle() -> None:
    """三期 B26：按施行日、废止日切换依据现行状态，处理替代关系（可重复执行）。"""
    from datetime import date

    from .kb import reg_lifecycle

    try:
        with SessionLocal() as db:
            r = reg_lifecycle.run(db, date.today())
        if r.changes:
            log.info("法规生命周期：状态变化 %d 条，已登记 %d 条", len(r.changes), r.counts()["applied"])
    except Exception as e:  # noqa: BLE001
        log.warning("法规生命周期任务出错：%s", type(e).__name__)


def _weekly() -> None:
    """B25②：排一个缺口池每周计划任务（已有没做完的不重复排）。"""
    from sqlalchemy import select

    from .models import Job

    try:
        with SessionLocal() as db:
            pending = db.scalar(select(Job.id).where(Job.kind == gap_pool.JOB_KIND,
                                                     Job.status.in_((jobs.QUEUED, jobs.RUNNING))).limit(1))
            if pending is None:
                jobs.enqueue(db, gap_pool.JOB_KIND, created_by="platform", max_attempts=2)
    except Exception as e:  # noqa: BLE001
        log.warning("缺口池每周计划出错：%s", type(e).__name__)


def _sync_runtime_config() -> None:
    """四期 G3②：追上页面改过的运行层配置（阈值、开关、模型绑定）。"""
    try:
        with SessionLocal() as db:
            runtime_config.sync(db)
    except Exception as e:  # noqa: BLE001
        log.warning("运行层配置同步出错：%s", type(e).__name__)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    jobs.validate_handlers()   # 签名不一致直接拒绝启动（四期 G7）
    wid = worker_id()
    r = _redis()
    log.info("worker %s 启动，任务种类：%s", wid, ", ".join(jobs.registered()))
    metrics.start_server(get_settings().metrics_port)
    last_scan = last_daily = last_recheck = last_weekly = None
    while not _stop:
        heartbeat(r, wid)
        if last_scan is None or time.monotonic() - last_scan >= SCAN_SECONDS:
            last_scan = time.monotonic()
            _sync_runtime_config()
            _scan_due()
            _scan_reconfirm()
            _auto_flow()
            _verify_audit()
        if last_daily is None or time.monotonic() - last_daily >= DAILY_SECONDS:
            last_daily = time.monotonic()
            _daily()
        if last_weekly is None or time.monotonic() - last_weekly >= WEEKLY_SECONDS:
            last_weekly = time.monotonic()
            _weekly()
        if last_recheck is None or time.monotonic() - last_recheck >= RECHECK_SECONDS:
            last_recheck = time.monotonic()
            _scan_recheck()
        try:
            with SessionLocal() as db:
                status = jobs.run_once(db, wid)
        except Exception as e:  # noqa: BLE001  数据库暂时不可用等：记类型，稍后重试
            log.warning("领取或执行任务出错：%s", type(e).__name__)
            time.sleep(POLL_SECONDS)
            continue
        if status is None:
            _wait(r)
        else:
            log.info("任务完成，状态 %s", status)
    if r is not None:
        try:
            r.delete(HEARTBEAT_PREFIX + wid)
        except Exception:  # noqa: BLE001
            pass
    log.info("worker %s 已退出", wid)


if __name__ == "__main__":
    main()
