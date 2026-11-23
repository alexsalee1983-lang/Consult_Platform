"""模型目录 v1（三期 G2，4A 架构 5.3）：哪些模型可用、各属哪个家族和档位，各角色的首选与有序备选。

- 配置：LLM_MODEL_CATALOG 留空 = 不用目录，网关照旧按 LLM_MODEL_REASONING / STANDARD / LIGHT 单模型调用；
  填文件路径（YAML 或 JSON）或直接填一段 JSON。格式见 deploy/model_catalog.example.yaml。v1 不做页面（G3）。
- 模型：id（模型平台上的模型 ID）、family（deepseek / glm / kimi / minimax / qwen ……）、tier（reasoning / standard /
  light，可写列表）、evaluated（是否通过评测，默认 true；false 的不参与选择）。所有模型走同一个 LLM_BASE_URL。
- 角色：generate.reasoning / generate.standard / generate.light / review（评审、知识审核）/ arbitrate（仲裁），
  值为模型 ID 的有序列表（首选在前）。不写时按 4A 5.3 的家族顺序排：生成 DeepSeek→GLM→Kimi→MiniMax，
  评审 GLM→Kimi→MiniMax→DeepSeek，仲裁 Kimi→MiniMax→GLM；同顺序下与调用档位相同的模型优先。
  列表之外的已评测模型排在最后兜底（首选都不可用时仍能取到下一个可用的）。
- 异构校验：评审、知识审核的家族必须与生成方不同，仲裁与两方都不同（plan() 过滤）；
  目录里没有满足要求的家族时降级为同家族运行并标记 degraded，不阻断（调用方据此提高抽检）。
"""
from __future__ import annotations

import json
import os
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

from ..core.config import get_settings

LEVELS = ("reasoning", "standard", "light")
ROLES = ("generate.reasoning", "generate.standard", "generate.light", "review", "arbitrate")
DEFAULT_ORDER = {
    "generate": ("deepseek", "glm", "kimi", "minimax"),
    "review": ("glm", "kimi", "minimax", "deepseek"),
    "arbitrate": ("kimi", "minimax", "glm"),
}
# 没登记在目录里的模型（如目录启用前的运行记录）按模型 ID 里的关键词认家族
_KEYWORDS = (("deepseek", "deepseek"), ("chatglm", "glm"), ("glm", "glm"), ("zhipu", "glm"), ("kimi", "kimi"),
             ("moonshot", "kimi"), ("minimax", "minimax"), ("abab", "minimax"), ("qwen", "qwen"))


class CatalogError(ValueError):
    """目录配置写错了（启动时拒绝，见 core/bootstrap.py）。"""


@dataclass(frozen=True)
class ModelEntry:
    id: str
    family: str
    tiers: tuple[str, ...]
    evaluated: bool = True


@dataclass(frozen=True)
class Catalog:
    models: tuple[ModelEntry, ...]
    roles: dict[str, tuple[str, ...]]

    def get(self, model_id: str) -> ModelEntry | None:
        return next((m for m in self.models if m.id == model_id), None)


@dataclass(frozen=True)
class Plan:
    models: tuple[ModelEntry, ...]      # 按序尝试
    degraded: bool                      # 评审 / 仲裁找不到不同家族，同家族降级运行


def parse(data: object) -> Catalog:
    if not isinstance(data, dict) or not isinstance(data.get("models"), list) or not data["models"]:
        raise CatalogError("模型目录至少要有一个模型（models 列表）")
    models: list[ModelEntry] = []
    for raw in data["models"]:
        if not isinstance(raw, dict) or not str(raw.get("id") or "").strip() or not str(raw.get("family") or "").strip():
            raise CatalogError("模型目录里每个模型都要写 id 和 family")
        tiers = raw.get("tier", "standard")
        tiers = tuple(tiers if isinstance(tiers, list) else [tiers])
        if not tiers or any(t not in LEVELS for t in tiers):
            raise CatalogError(f"模型 {raw['id']} 的 tier 只能是 {'、'.join(LEVELS)}")
        mid = str(raw["id"]).strip()
        if any(m.id == mid for m in models):
            raise CatalogError(f"模型目录里 {mid} 重复")
        models.append(ModelEntry(mid, str(raw["family"]).strip().lower(), tiers, raw.get("evaluated", True) is not False))
    roles: dict[str, tuple[str, ...]] = {}
    ids = {m.id for m in models}
    for role, lst in (data.get("roles") or {}).items():
        if role not in ROLES:
            raise CatalogError(f"模型目录的角色只能是 {'、'.join(ROLES)}，不认识 {role}")
        if not isinstance(lst, list) or any(str(x) not in ids for x in lst):
            raise CatalogError(f"角色 {role} 引用了目录里没有的模型")
        roles[role] = tuple(str(x) for x in lst)
    return Catalog(tuple(models), roles)


