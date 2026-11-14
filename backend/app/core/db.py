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
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    return create_engine(url, pool_pre_ping=True, connect_args=connect_args)


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
