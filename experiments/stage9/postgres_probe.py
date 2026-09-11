"""现有本机 pgvector 的隔离 schema 探针；完成后仅清理本探针创建的 schema。"""

import argparse
import json
import os
from uuid import uuid4

import psycopg
from langgraph.store.postgres import PostgresStore
from psycopg import sql
from psycopg.conninfo import make_conninfo

from tests.stage9.test_memory import CountingEmbeddings


def run(dsn: str) -> dict:
    """验证原生 PostgreSQL Store 的索引、直接读取、删除与连接重建。"""
    schema = "stage9_probe_" + uuid4().hex
    embeddings = CountingEmbeddings()
    index = {"dims": 3, "embed": embeddings, "fields": ["content"]}
    with psycopg.connect(dsn, autocommit=True) as connection:
        assert connection.execute("SELECT 1 FROM pg_extension WHERE extname='vector'").fetchone()
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    private = make_conninfo(dsn, options=f"-csearch_path={schema},public")
    namespace = ("financeclaw", "v2", "probe", "owner")
    try:
        with PostgresStore.from_conn_string(private, index=index) as store:
            store.setup()
            with psycopg.connect(private, autocommit=True) as connection:
                assert connection.execute("SELECT current_schema()").fetchone()[0] == schema
                tables = connection.execute(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema=%s",
                    (schema,),
                ).fetchall()
                assert {"store", "store_vectors"} <= {row[0] for row in tables}
            store.put((*namespace, "profile"), "language", {"content": "zh-CN"}, index=False)
            assert store.get((*namespace, "profile"), "language").value["content"] == "zh-CN"
            assert embeddings.documents == embeddings.queries == 0
            store.put(
                (*namespace, "events"),
                "goal",
                {"content": "三年后购房", "status": "active"},
                index=["content"],
            )
            assert embeddings.documents == 1
            assert store.search((*namespace, "events"), query="购房")[0].key == "goal"
            assert embeddings.documents + embeddings.queries == 2
            store.put(
                (*namespace, "events"),
                "goal",
                {"content": "三年后购房", "status": "revoked"},
                index=False,
            )
            assert embeddings.documents + embeddings.queries == 2
            assert not store.search(
                (*namespace, "events"), query="购房", filter={"status": "active"}
            )
            with psycopg.connect(private, autocommit=True) as connection:
                # 当前版本 index=False 只停止新编码，旧向量仍在；正常召回须带状态过滤。
                assert connection.execute("SELECT count(*) FROM store_vectors").fetchone()[0] == 1
            store.put((*namespace, "events"), "goal", {"content": "新购房计划"}, index=["content"])
        with PostgresStore.from_conn_string(private, index=index) as restarted:
            assert restarted.get((*namespace, "profile"), "language") is not None
            restarted.delete((*namespace, "events"), "goal")
            assert restarted.get((*namespace, "events"), "goal") is None
            assert not restarted.search((*namespace, "events"), query="购房")
        with psycopg.connect(private, autocommit=True) as connection:
            vectors = connection.execute("SELECT count(*) FROM store_vectors").fetchone()[0]
            assert vectors == 0
        return {
            "passed": True,
            "profile_embedding_calls": 0,
            "indexed_documents": 2,
            "query_encodings": 3,
            "embedding_methods": {"documents": embeddings.documents, "query": embeddings.queries},
            "connection_reconstruction": True,
            "delete_removes_vectors": True,
            "index_false_removes_vectors": False,
            "inactive_filtered_from_semantic_search": True,
            "semantic_quality_verified": False,
            "isolated_schema_cleaned": True,
        }
    finally:
        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    result = run(os.environ["FINANCECLAW_TEST_POSTGRES_DSN"])
    from pathlib import Path

    Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False))
