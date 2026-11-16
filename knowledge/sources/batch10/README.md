# batch10：海外优先国家的个人数据保护法（内容线）

负责人 11-16 定了海外优先国家：埃及、科特迪瓦、塞内加尔、南非、安哥拉、埃塞俄比亚、尼日利亚。本批先收 4 国，都按“外文按条”切分（依赖 #151）。

| 国家 | 法律 | 条数 | 来源 |
|---|---|---|---|
| 南非 | Protection of Personal Information Act 4 of 2013（POPIA） | 115 | gov.za 政府公报 PDF |
| 尼日利亚 | Nigeria Data Protection Act 2023（Act No. 37） | 66 | 尼日利亚通信委员会 ncc.gov.ng 上的政府公报扫描件，带文字层 |
| 塞内加尔 | Loi n° 2008-12 sur la protection des données à caractère personnel | 77（退回 1） | 塞内加尔政府门户 vie-publique.sn |
| 埃塞俄比亚 | Personal Data Protection Proclamation No. 1321/2024 | 70 | 埃塞俄比亚通信管理局网站 eca.et 链接的联邦公报 PDF |

这几份都是各国官方文本，可以收原文。

## 需要人工留意
- **原网址白名单**：这 4 国的域名都不在 `OFFICIAL_HOSTS` 里，核验会转人工审核。测试机要自动发布的话，可以在 `.env` 设 `KB_SOURCE_HOSTS=gov.za,gov.ng,ncc.gov.ng,vie-publique.sn`。白名单加哪些，由负责人决定。
- **南非**：原件 PDF 加了权限密码（空口令可打开），平台解析器拒收加密 PDF，所以入库的是用 pypdf 解除加密后另存的副本。manifest 的 sha256 是这份副本的，“网页原件sha256”记的是原件。
- **埃塞俄比亚**：公报是阿姆哈拉语和英语两栏对照。入库的是 txt，只抽了英文片段、去掉了页眉；“网页原件sha256”是原 PDF。个别单词中间夹了空格，例如“Pro clamation”，审核时对照原件。原网址是该局网站链接的 IP 地址。
- **塞内加尔**：第 43 条正文提到口令，被机密级检测拦下，转人工审核。新法草案还在立法程序中，季度核对时留意。

## 还没收
- **埃及**：Law No. 151 of 2020 和 2025 年的实施条例。官方阿拉伯文原件还没找到；律所的英译本有版权，不收。
- **科特迪瓦**：Loi n° 2013-450。只找到扫描件，没有文字层，本机也没有 OCR。
- **安哥拉**：Lei n.º 22/11。只在 lex.ao 等第三方法规库找到，官方《共和国公报》要付费。
