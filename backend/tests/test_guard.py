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
