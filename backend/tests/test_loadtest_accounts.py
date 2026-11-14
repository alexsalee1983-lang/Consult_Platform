"""压测账号命令：只在开发、测试环境；凭据只写文件（600），重跑重置口令，--disable 停用。"""
import json
import os

import pytest
from sqlalchemy import select

from app.cli import main as cli_main
from app.core.config import get_settings
from app.core.db import SessionLocal
from app.core.permissions import membership, roles_of
from app.core.security import verify_password
from app.models import AuditLog, Project, User


def _users(db):
    return list(db.scalars(select(User).where(User.username.startswith("lt-")).order_by(User.username)))


def test_loadtest_accounts_create_reset_disable(tmp_path, capsys):
    out = tmp_path / "lt.json"
    assert cli_main(["loadtest-accounts", "--count", "3", "--project", "LOAD-Q", "--out", str(out)]) == 0
    printed = capsys.readouterr().out
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["project"] == "LOAD-Q" and [u["username"] for u in data["users"]] == ["lt-01", "lt-02", "lt-03"]
    for u in data["users"]:
        assert u["password"] not in printed and u["totp_secret"] not in printed
    if os.name == "posix":
        assert (out.stat().st_mode & 0o777) == 0o600
    with SessionLocal() as db:
        p = db.scalar(select(Project).where(Project.code == "LOAD-Q"))
        assert p.tier == "standard" and len(p.deliverables) == 8
        users = {u.username: u for u in _users(db)}
        for i, u in enumerate(data["users"]):
            row = users[u["username"]]
            assert verify_password(row.password_hash, u["password"]) and row.totp_secret == u["totp_secret"]
            assert roles_of(db, row.id) == {"consultant"}
            assert membership(db, p.id, row.id).project_role == ("manager" if i == 0 else "member")
        row = db.scalars(select(AuditLog).where(AuditLog.action == "loadtest.accounts")
                         .order_by(AuditLog.id.desc())).first()
        assert '"count": 3' in row.detail and data["users"][0]["password"] not in row.detail

    # 重跑：口令和动态码密钥重置，成员关系不重复
    assert cli_main(["loadtest-accounts", "--count", "3", "--project", "LOAD-Q", "--out", str(out)]) == 0
    data2 = json.loads(out.read_text(encoding="utf-8"))
    assert data2["users"][0]["password"] != data["users"][0]["password"]

    assert cli_main(["loadtest-accounts", "--disable"]) == 0
    with SessionLocal() as db:
        assert all(not u.is_active for u in _users(db))


def test_loadtest_accounts_refused_in_prod(tmp_path, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "app_env", "prod")
    out = tmp_path / "lt.json"
    assert cli_main(["loadtest-accounts", "--out", str(out)]) == 1
    assert not out.exists()


@pytest.mark.parametrize("argv", [["--count", "0"], ["--count", "51"], ["--project", "某工厂"], []])
def test_loadtest_accounts_bad_args(tmp_path, argv):
    out = [] if argv == [] else ["--out", str(tmp_path / "x.json")]
    assert cli_main(["loadtest-accounts", *argv, *out]) == 1
