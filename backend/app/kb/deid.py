"""去标识（规则 + 词库）：把投料里的客户名、人名、联系方式、网络标识、编号、金额换成占位符。

依据 agents/roles/sanitizer.md 第 1 步“直接标识识别”：
- 同一份文档内同一实体用同一个占位符（【客户1】【人员2】……），保证文意连贯。
- 报告只记“类别、占位符、次数、出现在哪些章节”，**不记原文，也不记“原文 → 占位符”的对照**。
- 宁可多换，不可漏放：机构名按后缀识别，监管机构等公开名称除外；人名靠“姓氏 + 职务”和人工补充的词。
- 本模块是第一道；脱敏审查 Agent 复查漏网名称和组合推断风险（app/kb/deid_agent.py），最后人工确认。

extra_terms：复核人临时补充的词（客户简称、系统名、人名等），只在本次替换中使用，**不保存**。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# 类别 → 占位符前缀
CATEGORIES = {
    "客户名称": "客户", "人名": "人员", "联系方式": "联系方式已删除", "网络标识": "网络标识",
    "系统名称": "系统", "编号": "编号", "金额": "金额", "其他": "敏感信息",
}
NUMBERED = {"客户名称", "人名", "网络标识", "系统名称"}     # 这些类别编号，其他类别统一占位

# 机构后缀（长的在前）。“中心”“部”等太常见，不在这里，靠补充词和 Agent 复查。
_ORG_SUFFIX = ("股份有限公司", "有限责任公司", "有限公司", "农村商业银行", "城市商业银行", "农商银行", "农商行", "城商行",
               "商业银行", "银行", "信用社", "证券", "保险", "信托", "集团", "公司", "电网", "供电局", "电力局",
               "医院", "大学", "学院", "研究院", "研究所", "管理局")
# 不是客户名的常见说法（泛称、监管机构、公开组织）
_ORG_ALLOW = {
    "商业银行", "城市商业银行", "农村商业银行", "股份制银行", "国有银行", "大型银行", "中小银行", "外资银行", "政策性银行",
    "村镇银行", "民营银行", "中央银行", "开发银行", "该银行", "本银行", "某银行", "各银行", "托管公司", "安全公司",
    "咨询公司", "外包公司", "集团公司", "子公司", "母公司", "分公司", "总公司", "上市公司", "保险公司", "证券公司",
    "中国人民银行", "人民银行", "国家金融监督管理总局", "证监会", "银保监会", "公安部", "工业和信息化部",
}
_ORG_GENERIC_HEAD = ("某", "该", "本", "各", "其", "一家", "这家", "多家", "大型", "中型", "小型", "全国性", "区域性", "地方",
                     "国有", "股份制", "城市", "农村", "外资", "民营", "其他", "相关", "所属", "下属", "我")
_ORG_RE = re.compile(r"([一-龥A-Za-z]{2,12}?)(" + "|".join(_ORG_SUFFIX) + r")")
# 去掉机构名前面粘连的动词、介词等，例如“服务于某某银行”的“服务于”
_ORG_STRIP_RE = re.compile(r"^.*?(?:于|在|为|对|向|与|和|及|是|的|给|帮|到|从)(?=[一-龥A-Za-z]{2,})")

_SURNAMES = ("王李张刘陈杨黄赵吴周徐孙马朱胡郭何高林罗郑梁谢宋唐许韩冯邓曹彭曾肖田董袁潘于蒋蔡余杜叶程苏魏吕丁任沈"
             "姚卢姜崔钟谭陆汪范金石廖贾夏韦付方白邹孟熊秦邱江尹薛闫段雷侯龙史陶黎贺顾毛郝龚邵万钱严覃武戴莫孔向汤")
_TITLES = ("总经理", "董事长", "副总", "总监", "经理", "主任", "部长", "处长", "科长", "局长", "行长", "组长", "主管",
           "老师", "工程师", "总", "处", "局", "工")
# 人名前面只能是开头、标点、空白或常见虚词（的、和、请、由……），避免把“主张总体”里的“张总”当人名
_BEFORE = r"(?:(?<=^)|(?<=[\s，。；：、,.;:!?！？（(“\"'《的和与及跟同由请让向对给被找叫问是]))"
_PERSON_RE = re.compile(_BEFORE + f"([{_SURNAMES}])(?:[一-龥]{{1,2}})?(" + "|".join(_TITLES[:-4])
                        + ")(?![一-龥])")
_PERSON_SHORT_RE = re.compile(_BEFORE + f"([{_SURNAMES}])(总|处|局|工|经理|主任|行长|部长)"
                              "(?!体|结|部|计|量|数|共|之|是|监|裁|局|理|长)")

_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("联系方式", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("联系方式", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    ("联系方式", re.compile(r"(?<!\d)0\d{2,3}-?\d{7,8}(?!\d)")),
    ("网络标识", re.compile(r"https?://[^\s，。；）)]+")),
    ("网络标识", re.compile(r"(?<![\w.@])(?:[A-Za-z0-9-]+\.)+(?:com|cn|net|org|gov|edu|io|local|internal|corp)(?:\.cn)?(?![\w.])",
                         re.I)),
    ("网络标识", re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?:/\d{1,2})?(?![\d.])")),
    ("编号", re.compile(r"[一-龥]{1,8}[〔\[［(（]\d{4}[〕\]］)）]\s*\d{1,5}\s*号")),
    ("编号", re.compile(r"(?:合同|项目|采购|招标|订单|工单)(?:编号|号)[:：]?\s*[A-Za-z0-9-]{4,}")),
    ("编号", re.compile(r"(?<![A-Za-z0-9])[A-Z]{2,6}-\d{4,}(?:-\d+)*(?![A-Za-z0-9])")),
    ("金额", re.compile(r"(?:人民币|￥|¥|RMB)\s*\d[\d,]*(?:\.\d+)?\s*(?:亿|万)?元?")),
    ("金额", re.compile(r"\d[\d,]*(?:\.\d+)?\s*(?:亿元|万元|元)")),
]


@dataclass
class Term:
    term: str
    category: str


@dataclass
class Replacement:
    category: str
    placeholder: str
    count: int = 0
    sections: list[int] = field(default_factory=list)   # 出现在第几个章节（从 0 开始）

    def to_json(self) -> dict:
        return {"category": self.category, "placeholder": self.placeholder, "count": self.count,
                "sections": sorted(set(self.sections))}


@dataclass
class DeidResult:
    sections: list[tuple[str, str]]                     # (标题, 正文)，已替换
    filename: str
    replacements: list[Replacement]

    @property
    def total(self) -> int:
        return sum(r.count for r in self.replacements)


class _Mapper:
    """同一实体同一占位符。原文只存在内存里，不进入结果。"""

    def __init__(self) -> None:
        self._map: dict[tuple[str, str], Replacement] = {}
        self._counter: dict[str, int] = {}

    def placeholder(self, category: str, original: str, section: int) -> str:
        key = (category, original if category in NUMBERED else "")
        rep = self._map.get(key)
        if rep is None:
            prefix = CATEGORIES.get(category, "敏感信息")
            if category in NUMBERED:
                self._counter[category] = self._counter.get(category, 0) + 1
                ph = f"【{prefix}{self._counter[category]}】"
            else:
                ph = f"【{prefix}】"
            rep = self._map[key] = Replacement(category, ph)
        rep.count += 1
        rep.sections.append(section)
        return rep.placeholder

    def replacements(self) -> list[Replacement]:
        return list(self._map.values())


def _is_org(name: str, suffix: str) -> bool:
    full = name + suffix
    if full in _ORG_ALLOW or name.startswith(_ORG_GENERIC_HEAD) or name.endswith(("的", "和", "与", "及", "或")):
        return False
    return not any(full.endswith(a) and len(full) - len(a) <= 1 for a in _ORG_ALLOW)


def _apply(text: str, mapper: _Mapper, terms: list[Term], section: int) -> str:
    # 1. 补充词（长的在前，避免“某某银行信息科技部”被“某某银行”截断）
    for t in sorted(terms, key=lambda t: len(t.term), reverse=True):
        if t.term and t.term in text:
            text = text.replace(t.term, mapper.placeholder(t.category, t.term, section))
    # 2. 正则规则（联系方式、网络标识、编号、金额）
    for category, pattern in _PATTERNS:
        text = pattern.sub(lambda m, c=category: mapper.placeholder(c, m.group(0), section), text)
    # 3. 机构名
    def org(m: re.Match) -> str:
        name, suffix = m.group(1), m.group(2)
        # 去掉前面粘连的动词、介词等，例如“服务于某某银行”的“服务于”
        name = _ORG_STRIP_RE.sub("", name)
        if not _is_org(name, suffix):
            return m.group(0)
        prefix = m.group(1)[: len(m.group(1)) - len(name)]
        return prefix + mapper.placeholder("客户名称", name + suffix, section)
    text = _ORG_RE.sub(org, text)
    # 4. 姓氏 + 职务（张总、李处、王经理……）→ 【人员N】
    for pattern in (_PERSON_RE, _PERSON_SHORT_RE):
        text = pattern.sub(lambda m: mapper.placeholder("人名", m.group(0), section), text)
    return text


def deidentify(sections: list[tuple[str, str]], filename: str, extra_terms: list[Term] | None = None) -> DeidResult:
    terms = [t for t in (extra_terms or []) if t.term.strip()]
    for t in terms:
        if t.category not in CATEGORIES:
            raise ValueError(f"补充词类别不对：{t.category}，可选 {'、'.join(CATEGORIES)}")
    mapper = _Mapper()
    out = [(_apply(h, mapper, terms, i), _apply(body, mapper, terms, i)) for i, (h, body) in enumerate(sections)]
    stem, dot, ext = filename.rpartition(".")
    safe_name = (_apply(stem, mapper, terms, -1) + dot + ext) if dot else _apply(filename, mapper, terms, -1)
    return DeidResult(sections=out, filename=safe_name, replacements=mapper.replacements())


def residual_terms_after(result: DeidResult, terms: list[str]) -> list[str]:
    """检查补充词替换后是否还有残留（例如大小写不同的写法），返回仍出现的词。只在内存中使用，不保存。"""
    joined = "\n".join(h + "\n" + b for h, b in result.sections) + "\n" + result.filename
    return [t for t in terms if t and t in joined]
