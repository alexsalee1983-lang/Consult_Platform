"""A15：前端编辑器的来源标签正则与后端正文解析一致（前后端共用 frontend/src/editor/refSamples.json）。"""
import json
from pathlib import Path

import pytest

from app.project.deliverables import _EVIDENCE_REF, _INFERRED, _KB_REF

SAMPLES = Path(__file__).resolve().parents[2] / "frontend" / "src" / "editor" / "refSamples.json"


@pytest.mark.skipif(not SAMPLES.exists(), reason="没有前端目录（只装了后端）")
def test_backend_regex_matches_shared_samples() -> None:
    data = json.loads(SAMPLES.read_text(encoding="utf-8"))
    assert data["samples"]
    for s in data["samples"]:
        text = s["text"]
        assert [m.group(0) for m in _EVIDENCE_REF.finditer(text)] == s["ev"], text
        assert [m.group(0) for m in _KB_REF.finditer(text)] == s["kb"], text
        assert [m.group(0) for m in _INFERRED.finditer(text)] == s["inf"], text
        if "kb_refs" in s:
            assert [m.group(1) for m in _KB_REF.finditer(text)] == s["kb_refs"], text
