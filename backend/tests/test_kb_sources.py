"""依据原文切条（二期 B7）：法规按条、标准只收条款目录、清单解析、切出的条目能导入，已被替代按已废止判。"""
from datetime import date

from sqlalchemy import select

from app.cli import main as cli_main
from app.core.db import SessionLocal
from app.kb import importer, sources
from app.kb.files import parse_entry
from app.models import KbItem, User

from .conftest import create_user

LAW = """中华人民共和国示范安全法
第一章 总则
第一条 为了保障示范安全，制定本法。
第二条 在境内建设、运营示范系统，适用本法。
依照第一条规定，本行为换行开头，
第一条 这一行在正文中换行开头，不是新的一条。
第二章 运行安全
第一节 一般规定
第三条 运营者应当按照等级保护制度的要求，
履行下列安全保护义务。
第三条之一 运营者应当制定应急预案。
第四条 本法自2030年1月1日起施行。
"""

STANDARD = """示范安全技术 基本要求
目次
8.1.4 安全计算环境 ........ 12
8.1 安全通用要求
8.1.4 安全计算环境
8.1.4.1 身份鉴别
应对登录的用户进行身份标识和鉴别，身份标识具有唯一性。
8.1.4.2 访问控制
应对登录的用户分配账户和权限。
"""

MANIFEST = """文件名,名称,发布机关,文号/标准号,发布日期,施行日期,现行状态,原网址,获取方式,替代文件
law.txt,中华人民共和国示范安全法,示范机关,主席令第1号,2029-06-01,2030-01-01,现行,https://example.gov.cn/1,已下载,
std.txt,示范安全技术 基本要求,示范标委,GB/T 99999-2029,2029-05-10,2029-12-01,已被替代,https://example.gov.cn/2,已下载,GB/T 99999-2031
old.txt,示范旧办法,示范机关,令第2号,2010-01-01,2010-03-01,已废止,,未公开,
"""


def test_cn_int():
    assert [sources.cn_int(x) for x in ("一", "十", "十二", "二十一", "一百零三", "两百", "7", "甲")] == \
        [1, 10, 12, 21, 103, 200, 7, 0]


def test_law_split_sequential_and_chapters():
    ps = sources.split_law([ln for ln in LAW.splitlines() if ln.strip()])
    assert [p.clause for p in ps] == ["第一条", "第二条", "第三条", "第三条之一", "第四条"]
    assert "不是新的一条" in ps[1].text                     # 正文换行开头的“第一条”不切
    assert ps[2].text == "运营者应当按照等级保护制度的要求，履行下列安全保护义务。"   # 断行拼回
    assert ps[2].context == "第二章 运行安全 第一节 一般规定"
    assert [p.key_part for p in ps] == ["1", "2", "3", "3-1", "4"]


def test_manifest_and_modes():
    docs, skipped = sources.read_manifest(MANIFEST.encode("utf-8-sig"))
    assert [d.file for d in docs] == ["law.txt", "std.txt"]
    assert "未公开" in skipped[0]
    law, std = docs
    assert (law.mode, law.status, law.effective, law.version) == ("law", "current", date(2030, 1, 1), "2029")
    assert (std.mode, std.status, std.version, std.replaced_by) == ("catalog", "superseded", "2029", "GB/T 99999-2031")


def test_catalog_has_no_body_text():
    docs, _ = sources.read_manifest(MANIFEST.encode())
    entries, rejected, note = sources.split_doc(docs[1], STANDARD.encode())
    assert not rejected and not note
    assert [e.title.split()[-2] for e in entries] == ["8.1", "8.1.4", "8.1.4.1", "8.1.4.2"]
    for e in entries:
        assert "身份标识具有唯一性" not in e.content.body and sources.CATALOG_NOTE in e.content.body
        assert (e.meta.layer, e.meta.doc_type, e.content.ref_code, e.content.ref_status) == \
            ("L4", "SRC", "GB/T 99999-2029", "superseded")


def test_guard_hit_rejected():
    d = sources.Doc(file="x.txt", title="示范办法")
    text = "第一条 正常条款。\n第二条 管理口令为 password=Abc!23456 。\n"
    entries, rejected, _ = sources.split_doc(d, text.encode())
    assert [e.content.title for e in entries] == ["示范办法 第一条"]
    assert len(rejected) == 1 and "机密级" in rejected[0][1] and "Abc" not in rejected[0][1]


