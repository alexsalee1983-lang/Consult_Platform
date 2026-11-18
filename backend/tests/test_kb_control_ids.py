"""知识条目挂控制项（4A 架构设计 v4.0 §7.2 埋点 control_ids，知识区迁移 k0008）：

条目级可选字段，同 tags：条目文件 control_ids 可写，规整（去空白、去重、转大写），格式不对拒收；
重复导入直接更新、不进版本内容、不走审核；接口返回；搬迁带上。控制项中枢 v1（五期）之前只记不用。"""
import secrets

import pytest
from sqlalchemy import select

from app.core.db import SessionLocal
from app.kb import importer
from app.kb.files import format_entry, parse_entry
from app.kb.items import ItemMeta, check_control_ids
from app.kb.meta import MetaError
from app.models import KbItem, User

from .conftest import create_user
from .test_kb_import_eval import _entry


def test_entry_control_ids_normalized_and_roundtrip():
    e = parse_entry(_entry(key="ctl-entry", control_ids="[soc-mon-01, SOC-MON-01,  ir.02 ]"))
    e.meta.check()
    assert e.meta.control_ids == ["SOC-MON-01", "IR.02"]
    assert parse_entry(format_entry(e)).meta.control_ids == ["SOC-MON-01", "IR.02"]


@pytest.mark.parametrize("bad", [["有中文"], ["A B"], ["X" * 33], [f"C-{i}" for i in range(21)]])
def test_bad_control_ids_rejected(bad):
    with pytest.raises(MetaError, match="控制项"):
        check_control_ids(bad)


def test_default_empty():
    m = ItemMeta(layer="L2", domain="OPS", product="SOC", doc_type="MTD", stage="设计", capabilities=["运营"])
    m.check()
    assert m.control_ids == []


def test_reimport_updates_control_ids_without_new_version():
    name, *_ = create_user(("knowledge_expert",))
    key = f"ctl-{secrets.token_hex(3)}"
    with SessionLocal() as db:
        u = db.scalar(select(User).where(User.username == name))
        assert importer.import_entry(db, u, parse_entry(_entry(key=key, control_ids="[SOC-MON-01]"))).action == "created"
        item = db.scalar(select(KbItem).where(KbItem.import_key == key))
        assert item.control_ids == ["SOC-MON-01"]
        item.versions[-1].status = "published"
        db.commit()
        r = importer.import_entry(db, u, parse_entry(_entry(key=key, control_ids="[SOC-MON-02]")))
        db.refresh(item)
        assert item.control_ids == ["SOC-MON-02"] and r.action == "unchanged" and "控制项" in r.message
        assert len(item.versions) == 1
