"""输出包与阶段包导出（四期 A33，docs/交付链输入输出方案.md 第六节、8.1）。

- 组件清单读产品配置 `outputs:`（C22 起草，规则见 tests/test_product_outputs.py），按交付件所在阶段分组给输出页；
  已签署的交付件标 signed，输出页给只读查看入口（非项目经理也可看、不可改）。
- 阶段包 = 该阶段每个交付件的单件导出（Word；组件里有 PPT 的另出 PPT）打成 zip，附“清单.txt”写导出了哪些、跳过哪些及原因。
  单件导出的规则全部沿用（未签署“草稿”水印、页脚导出编号、质量事件冻结、对外硬规则、严格档占位符），每件照常记各自审计；
  阶段包另记一条 outputs.stage_export，只记阶段、件数、大小和摘要哈希，不记正文。
"""
from __future__ import annotations

import hashlib
import io
import zipfile
from collections.abc import Callable
from typing import Any

from ..agents.knowledge import product_code
from ..products import config as product_config

COMPONENT_KEYS = ("id", "name", "kind", "format", "gen", "to", "default", "client_signs", "truth", "per")


def _product(service: str) -> product_config.Product | None:
    try:
        return product_config.product(product_code(service))
    except product_config.CatalogError:
        return None


def _components(raw: list[Any] | None) -> list[dict[str, Any]]:
    out = []
    for c in raw or []:
        if isinstance(c, dict):
            out.append({k: c.get(k, False if k in ("client_signs", "truth") else None) for k in COMPONENT_KEYS})
    return out


def packs(project: Any) -> dict[str, Any]:
    """输出页数据：阶段（按产品配置顺序）→ 交付件 → 组件；外加结项复盘组件。"""
    prod = _product(project.service)
    outputs = (prod.raw.get("outputs") if prod else None) or {}
    stage_of = {d.code: d.stage for d in prod.deliverables} if prod else {}
    stages: dict[str, list[dict[str, Any]]] = {}
    for d in sorted(project.deliverables, key=lambda x: x.code):
        st = stage_of.get(d.code, "其他")
        stages.setdefault(st, []).append({
            "code": d.code, "title": d.title, "stage": st, "status": d.status, "signed": d.status == "signed",
            "revision": d.revision, "components": _components(outputs.get(d.code))})
    return {"stages": [{"stage": k, "deliverables": v} for k, v in stages.items()],
            "closeout": _components(outputs.get("closeout"))}


def stage_codes(project: Any, stage: str) -> list[str]:
    for st in packs(project)["stages"]:
        if st["stage"] == stage:
            return [d["code"] for d in st["deliverables"]]
    return []


def stage_zip(project: Any, stage: str, codes: list[str],
              export_one: Callable[[str, str], tuple[str, bytes] | str]) -> tuple[bytes, dict[str, Any]]:
    """export_one(交付件编号, 格式) → (文件名, 内容) 或跳过原因。返回 zip 与审计字段。"""
    p = packs(project)
    formats = {d["code"]: {c["format"] for c in d["components"]} for st in p["stages"] for d in st["deliverables"]}
    buf = io.BytesIO()
    lines = [f"{project.code} · {stage} 阶段包", ""]
    exported = skipped = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for code in codes:
            for fmt in ["docx"] + (["pptx"] if "pptx" in formats.get(code, set()) else []):
                r = export_one(code, fmt)
                if isinstance(r, str):
                    skipped += 1
                    lines.append(f"跳过 {code}（{fmt}）：{r}")
                else:
                    exported += 1
                    z.writestr(r[0], r[1])
                    lines.append(f"导出 {code}（{fmt}）：{r[0]}")
        z.writestr("清单.txt", "\n".join(lines) + "\n")
    data = buf.getvalue()
    return data, {"stage": stage, "exported": exported, "skipped": skipped, "size": len(data),
                  "file_sha": hashlib.sha256(data).hexdigest()[:16]}
