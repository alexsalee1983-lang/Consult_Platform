"""立项与项目配置快照（二期 A3，docs/接口约定.md 第四节）。

- 立项（命令行 create-project、网页 POST /api/projects）只经 create()：选一个 ready 的产品、勾 0 个或多个它允许的
  模块、必须定档、指定首位项目经理；平台把合并后的完整配置（config.compose）存进 project_configs，
  交付件按快照建（不再固定 D1～D8）。快照立项后不变，二期不提供“升级到新配置”。
- 交付链、门禁、检索、提示词只读快照（snapshot()），不读当前配置文件。
- 一期已有项目没有快照行：后端启动时按当前 products/soc.yaml、无模块补建（backfill，与 v1.0 行为一致，
  A2 测试逐字核对），补建进审计。迁移 0032 只建表，不依赖配置文件能否读出。
- 审计只记代号、档位、产品、模块和版本，不记行业以外的项目信息。
"""
from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..core import audit
from ..core.permissions import BUSINESS_ROLES, roles_of
from ..models import Deliverable, Project, ProjectConfig, ProjectMember, User
from ..products import config

PROJECT_CODE = re.compile(r"^[A-Z]{2,8}-[A-Z0-9]{1,6}$")
TIERS = {"standard": "标准档", "strict": "严格档"}


class CreateError(ValueError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _row(db: Session, project: Project) -> ProjectConfig | None:
    return db.scalar(select(ProjectConfig).where(ProjectConfig.project_id == project.id))


def _store(db: Session, project: Project, snap: dict[str, Any], by: str) -> ProjectConfig:
    row = ProjectConfig(project_id=project.id, product=snap["product"], modules=[m["code"] for m in snap["modules"]],
                        product_version=snap["product_version"],
                        module_versions={m["code"]: m["version"] for m in snap["modules"]}, snapshot=snap,
                        created_by=by)
    db.add(row)
    return row


def backfill(db: Session) -> int:
    """后端启动时：没有快照的项目（一期项目、演示数据）按 SOC、无模块补建，返回补建个数。

    配置有问题时不补（日志和 catalog-check 会报），读快照时临时按当前配置现算，修好后下次启动补上。
    """
    missing = db.scalars(select(Project).where(~Project.id.in_(select(ProjectConfig.project_id)))).all()
    if not missing:
        return 0
    try:
        snap = config.compose_codes(config.DEFAULT_PRODUCT)
    except config.CatalogError:
        return 0
    for p in missing:
        _store(db, p, snap, "platform")
    try:
        db.commit()
    except IntegrityError:                              # 两个后端进程同时启动：另一个已补
        db.rollback()
        return 0
    audit.record(db, actor="platform", action="project.config_backfill", result="allowed", count=len(missing),
                 product=snap["product"], version=snap["product_version"])
    return len(missing)


def snapshot(db: Session, project: Project) -> dict[str, Any]:
    """项目的配置快照。还没补建快照的项目按当前 SOC、无模块现算（不写库）。配置有问题抛 config.CatalogError。"""
    row = _row(db, project)
    if row is None:
        return config.compose_codes(config.DEFAULT_PRODUCT)
    return config.check_snapshot(row.snapshot)


def info(db: Session, project: Project) -> dict[str, Any]:
    """项目接口返回体里的产品信息；配置坏了不影响看项目。"""
    try:
        snap = snapshot(db, project)
    except config.CatalogError:
        return {"product": "", "product_name": "", "modules": [], "product_version": ""}
    return {"product": snap["product"], "product_name": snap["product_name"],
            "modules": [{"code": m["code"], "name": m["name"]} for m in snap["modules"]],
            "product_version": snap["product_version"]}


def options() -> list[dict[str, Any]]:
    """立项可选的产品和模块（只列 ready 的；配置有问题时不能立项，返回空）。"""
    cat = config.catalog()
    if cat.problems:
        return []
    out = []
    for p in sorted(cat.products.values(), key=lambda x: x.code):
        if p.status != "ready":
            continue
        mods = [cat.modules[c] for c in p.modules if c in cat.modules and cat.modules[c].status == "ready"]
        out.append({"code": p.code, "name": p.name, "version": p.version,
                    "deliverables": [{"code": d.code, "title": d.title} for d in p.deliverables],
                    "modules": [{"code": m.code, "name": m.name} for m in mods]})
    return out


def create(db: Session, *, actor: str, code: str, tier: str, manager: User | None, product: str,
           modules: list[str] | tuple[str, ...] = (), industry: str = "", weeks: int = 12,
           service: str = "") -> Project:
    """立项。成功时已提交；失败抛 CreateError，不写库。"""
    code = code.strip().upper()
    if not PROJECT_CODE.match(code):
        raise CreateError(422, "项目代号格式为“字母-字母或数字”，如 BANK-D、GRID-B2；只用客户代号，不写客户真实名称")
    if tier not in TIERS:
        raise CreateError(422, "必须定档：standard 标准档或 strict 严格档")
    if not 1 <= weeks <= 104:
        raise CreateError(422, "计划周数应在 1～104 之间")
    if db.scalar(select(Project.id).where(Project.code == code)) is not None:
        raise CreateError(409, f"项目 {code} 已存在（代号不复用，已删除的项目也占用代号）")
    if manager is None or not manager.is_active or not roles_of(db, manager.id) & BUSINESS_ROLES:
        raise CreateError(422, "项目经理必须是启用状态、持业务角色（顾问、项目负责人、知识专家、知识管理员）的用户")
    try:
        snap = config.compose_codes(product.strip().upper(), [m.strip().upper() for m in modules])
    except config.CatalogError as e:
        raise CreateError(422, f"不能按这个产品立项：{e}") from e
    p = Project(code=code, service=(service.strip() or snap["product_name"])[:64], industry=industry.strip()[:32],
                tier=tier, total_weeks=weeks)
    p.deliverables = [Deliverable(code=d["code"], title=d["title"], status="not_started")
                      for d in snap["deliverables"]]
    db.add(p)
    db.flush()
    _store(db, p, snap, actor)
    db.add(ProjectMember(project_id=p.id, user_id=manager.id, project_role="manager", added_by=actor))
    db.commit()
    audit.record(db, actor=actor, action="project.create", result="allowed", project_code=code, tier=tier,
                 manager=manager.username, product=snap["product"], modules=[m["code"] for m in snap["modules"]],
                 version=snap["product_version"])
    return p
