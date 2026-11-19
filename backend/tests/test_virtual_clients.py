"""二期 D 线 ⑤：虚拟客户素材能进平台——每个文件过机密级检测、不含资质边界用语，四个新产品都有纪要和资料。"""
from pathlib import Path

import pytest

from app.project import quality
from app.services import guard

ROOT = Path(__file__).resolve().parents[2] / "knowledge" / "virtual-clients"
FILES = sorted(p for p in ROOT.glob("*/*") if p.suffix in (".md", ".txt"))


@pytest.mark.parametrize("path", FILES, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_material_passes_guard(path: Path):
    text = path.read_text(encoding="utf-8")
    assert not guard.check(text).blocked, guard.check(text)
    assert quality.qualification_hits(text) == 0


@pytest.mark.parametrize("product", ["ire", "dsm", "iso", "nsg"])
def test_each_product_has_notes_and_documents(product: str):
    names = [p.name for p in (ROOT / product).iterdir()]
    assert any(n.startswith("notes-") for n in names) and any(n.startswith("doc-") for n in names)
