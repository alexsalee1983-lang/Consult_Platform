"""项目配置快照（二期 A4 / A3，docs/接口约定.md 第四节）。

交付链、门禁、检索、提示词只读项目快照（config.compose 的产出），不读当前配置文件。
A4：快照表还没有（A3 写迁移 project_configs），所有项目按一期的 SOC、无模块现算。
A3 起改为读表，没有快照行的旧项目由迁移补成 SOC。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from ..models import Project
from ..products import config


def snapshot(db: Session, project: Project) -> dict[str, Any]:
    """项目的配置快照；配置有问题抛 config.CatalogError。"""
    return config.check_snapshot(config.compose_codes(config.DEFAULT_PRODUCT))
