// 知识编号的识别模式（三期 B15）：与后端 backend/app/kb/refs.py 的 REF_PATTERN 逐字一致（tests/test_kb_refs.py 校验），
// 别处不再写死编号正则。旧格式 L2-OPS-SOC-TPL-007；新格式 KB-000123（B15 第三步换编号后才出现，先预留）。
export const KB_REF_PATTERN = String.raw`(?:L[1-5](?:-[A-Z0-9]{2,8}){3,4}-\d{3,5}|KB-\d{6})`;
