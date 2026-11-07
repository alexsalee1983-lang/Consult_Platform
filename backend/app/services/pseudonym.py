"""自动假名化：发给模型前把登记的敏感名词换成占位符，模型回复里再换回来。

- 名词表来自项目卡（客户名、人名、系统名等，M3 交付链中建立）；一期模型为公司私有化部署，开关默认关闭。
- 以后接入云端模型时，标准档必须开启（决策记录 2026-11-07）。
- 长的名词先替换，避免“某某银行信息科技部”被“某某银行”截断。
- 流式输出时，占位符可能被拆在两段里，StreamRestorer 会把不完整的占位符留到下一段再还原。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Pseudonymizer:
    terms: dict[str, str] = field(default_factory=dict)   # 原词 → 占位符，如 {"某某城商行": "【客户】"}

    @classmethod
    def from_terms(cls, names: dict[str, str]) -> Pseudonymizer:
        """names：原词 → 类别（客户、人员、系统……）。同类多个时编号：【人员1】【人员2】。"""
        counts: dict[str, int] = {}
        mapping: dict[str, str] = {}
        for term, kind in sorted(names.items(), key=lambda kv: kv[0]):
            if not term:
                continue
            counts[kind] = counts.get(kind, 0) + 1
            mapping[term] = f"【{kind}{counts[kind]}】"
        return cls(mapping)

    def apply(self, text: str) -> str:
        for term in sorted(self.terms, key=len, reverse=True):
            text = text.replace(term, self.terms[term])
        return text

    def restore(self, text: str) -> str:
        for term, placeholder in self.terms.items():
            text = text.replace(placeholder, term)
        return text

    def stream_restorer(self) -> StreamRestorer:
        return StreamRestorer(self)


@dataclass
class StreamRestorer:
    p: Pseudonymizer
    buf: str = ""

    def feed(self, chunk: str) -> str:
        self.buf += chunk
        cut = self.buf.rfind("【")
        if cut != -1 and "】" not in self.buf[cut:]:   # 末尾有不完整的占位符，先留着
            out, self.buf = self.buf[:cut], self.buf[cut:]
        else:
            out, self.buf = self.buf, ""
        return self.p.restore(out)

    def flush(self) -> str:
        out, self.buf = self.buf, ""
        return self.p.restore(out)