def test_cli_split_then_import(tmp_path):
    src, out = tmp_path / "src", tmp_path / "out"
    src.mkdir()
    (src / "law.txt").write_text(LAW, encoding="utf-8")
    (src / "std.txt").write_text(STANDARD, encoding="utf-8")
    (tmp_path / "m.csv").write_text(MANIFEST, encoding="utf-8")
    assert cli_main(["kb-source-split", str(tmp_path / "m.csv"), "--dir", str(src), "--out", str(out)]) == 0
    files = sorted(p.name for p in out.glob("*.md"))
    assert len(files) == 9 and (out / "_report.json").is_file()
    e = parse_entry((out / files[0]).read_text(encoding="utf-8"))
    assert e.content.source_type == "public" and e.content.source_detail.startswith("公开资料：")

    name, *_ = create_user(("knowledge_expert",))
    with SessionLocal() as db:
        u = db.scalar(select(User).where(User.username == name))
        rs = importer.import_dir(db, u, out)
        assert [r.action for r in rs] == ["created"] * 9, [(r.file, r.message) for r in rs]
        art3 = db.scalar(select(KbItem).where(KbItem.import_key == sources.src_key("中华人民共和国示范安全法", "3")))
        v = art3.versions[-1]
        assert (v.ref_code, v.ref_status, v.effective_date) == ("主席令第1号", "current", date(2030, 1, 1))
        assert importer.import_dir(db, u, out)[0].action == "skipped"     # 草稿未处理


GUIDE = """示范事件分级指南
一、
特别重大示范事件
符合下列情形之一的，为特别重大示范事件：
1.示范系统大面积瘫痪。
二、
重大示范事件
（一）示范系统长时间中断。
四、这一行项号跳过三，不是新的一项。
三、对示范处理活动进行审计的，应当重点审查下列事项：
（一）是否取得示范同意。
注：本指南中的“以上”均包括本数。
"""


def test_item_split_sequential_and_headings():
    ps = sources.split_items([ln for ln in GUIDE.splitlines() if ln.strip()])
    assert [(p.clause, p.key_part, p.heading) for p in ps] == [
        ("一、", "1", "特别重大示范事件"), ("二、", "2", "重大示范事件"), ("三、", "3", "")]
    assert ps[0].text == "符合下列情形之一的，为特别重大示范事件：1.示范系统大面积瘫痪。"
    assert "不是新的一项" in ps[1].text                      # 跳号的“四、”留在正文里
    assert ps[2].text.startswith("对示范处理活动进行审计的") and ps[2].text.endswith("均包括本数。")


def test_item_mode_manifest_and_verify():
    m = ("文件名,名称,文号/标准号,原网址,切分方式\n"
         "g.txt,示范事件分级指南,示范办法附件,https://www.example.gov.cn/g,按项\n")
    (d,), _ = sources.read_manifest(m.encode())
    assert d.mode == "item"
    data = GUIDE.encode()
    entries, rejected, _ = sources.split_doc(d, data)
    assert [e.content.title for e in entries] == ["示范事件分级指南 一、 特别重大示范事件",
                                                  "示范事件分级指南 二、 重大示范事件", "示范事件分级指南 三、"]
    assert entries[0].key == sources.src_key("示范事件分级指南", "1")
    assert "continuous" in sources.verify(d, data, entries, rejected)     # 行首“四、”没切出来，转人工
    ok = GUIDE.replace("四、这一行项号跳过三，不是新的一项。\n", "").encode()
    e2, r2, _ = sources.split_doc(d, ok)
    assert "continuous" not in sources.verify(d, ok, e2, r2)


def test_law_stops_at_appendix_and_strips_pdf_page_numbers():
    d = sources.Doc(file="x.txt", title="示范等级保护管理办法")
    text = ("第一条 运营者应当开展定级、等— 5 —级测评。\n第二条 本办法自发布之日起施行。\n"
            "附件：示范定级审核表\n附件\n示范定级审核表\n第三条 表格里的说明，不是条款。\n")
    entries, rejected, _ = sources.split_doc(d, text.encode())
    assert [e.content.title for e in entries] == ["示范等级保护管理办法 第一条", "示范等级保护管理办法 第二条"]
    assert "审核表" not in entries[1].content.body
    assert sources.verify(d, text.encode(), entries, rejected) == ["official_source", "sha256", "code"]
    pdf = sources.parse.ParsedDoc(kind="pdf", sections=[sources.parse.Section(heading="", text=text)])
    assert sources._lines(pdf)[0] == "第一条 运营者应当开展定级、等级测评。"
