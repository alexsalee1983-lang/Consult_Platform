import os
import sys
from pathlib import Path

# 测试用 SQLite，不依赖 Postgres；不配置模型地址，走演示回复。
_db_file = Path(__file__).parent / "test.db"
if _db_file.exists():
    _db_file.unlink()
os.environ["DATABASE_URL"] = f"sqlite:///{_db_file}"
os.environ["LLM_BASE_URL"] = ""
os.environ["REDIS_URL"] = "redis://127.0.0.1:1/0"
os.environ["MINIO_ENDPOINT"] = "127.0.0.1:1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
