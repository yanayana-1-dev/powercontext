"""seekmem 库表初始化：文档表 + 分块表（向量索引 + IK 全文索引）。"""
from . import db

DDL_DATABASE = "CREATE DATABASE IF NOT EXISTS {db}"

DDL_DOCUMENTS = """
CREATE TABLE IF NOT EXISTS {db}.sm_documents (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  title VARCHAR(512) NOT NULL,
  source VARCHAR(512) NOT NULL DEFAULT '',
  tags VARCHAR(512) NOT NULL DEFAULT '',
  n_chunks INT NOT NULL DEFAULT 0,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

DDL_CHUNKS = """
CREATE TABLE IF NOT EXISTS {db}.sm_chunks (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  doc_id BIGINT NOT NULL,
  seq INT NOT NULL DEFAULT 0,
  doc_title VARCHAR(512) NOT NULL DEFAULT '',
  source VARCHAR(512) NOT NULL DEFAULT '',
  tags VARCHAR(512) NOT NULL DEFAULT '',
  heading VARCHAR(512) NOT NULL DEFAULT '',
  content TEXT NOT NULL,
  embedding VECTOR({dim}),
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  VECTOR INDEX v_emb (embedding) WITH (distance=L2, type=hnsw),
  FULLTEXT INDEX f_content (content) WITH PARSER IK
)
"""

DDL_USERS = """
CREATE TABLE IF NOT EXISTS {db}.sm_users (
  user_key VARCHAR(128) PRIMARY KEY,
  display_name VARCHAR(128) NOT NULL DEFAULT '',
  scope_id VARCHAR(128) NOT NULL DEFAULT '',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""


def ensure_schema() -> None:
    db.execute(DDL_DATABASE.format(db=config_db()), channel="schema")
    db.execute(DDL_DOCUMENTS.format(db=config_db()), channel="schema")
    from . import config
    db.execute(DDL_CHUNKS.format(db=config_db(), dim=config.EMBED_DIM), channel="schema")
    db.execute(DDL_USERS.format(db=config_db()), channel="schema")


def config_db() -> str:
    from . import config
    return config.SEEKDB_DATABASE
