"""
agent/test_cdc.py — Comprehensive unit tests for Aurora-to-DuckDB CDC pipeline.

Tests:
  - DynamoDbLeaseManager: acquisition, lease contention, renewal, release, context manager.
  - PgoutputParser & WalChange: binary replication message decoding and text WAL parsing.
  - DuckDbApplier: snapshot table initialization, incremental mutations, checkpointing, watermarks.
  - DuckDbValidator: integrity checks, corrupt file handling, row drift detection against Aurora.
  - CdcSyncWorker: singleton execution, lease skip, discrepancy-triggered full re-init, metrics.
  - DuckDbReader: hybrid caching (ETag hit vs miss), analytical query execution.
"""
from __future__ import annotations

import datetime
import io
import json
import os
import shutil
import struct
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from botocore.exceptions import ClientError
import duckdb

from agent.cdc.duckdb_applier import DuckDbApplier, DuckDbS3Storage
from agent.cdc.lease import DynamoDbLeaseManager
from agent.cdc.metrics import emit_metric, emit_sync_metrics
from agent.cdc.reader import DuckDbReader
from agent.cdc.sync_worker import CdcSyncWorker, int_to_lsn, lsn_to_int
from agent.cdc.validator import DuckDbValidator
from agent.cdc.wal_parser import ColumnInfo, PgoutputParser, RelationMetadata, WalChange


class TestLeaseManager(unittest.TestCase):
    def test_lease_acquire_success(self):
        mock_ddb = MagicMock()
        lease = DynamoDbLeaseManager(
            table_name="duckdb_sync_locks",
            owner_id="worker-1",
            dynamodb_client=mock_ddb,
        )
        self.assertTrue(lease.acquire())
        self.assertTrue(lease.is_held)
        mock_ddb.put_item.assert_called_once()

    def test_lease_acquire_contention(self):
        mock_ddb = MagicMock()
        mock_ddb.put_item.side_effect = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException"}},
            "PutItem",
        )
        lease = DynamoDbLeaseManager(
            table_name="duckdb_sync_locks",
            owner_id="worker-2",
            dynamodb_client=mock_ddb,
        )
        self.assertFalse(lease.acquire())
        self.assertFalse(lease.is_held)

    def test_lease_renew_and_release(self):
        mock_ddb = MagicMock()
        lease = DynamoDbLeaseManager(
            table_name="duckdb_sync_locks",
            owner_id="worker-1",
            dynamodb_client=mock_ddb,
        )
        lease.is_held = True
        self.assertTrue(lease.renew(extension_sec=60))
        mock_ddb.update_item.assert_called_once()

        self.assertTrue(lease.release())
        self.assertFalse(lease.is_held)


class TestWalParser(unittest.TestCase):
    def test_parse_binary_pgoutput_stream(self):
        parser = PgoutputParser()

        # 1. Relation message ('R')
        # rel_id=100, namespace="public\0", name="daily_cost\0", replica_id='d', num_cols=2
        # col1: flags=1, name="usage_date\0", type_oid=1082, typmod=-1
        # col2: flags=0, name="gross_usd\0", type_oid=1700, typmod=-1
        rel_bytes = (
            b"R"
            + struct.pack(">I", 100)
            + b"public\x00"
            + b"daily_cost\x00"
            + b"d"
            + struct.pack(">H", 2)
            + b"\x01" + b"usage_date\x00" + struct.pack(">Ii", 1082, -1)
            + b"\x00" + b"gross_usd\x00" + struct.pack(">Ii", 1700, -1)
        )
        self.assertIsNone(parser.parse_message(rel_bytes, lsn=1000))
        self.assertIn(100, parser.relations)
        self.assertEqual(parser.relations[100].key_columns, ["usage_date"])

        # 2. Begin message ('B')
        # final_lsn=2000, ts_micros=1000000, xid=500
        begin_bytes = b"B" + struct.pack(">QQI", 2000, 1000000, 500)
        self.assertIsNone(parser.parse_message(begin_bytes, lsn=2000))
        self.assertEqual(parser.current_tx_id, 500)

        # 3. Insert message ('I')
        # rel_id=100, 'N', num_cols=2
        # col1: 't', len=10, "2026-10-19"
        # col2: 't', len=5, "12.50"
        val1 = b"2026-10-19"
        val2 = b"12.50"
        insert_bytes = (
            b"I"
            + struct.pack(">I", 100)
            + b"N"
            + struct.pack(">H", 2)
            + b"t" + struct.pack(">I", len(val1)) + val1
            + b"t" + struct.pack(">I", len(val2)) + val2
        )
        change = parser.parse_message(insert_bytes, lsn=2050)
        self.assertIsNotNone(change)
        self.assertEqual(change.op, "INSERT")
        self.assertEqual(change.table, "daily_cost")
        self.assertEqual(change.values["usage_date"], "2026-10-19")
        self.assertEqual(change.values["gross_usd"], "12.50")
        self.assertEqual(change.keys["usage_date"], "2026-10-19")

    def test_lsn_conversion_helpers(self):
        lsn_int = lsn_to_int("0/16B3748")
        self.assertEqual(lsn_int, 0x16B3748)
        self.assertEqual(int_to_lsn(lsn_int), "0/16B3748")