@lru_cache(maxsize=8)
def _load(raw: str, mtime: float) -> Catalog:
    try:
        if raw.lstrip().startswith("{"):
            data = json.loads(raw)
        else:
            data = yaml.safe_load(Path(raw).read_text(encoding="utf-8"))
    except (OSError, ValueError, yaml.YAMLError) as e:
        raise CatalogError(f"模型目录读不出来：{type(e).__name__}") from e
    return parse(data)


def load(raw: str | None = None) -> Catalog | None:
    """未配置返回 None；配置写错抛 CatalogError。文件改了（修改时间变化）自动重新读。"""
    raw = (get_settings().llm_model_catalog if raw is None else raw).strip()
    if not raw:
        return None
    mtime = 0.0
    if not raw.startswith("{"):
        try:
            mtime = os.path.getmtime(raw)
        except OSError as e:
            raise CatalogError("模型目录文件不存在") from e
    return _load(raw, mtime)


def family_of(model_id: str, cat: Catalog | None = None) -> str:
    """模型 ID → 家族：先查目录，查不到按 ID 里的关键词认；都认不出返回空串。"""
    if not model_id:
        return ""
    try:
        cat = cat or load()
    except CatalogError:
        cat = None
    entry = cat.get(model_id) if cat else None
    if entry:
        return entry.family
    low = model_id.lower()
    return next((fam for key, fam in _KEYWORDS if key in low), "")


def families_of(model_ids: Iterable[str], cat: Catalog | None = None) -> set[str]:
    return {f for f in (family_of(m, cat) for m in model_ids) if f}


def candidates(cat: Catalog, role: str, level: str) -> list[ModelEntry]:
    """某角色在某档位下按序可用（已通过评测）的模型。"""
    kind = "generate" if role == "generate" else role
    key = f"generate.{level}" if kind == "generate" else kind
    usable = [m for m in cat.models if m.evaluated]
    if key in cat.roles:
        listed = [e for e in (cat.get(i) for i in cat.roles[key]) if e and e.evaluated]
    else:
        order = DEFAULT_ORDER[kind]
        listed = sorted((m for m in usable if m.family in order),
                        key=lambda m: (level not in m.tiers, order.index(m.family), cat.models.index(m)))
    rest = sorted((m for m in usable if m not in listed), key=lambda m: (level not in m.tiers, cat.models.index(m)))
    return listed + rest


def plan(cat: Catalog, role: str, level: str, exclude_families: Iterable[str] = ()) -> Plan:
    """按角色排出尝试顺序。评审 / 仲裁过滤掉 exclude_families；没给时评审按生成首选的家族排除，
    仲裁按生成首选和评审首选的家族排除（平台强制，调用方忘了传也不会同家族评审自己）。"""
    cands = candidates(cat, role, level)
    if role == "generate":
        return Plan(tuple(cands), False)
    exclude = set(exclude_families)
    if not exclude:
        gen = candidates(cat, "generate", level)
        exclude = {gen[0].family} if gen else set()
        if role == "arbitrate":
            rev = [m for m in candidates(cat, "review", level) if m.family not in exclude]
            exclude |= {rev[0].family} if rev else set()
    ok = [m for m in cands if m.family not in exclude]
    if ok:
        return Plan(tuple(ok), False)
    return Plan(tuple(cands), True)
