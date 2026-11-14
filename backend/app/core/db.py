from collections.abc import Iterator
from datetime import datetime, timezone

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class KbBase(Base):
    """知识区的表（kb_ 开头）都继承它，会话据此把读写发往知识区数据库（M5 ③ 分库）。
    原生 SQL 查知识区表时要带 bind_arguments=KB_BIND，否则会发到项目区所在的主库。"""
    __abstract__ = True


def _engine(url: str) -> Engine:
    if url.startswith("sqlite"):
        return create_engine(url, pool_pre_ping=True, connect_args={"check_same_thread": False})
    # 同步接口在 40 个线程的线程池里执行；默认连接池 5 + 10 时线程排队等连接，压测 10 人时响应超时（#78）。
    # 上限 40 与线程池一致；实际连接数随并发增减，空闲时只保留 10 个
    return create_engine(url, pool_pre_ping=True, pool_size=10, max_overflow=30, pool_timeout=30)


_settings = get_settings()
engine = _engine(_settings.database_url)
# 知识区数据库：没配就与主库共用同一个连接池（同库分表）
kb_engine = _engine(_settings.kb_database_url) if _settings.kb_database_url else engine
KB_SPLIT = kb_engine is not engine
KB_BIND = {"bind": kb_engine}
# 一个会话可以同时读写两个库；提交时两个库分别提交（不是两阶段提交），跨库写入的代码要容忍只成功一半
SessionLocal = sessionmaker(bind=engine, binds={KbBase: kb_engine}, autoflush=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