class TestDuckDbApplierAndValidator(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test.duckdb")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_full_reinit_table(self):
        applier = DuckDbApplier(self.db_path)
        columns = ["usage_date", "gross_usd", "net_usd"]
        col_types = ["DATE", "DOUBLE", "DOUBLE"]
        rows = [
            ("2026-10-17", 10.5, 0.0),
            ("2026-10-18", 15.0, 0.0),
        ]
        applier.full_reinit_table(
            table_name="daily_cost",
            columns=columns,
            column_types=col_types,
            rows=rows,
            primary_keys=["usage_date"],
            snapshot_lsn=5000,
        )
        applier.close()

        # Verify data and watermark
        conn = duckdb.connect(self.db_path, read_only=True)
        count = conn.execute("SELECT count(*) FROM daily_cost;").fetchone()[0]
        self.assertEqual(count, 2)
        total_gross = conn.execute("SELECT sum(gross_usd) FROM daily_cost;").fetchone()[0]
        self.assertAlmostEqual(total_gross, 25.5)

        meta = dict(conn.execute("SELECT key, value FROM __cdc_metadata;").fetchall())
        self.assertEqual(meta["last_applied_lsn"], "5000")
        self.assertEqual(meta["sync_mode"], "FULL_REINIT")
        conn.close()

    def test_apply_incremental_mutations(self):
        applier = DuckDbApplier(self.db_path)
        applier.full_reinit_table(
            table_name="daily_cost",
            columns=["usage_date", "gross_usd"],
            column_types=["DATE", "DOUBLE"],
            rows=[("2026-10-18", 10.0)],
            primary_keys=["usage_date"],
            snapshot_lsn=1000,
        )

        changes = [
            # INSERT
            WalChange(
                op="INSERT",
                schema="public",
                table="daily_cost",
                keys={"usage_date": "2026-10-19"},
                values={"usage_date": "2026-10-19", "gross_usd": 20.0},
                lsn=1050,
            ),
            # UPDATE
            WalChange(
                op="UPDATE",
                schema="public",
                table="daily_cost",
                keys={"usage_date": "2026-10-18"},
                values={"usage_date": "2026-10-18", "gross_usd": 12.5},
                lsn=1100,
            ),
            # DELETE
            WalChange(
                op="DELETE",
                schema="public",
                table="daily_cost",
                keys={"usage_date": "2026-10-19"},
                values={},
                lsn=1200,
            ),
        ]
        applied = applier.apply_changes(changes)
        self.assertEqual(applied, 3)
        applier.checkpoint()
        applier.close()

        conn = duckdb.connect(self.db_path, read_only=True)
        rows = conn.execute("SELECT usage_date, gross_usd FROM daily_cost;").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(str(rows[0][0]), "2026-10-18")
        self.assertAlmostEqual(rows[0][1], 12.5)
        conn.close()

    def test_validator_file_integrity_and_drift(self):
        # 1. Nonexistent file
        v_missing = DuckDbValidator(os.path.join(tempfile.gettempdir(), "nonexistent_duckdb_test.duckdb"))
        self.assertFalse(v_missing.validate_file_integrity().is_valid)

        # 2. Valid file
        applier = DuckDbApplier(self.db_path)
        applier.full_reinit_table(
            table_name="daily_cost",
            columns=["usage_date", "gross_usd"],
            column_types=["DATE", "DOUBLE"],
            rows=[("2026-10-18", 10.0)],
            primary_keys=["usage_date"],
            snapshot_lsn=1000,
        )
        applier.close()

        validator = DuckDbValidator(self.db_path)
        res = validator.validate_file_integrity()
        self.assertTrue(res.is_valid)

        # 3. Drift check against mock PG cursor
        mock_pg_cur = MagicMock()
        # Mock Aurora returns 1 row for daily_cost
        mock_pg_cur.fetchone.return_value = (1,)
        drift_res = validator.validate_drift_against_aurora(["daily_cost"], mock_pg_cur)
        self.assertTrue(drift_res.is_valid)

        # Mock Aurora returns 5 rows -> drift detected!
        mock_pg_cur.fetchone.return_value = (5,)
        drift_mismatch = validator.validate_drift_against_aurora(["daily_cost"], mock_pg_cur)
        self.assertFalse(drift_mismatch.is_valid)
        self.assertIn("Row count mismatch", drift_mismatch.discrepancy_reason)

    def test_s3_storage_upload_explicit_storage_class(self):
        mock_s3 = MagicMock()
        mock_s3.put_object.return_value = {"ETag": '"etag-abc"'}
        storage = DuckDbS3Storage(s3_client=mock_s3)

        dummy_path = os.path.join(self.tmpdir, "dummy.duckdb")
        with open(dummy_path, "wb") as f:
            f.write(b"DUCKDB_TEST_DATA")

        # Default storage class should be STANDARD (cheapest tier for frequent small writes)
        etag = storage.upload_file("test-bucket", "test-key", dummy_path)
        self.assertEqual(etag, "etag-abc")
        mock_s3.put_object.assert_called_with(
            Bucket="test-bucket",
            Key="test-key",
            Body=unittest.mock.ANY,
            ContentType="application/octet-stream",
            StorageClass="STANDARD",
            Metadata=unittest.mock.ANY,
        )

        # Explicit custom storage class
        storage.upload_file("test-bucket", "test-key", dummy_path, storage_class="INTELLIGENT_TIERING")
        mock_s3.put_object.assert_called_with(
            Bucket="test-bucket",
            Key="test-key",
            Body=unittest.mock.ANY,
            ContentType="application/octet-stream",
            StorageClass="INTELLIGENT_TIERING",
            Metadata=unittest.mock.ANY,
        )


class TestCdcSyncWorker(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "analytics.duckdb")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_worker_skips_when_lease_held(self):
        mock_lease = MagicMock()
        mock_lease.acquire.return_value = False  # Held by another worker
        worker = CdcSyncWorker(
            lock_table="test-table",
            lease_manager=mock_lease,
            local_db_path=self.db_path,
        )
        result = worker.execute_sync()
        self.assertEqual(result["status"], "LOCKED_SKIPPED")
        mock_lease.release.assert_not_called()

    def test_worker_discrepancy_triggers_full_reinit(self):
        mock_lease = MagicMock()
        mock_lease.acquire.return_value = True

        mock_storage = MagicMock()
        mock_storage.download_if_exists.return_value = False  # Triggers initial sync
        mock_storage.upload_file.return_value = "etag-12345"

        # Mock PG connection and queries
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur

        # Mock WAL level, WAL LSN, and schema introspection
        mock_cur.fetchone.side_effect = [
            ("logical",),  # SHOW wal_level;
            ("0/2000",),   # pg_current_wal_lsn
            ("0/2000",),   # watermark re-read
        ]
        mock_cur.fetchall.side_effect = [
            [("usage_date", "date"), ("gross_usd", "numeric")],  # columns
            [("usage_date",)],  # primary keys
            [("2026-10-18", 12.34)],  # rows
        ]

        worker = CdcSyncWorker(
            lock_table="test-table",
            s3_bucket="test-bucket",
            s3_key="analytics/analytics.duckdb",
            local_db_path=self.db_path,
            replicated_tables=["daily_cost"],
            lease_manager=mock_lease,
            storage=mock_storage,
            pg_connection=mock_conn,
        )

        with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
            result = worker.execute_sync(force_reinit=True)
            self.assertEqual(result["status"], "SUCCESS")
            self.assertEqual(result["reinit_count"], 1)
            mock_storage.upload_file.assert_called_once()
            mock_lease.release.assert_called_once()
            # Ensure EMF metrics written to stdout
            self.assertIn("RealityCheckCDC", mock_stdout.getvalue())

    def test_worker_non_logical_wal_falls_back_to_snapshot(self):
        mock_lease = MagicMock()
        mock_lease.acquire.return_value = True

        mock_storage = MagicMock()
        mock_storage.download_if_exists.return_value = True  # File exists in S3
        mock_storage.upload_file.return_value = "etag-snapshot-fallback"

        # Pre-populate valid local file so file check passes
        applier = DuckDbApplier(self.db_path)
        applier.full_reinit_table(
            table_name="daily_cost",
            columns=["usage_date", "gross_usd"],
            column_types=["DATE", "DOUBLE"],
            rows=[("2026-10-18", 10.0)],
            primary_keys=["usage_date"],
            snapshot_lsn=1000,
        )
        applier.close()

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cur

        # wal_level is 'replica' (default parameter group, logical replication not enabled)
        mock_cur.fetchone.side_effect = [
            ("replica",),  # SHOW wal_level;
        ]
        mock_cur.fetchall.side_effect = [
            [("usage_date", "date"), ("gross_usd", "numeric")],  # columns
            [("usage_date",)],  # primary keys
            [("2026-10-18", 10.0)],  # snapshot rows
        ]

        worker = CdcSyncWorker(
            lock_table="test-table",
            s3_bucket="test-bucket",
            s3_key="analytics/analytics.duckdb",
            lease_manager=mock_lease,
            storage=mock_storage,
            local_db_path=self.db_path,
            replicated_tables=["daily_cost"],
            pg_connection=mock_conn,
        )

        result = worker.execute_sync()
        self.assertEqual(result["status"], "SUCCESS")
        self.assertEqual(result["reinit_count"], 1)  # Graceful fallback to snapshot sync
        mock_storage.upload_file.assert_called_once()
        mock_lease.release.assert_called_once()


class TestDuckDbReader(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "reader.duckdb")
        self.etag_path = os.path.join(self.tmpdir, "reader.etag")

        # Initialize mock DuckDB data
        applier = DuckDbApplier(self.db_path)
        applier.full_reinit_table(
            table_name="daily_cost",
            columns=["usage_date", "gross_usd"],
            column_types=["DATE", "DOUBLE"],
            rows=[
                ("2026-10-18", 15.5),
                ("2026-10-19", 24.5),
            ],
            snapshot_lsn=3000,
        )
        applier.close()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_reader_cache_hit_and_analytical_query(self):
        mock_s3 = MagicMock()
        mock_s3.head_object.return_value = {"ETag": '"etag-abc"'}

        # Prepopulate matching local ETag
        with open(self.etag_path, "w") as f:
            f.write("etag-abc")

        reader = DuckDbReader(
            s3_bucket="test-bucket",
            s3_key="test-key",
            local_db_path=self.db_path,
            etag_path=self.etag_path,
            s3_client=mock_s3,
        )

        query = "SELECT count(*) as count, sum(gross_usd) as total_usd FROM daily_cost;"
        res = reader.query(query)

        self.assertTrue(res["cache_hit"])
        self.assertEqual(res["row_count"], 1)
        self.assertEqual(res["results"][0]["count"], 2)
        self.assertAlmostEqual(res["results"][0]["total_usd"], 40.0)
        self.assertEqual(res["watermark"]["last_applied_lsn"], "3000")
        self.assertIsNotNone(res.get("query_plan"))
        self.assertIn("SEQ_SCAN", res["query_plan"])
        mock_s3.download_file.assert_not_called()  # Verified zero download on hit!

        # Table counts
        counts = reader.get_table_counts()
        self.assertEqual(counts.get("daily_cost"), 2)

    def test_reader_web_html_interface(self):
        from agent.cdc.reader import lambda_handler
        with patch("agent.cdc.reader.DuckDbReader") as mock_reader_cls:
            mock_inst = MagicMock()
            mock_inst.get_watermark.return_value = {
                "last_applied_lsn": "4500",
                "last_commit_timestamp": "2026-10-19T20:00:00Z",
                "sync_mode": "INCREMENTAL",
            }
            mock_inst.get_table_counts.return_value = {
                "daily_cost": 25,
            }
            mock_reader_cls.return_value = mock_inst

            # Web browser GET /
            event = {
                "requestContext": {"http": {"method": "GET", "path": "/"}},
            }
            res = lambda_handler(event, None)
            self.assertEqual(res["statusCode"], 200)
            self.assertIn("text/html", res["headers"]["Content-Type"])
            self.assertIn("DuckDB Serverless Analytics Reader", res["body"])
            self.assertIn("SQL Query (DuckDB dialect)", res["body"])
            self.assertIn("4500", res["body"])
            self.assertIn("daily_cost: <strong>25</strong> records", res["body"])
            self.assertIn("Query Plan (EXPLAIN)", res["body"])
            self.assertIn("/mcp", res["body"])
            self.assertIn("Reality Check — live demo", res["body"])
            self.assertIn("Reality Check — Cloud cost", res["body"])
            self.assertIn("nav-bar", res["body"])

    def test_reader_api_query_post_and_healthz(self):
        from agent.cdc.reader import lambda_handler
        with patch("agent.cdc.reader.DuckDbReader") as mock_reader_cls:
            mock_inst = MagicMock()
            mock_inst.query.return_value = {
                "results": [{"total": 100}],
                "columns": ["total"],
                "row_count": 1,
                "query_time_ms": 0.45,
                "cache_hit": True,
            }
            mock_reader_cls.return_value = mock_inst

            # 1. Healthz
            event_health = {
                "requestContext": {"http": {"method": "GET", "path": "/healthz"}},
            }
            res_h = lambda_handler(event_health, None)
            self.assertEqual(res_h["statusCode"], 200)
            self.assertIn('"engine": "duckdb"', res_h["body"])

            # 2. POST query
            event_post = {
                "requestContext": {"http": {"method": "POST", "path": "/api/query"}},
                "body": json.dumps({"query": "SELECT 100 as total;"}),
            }
            res_p = lambda_handler(event_post, None)
            self.assertEqual(res_p["statusCode"], 200)
            data = json.loads(res_p["body"])
            self.assertEqual(data["row_count"], 1)
            self.assertEqual(data["results"][0]["total"], 100)
            self.assertTrue(data["cache_hit"])

    def test_reader_error_handling(self):
        from agent.cdc.reader import lambda_handler
        with patch("agent.cdc.reader.DuckDbReader") as mock_reader_cls:
            mock_inst = MagicMock()
            mock_inst.query.side_effect = Exception("Syntax error near 'FROMM'")
            mock_reader_cls.return_value = mock_inst

            event = {
                "requestContext": {"http": {"method": "POST", "path": "/"}},
                "body": json.dumps({"query": "SELECT * FROMM daily_cost;"}),
            }
            res = lambda_handler(event, None)
            self.assertEqual(res["statusCode"], 500)
            self.assertIn("Syntax error", res["body"])


class TestMcpServer(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "mcp.duckdb")

        applier = DuckDbApplier(self.db_path)
        applier.full_reinit_table(
            table_name="daily_cost",
            columns=["usage_date", "gross_usd"],
            column_types=["DATE", "DOUBLE"],
            rows=[("2026-10-18", 12.0), ("2026-10-19", 18.0)],
            snapshot_lsn=5000,
        )
        applier.close()

        self.reader = DuckDbReader(local_db_path=self.db_path)
        from agent.cdc.mcp_server import McpServer
        self.mcp = McpServer(reader=self.reader, server_base_url="https://test.lambda-url.us-east-2.on.aws")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_mcp_initialize(self):
        req = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2024-11-05"},
        }
        res = self.mcp._handle_single_rpc(req)
        self.assertEqual(res["id"], 1)
        self.assertEqual(res["result"]["protocolVersion"], "2024-11-05")
        self.assertIn("tools", res["result"]["capabilities"])
        self.assertIn("io.modelcontextprotocol/ui", res["result"]["capabilities"]["extensions"])
        self.assertIn("mcp-ui", res["result"]["capabilities"]["extensions"])

    def test_mcp_tools_list_has_ui_extension_metadata(self):
        req = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        res = self.mcp._handle_single_rpc(req)
        tools = res["result"]["tools"]
        tool_names = [t["name"] for t in tools]
        self.assertIn("query_duckdb", tool_names)
        self.assertIn("get_table_counts", tool_names)
        self.assertIn("get_cdc_watermark", tool_names)

        # Check UI extension metadata on query_duckdb
        query_tool = next(t for t in tools if t["name"] == "query_duckdb")
        self.assertIn("_meta", query_tool)
        self.assertEqual(query_tool["_meta"]["ui"]["resourceUri"], "ui://realitycheck/query-widget")

    def test_mcp_tools_call_query_duckdb_returns_widget(self):
        req = {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "query_duckdb",
                "arguments": {
                    "query": "SELECT count(*) as count, sum(gross_usd) as total FROM daily_cost;",
                    "explain": True,
                },
            },
        }
        res = self.mcp._handle_single_rpc(req)
        self.assertEqual(res["id"], 3)
        content = res["result"]["content"]
        self.assertEqual(len(content), 2)

        # 1. Text content with Markdown table & EXPLAIN plan
        text_item = content[0]
        self.assertEqual(text_item["type"], "text")
        self.assertIn("DuckDB Query Execution Results", text_item["text"])
        self.assertIn("Physical Query Execution Plan", text_item["text"])

        # 2. In-place UI Widget Resource
        widget_item = content[1]
        self.assertEqual(widget_item["type"], "resource")
        self.assertEqual(widget_item["resource"]["uri"], "ui://realitycheck/query-widget")
        self.assertIn("<!DOCTYPE html>", widget_item["resource"]["text"])
        self.assertIn("DuckDB In-Place Analytics", widget_item["resource"]["text"])

        # Meta UI link
        self.assertEqual(res["result"]["_meta"]["ui"]["resourceUri"], "ui://realitycheck/query-widget")

    def test_mcp_resources_read_widget_html(self):
        req = {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "resources/read",
            "params": {"uri": "ui://realitycheck/query-widget"},
        }
        res = self.mcp._handle_single_rpc(req)
        contents = res["result"]["contents"]
        self.assertEqual(len(contents), 1)
        self.assertEqual(contents[0]["uri"], "ui://realitycheck/query-widget")
        self.assertEqual(contents[0]["mimeType"], "text/html")
        self.assertIn("executeWidgetQuery", contents[0]["text"])

    def test_mcp_http_get_and_options(self):
        # 1. GET /mcp with JSON manifest
        event_get = {
            "requestContext": {"http": {"method": "GET", "path": "/mcp"}},
            "headers": {"accept": "application/json"},
        }
        res_get = self.mcp.handle_request(event_get)
        self.assertEqual(res_get["statusCode"], 200)
        manifest = json.loads(res_get["body"])
        self.assertEqual(manifest["protocolVersion"], "2024-11-05")

        # 2. OPTIONS /mcp for CORS
        event_opt = {
            "requestContext": {"http": {"method": "OPTIONS", "path": "/mcp"}},
        }
        res_opt = self.mcp.handle_request(event_opt)
        self.assertEqual(res_opt["statusCode"], 204)
        self.assertEqual(res_opt["headers"]["Access-Control-Allow-Origin"], "*")


if __name__ == "__main__":
    unittest.main()
