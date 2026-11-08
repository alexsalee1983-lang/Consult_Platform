"""数据表。新增表在这里导出，Alembic 通过 Base.metadata 看到全部表结构。

表结构变更只通过 backend/alembic/versions 下的迁移脚本，不再自动建表。
"""
from .audit import AuditLog
from .project import Confirmation, Deliverable, Project

__all__ = ["AuditLog", "Confirmation", "Deliverable", "Project"]
