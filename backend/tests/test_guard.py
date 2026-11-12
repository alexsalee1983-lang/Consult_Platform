from app.services.guard import check, mask


def kinds(text):
    return {f.kind for f in check(text).findings}


def test_clean_consulting_text_passes():
    text = "SOC 现有 6 人，全部为白班，夜间无人值守。一级事件 30 分钟内启动处置。依据 L4-OPS-012。"
    r = check(text)
    assert not r.blocked
    assert r.summary() == "未发现机密级内容"


def test_credentials():
    assert "credential" in kinds("堡垒机 admin 密码：Abc@12345")
    assert "credential" in kinds("password=P@ssw0rd!")
    assert "credential" in kinds("api_key: 8f3e2a9b77")


def test_private_and_cloud_keys():
    assert "private_key" in kinds("-----BEGIN RSA PRIVATE KEY-----\nMIIE...")
    assert "cloud_key" in kinds("AKIAABCDEFGHIJKLMNOP")
    assert "cloud_key" in kinds("token sk-abcdefghijklmnopqrstuvwxyz123")


def test_ip_and_cidr():
    assert "ip" in kinds("核心交换机 10.1.20.1")
    r = check("办公网 192.168.0.0/16")
    assert r.findings[0].label == "IP 网段规划"


def test_version_number_is_not_ip():
    assert "ip" not in kinds("升级到 2.300.1.4 版本")


def test_standard_clause_number_is_not_ip():
    for text in (
        "依据 GB/T 22239-2019 第 8.1.4.1 条",
        "GB/T 22239-2019 8.1.4.1 身份鉴别",
        "GB/T22239 8.1.4.1",
        "JR/T 0071-2020 6.2.3.1 访问控制",
        "ISO/IEC 27001:2022 5.1.2.3",
        "见 8.1.4.1 条",
    ):
        assert not check(text).blocked, text
    for text in (                                                     # 真 IP 不能因为附近有标准号或“条”字被放过
        "核心交换机 10.1.20.1",
        "依据标准把 10.1.20.1 改成",
        "GB/T 22239 要求 10.1.20.1",
        "第 8.1.4.1 条要求，核心交换机 10.1.20.1",
        "10.1.20.1 节点",
        "10.1.20.1 条目",
        "网段 第 10.0.0.0/8",
    ):
        assert "ip" in kinds(text), text


def test_cn_id_checksum():
    assert "cn_id" in kinds("身份证 11010519491231002X")
    assert "cn_id" not in kinds("编号 110105194912310021")  # 校验位不对


def test_bank_card_luhn():
    assert "bank_card" in kinds("卡号 6222021234567890128")
    assert "bank_card" not in kinds("流水号 6222021234567890123")


def test_mobile_and_email():
    assert "mobile" in kinds("联系人 13812345678")
    assert "email" in kinds("发到 zhang.san@bank.com")


def test_excerpt_is_masked():
    r = check("密码：Abc@12345")
    assert "Abc@12345" not in r.findings[0].excerpt
    assert mask("abcdefgh") == "ab****gh"


def test_credentials_without_separator():
    # 关键词后只有空白、没有冒号等分隔符
    assert "credential" in kinds("管理口令 Abc@12345")
    assert "credential" in kinds("password Abc@12345")
    assert "credential" in kinds("数据库 root 密码  P@ssw0rd!")
    assert "credential" in kinds("Secret 9f8e7d6c5b")
    assert "credential" in kinds("密钥 abc123，下周轮换")


def test_bare_credential_excerpt_is_masked_and_not_duplicated():
    r = check("管理口令 Abc@12345")
    creds = [f for f in r.findings if f.kind == "credential"]
    assert len(creds) == 1
    assert "Abc@12345" not in creds[0].excerpt
    assert creds[0].excerpt == mask("Abc@12345")
    # 带分隔符的写法只报一次
    assert len([f for f in check("password =P@ssw0rd!").findings if f.kind == "credential"]) == 1


def test_credential_prose_is_not_blocked():
    for text in (
        "口令策略需要整改",
        "密码复杂度要求为 8 位以上",
        "修改默认口令",
        "管理口令 定期更换",
        "口令 8 位以上",
        "password policy review",
        "token 有效期 30 分钟",
        "password 123456",  # 只有一类字符，不按口令处理
    ):
        assert "credential" not in kinds(text), text


def test_bare_password_without_keyword():
    # #36 测试发现漏拦：不带“口令”“密码”字样的口令
    for text in (
        "用 Abc@12345 登录",
        "使用Qwe!2024ok登录堡垒机",
        "login with P@ssw0rd1",
        "admin/Abc@12345",
        "默认账号 root:123456 未修改",
        "弱口令 admin/admin123",
        "sa：Sql2019#db",
        "交换机统一用 P@ssw0rd 管理",
        "备份口径见附件，Zx#9876ab 为临时值",
    ):
        assert "credential" in kinds(text), text


def test_bare_password_excerpt_is_masked_and_reported_once():
    r = check("admin/Abc@12345，用 Abc@12345 登录")
    creds = [f for f in r.findings if f.kind == "credential"]
    assert len(creds) == 2  # 两处各报一次，不因三条规则重复报
    assert all("Abc@12345" not in f.excerpt for f in creds)
    # 带关键词的写法仍只报一次
    assert len([f for f in check("管理口令 P@ssw0rd!").findings if f.kind == "credential"]) == 1


def test_bare_password_rules_do_not_block_ordinary_text():
    for text in (
        "依据 GB/T 22239-2019 第 8.1.4.1 条",
        "GB/T22239-2019、JR/T 0071-2020",
        "升级到 Windows10-Pro、iOS-17.2、AES-256-GCM",
        "镜像 redis:7-alpine、mysql:5.7.44、postgres:16",
        "接口 /api/admin/agent-grants 与 /api/admin/users/x/roles",
        "admin/root 等默认账号应禁用",
        "使用 SSL-VPN 登录",
        "使用 Windows10 登录",
        "TCP/IP、I/O、A/B 测试、8:30-17:30 值班",
        "LLM_MODEL=deepseek-v4.1-flash",
        "https://example.com/a?Id=1&b=X2",
        "C++ 与 C# 项目，R&D 部门",
        "等保 2.0 三级，SOC 7x24 值守",
        "user:test 这类说明",
    ):
        assert not check(text).blocked, text                          # 整句放行，不只是不报口令（测试 #38 漏了 IP 误报）
    assert "credential" not in kinds("发到 zhang.san@bank.com")       # 邮箱另有规则拦截，这里只看口令规则
