"""统一配置登记（四期 G3①，docs/4A架构设计.md 5.6 配置三层）。

把锁定层（.env / 代码）、方法层（仓库里的产品、模块、行业包、Agent md）、运行层（模型绑定、门槛、开关、阈值）
汇成一个只读视图，每层和总体各出一个哈希，用来回答“这次运行时生效的是哪套配置”。
密钥、口令只显示“已配置/未配置”，地址只留协议、主机和端口；方法层文件只记内容哈希，不带正文。
运行层改动的理由、审计、回滚与二次确认见 runtime_config（G3②）；页面改过的键 source 显示“页面”。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..agents import registry
from ..core.config import get_settings
from ..products.config import catalog_dir

ENV, REPO, PAGE = ".env", "仓库", "页面"

# 锁定层：不上页面，改动走运维变更流程
LOCKED_FLAGS = ("app_env", "llm_external", "rerank_external", "llm_pseudonymize", "session_backend", "cookie_secure",
                "login_max_failures", "login_lock_minutes", "password_min_length", "password_max_age_days",
                "session_idle_minutes", "session_absolute_hours")
LOCKED_SECRETS = ("llm_api_key", "embed_api_key", "rerank_api_key", "minio_secret_key", "totp_keys",
                  "bootstrap_admin_password")
LOCKED_URLS = ("llm_base_url", "embed_base_url", "rerank_base_url")

# 运行层：值 → 是否须负责人二次确认（切模型、开自动发布、调高自主度）
RUNTIME: dict[str, bool] = {
    "llm_model_reasoning": True, "llm_model_standard": True, "llm_model_light": True, "llm_model_tools": True,
    "embed_model": True, "rerank_model": True, "llm_model_catalog": True,
    "kb_src_auto_publish": True, "kb_agent_auto_publish": True, "auto_flow": True, "auto_submit_deliverables": True,
    "kb_vector_min_similarity": False, "kb_basis_min_similarity": False, "kb_basis_min_rerank": False,
    "kb_search_min_rerank": False, "kb_rerank_candidates": False, "kb_citation_gate": False,
    "kb_single_reviewer_public": False, "kb_sample_rate_src": False, "kb_sample_rate_src_partial": False,
    "kb_sample_fail_max": False, "kb_sample_rate_agent": False, "kb_candidate_shadow": False,
    "kb_candidate_days": False, "kb_eval_min_accuracy": False, "kb_eval_gate": False,
    "auto_run_quiet_hours": False, "auto_review_idle_hours": False, "judgment_capture": False,
    "llm_tool_calling": False, "llm_tool_max_rounds": False, "llm_tool_max_calls": False,
    "agent_history_chars": False, "backup_keep_days": False, "inactive_account_days": False,
}

METHOD_DIRS = ("products", "modules", "industries")


def _sha(data: Any) -> str:
    raw = json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _endpoint(url: str) -> str:
    if not url:
        return ""
    u = urlsplit(url)
    return f"{u.scheme}://{u.hostname or ''}{f':{u.port}' if u.port else ''}"


def _locked() -> list[dict]:
    s = get_settings()
    items = [{"key": k, "value": getattr(s, k), "source": ENV} for k in LOCKED_FLAGS]
    items += [{"key": k, "value": "已配置" if getattr(s, k) else "未配置", "source": ENV} for k in LOCKED_SECRETS]
    items += [{"key": k, "value": _endpoint(getattr(s, k)), "source": ENV} for k in LOCKED_URLS]
    return items


def _file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def _method() -> list[dict]:
    items: list[dict] = []
    base = registry.agents_dir()
    common = base / registry.COMMON_FILE
    if common.exists():
        items.append({"key": f"agents/{registry.COMMON_FILE}", "value": _file_sha(common), "source": REPO})
    for role in sorted((base / "roles").glob("*.md")):
        doc = registry.load(role.stem)
        if doc is not None:
            items.append({"key": f"agent:{doc.code}", "value": doc.version, "md_version": doc.md_version,
                          "level": doc.level, "source": REPO})
    root = catalog_dir()
    for d in METHOD_DIRS:
        for f in sorted((root / d).glob("*.yaml")):
            items.append({"key": f"{d}/{f.name}", "value": _file_sha(f), "source": REPO})
    return items


def _runtime() -> list[dict]:
    from . import runtime_config   # 避免循环导入

    s = get_settings()
    over = runtime_config.overridden_keys()
    return [{"key": k, "value": getattr(s, k), "source": PAGE if k in over else ENV, "confirm": c}
            for k, c in RUNTIME.items()]


LAYERS = (("locked", "锁定层", _locked), ("method", "方法层", _method), ("runtime", "运行层", _runtime))


def build() -> dict:
    """三层登记视图：layers[].sha 是该层哈希，sha 是三层合起来的生效配置哈希。"""
    layers = []
    for key, name, fn in LAYERS:
        items = fn()
        layers.append({"key": key, "name": name, "sha": _sha(items), "items": items})
    return {"sha": _sha({x["key"]: x["sha"] for x in layers}), "layers": layers}


def effective_sha() -> str:
    """当前生效配置哈希（三层合计），供运行记录、回放比对使用。"""
    return build()["sha"]
