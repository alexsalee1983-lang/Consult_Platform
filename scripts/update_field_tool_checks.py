"""把平台的摘要卡规则（cards.field_checks()）写进现场工具 field-tool/field-tool.html 的 DEFAULT_CHECKS。

改了 app/project/cards.py 或 app/kb/deid.py 的规则后运行（在 backend 目录下）：
    python ../scripts/update_field_tool_checks.py
tests/test_field_tool.py 核对两边一致。
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from app.project.cards import field_checks  # noqa: E402

HTML = ROOT / "field-tool" / "field-tool.html"
MARK = re.compile(r"(/\*CHECKS-BEGIN\*/const DEFAULT_CHECKS = ).*?(;/\*CHECKS-END\*/)", re.S)


def main() -> None:
    text = HTML.read_text(encoding="utf-8")
    data = json.dumps(field_checks(), ensure_ascii=False, sort_keys=True)
    new, n = MARK.subn(lambda m: m.group(1) + data + m.group(2), text)
    if n != 1:
        raise SystemExit("没找到 DEFAULT_CHECKS 标记")
    HTML.write_text(new, encoding="utf-8", newline="\n")
    print("已更新" if new != text else "无变化")


if __name__ == "__main__":
    main()
