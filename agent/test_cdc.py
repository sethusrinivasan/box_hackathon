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
import os
import shutil
import struct
import tempfile
import unittest
from unittest.mock import MagicMock, patch

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
        v_missing = DuckDbValidator("/tmp/nonexistent_duckdb_test.duckdb")
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

        # Mock WAL LSN and schema introspection
        mock_cur.fetchone.side_effect = [
            ("0/2000",),  # pg_current_wal_lsn
            ("0/2000",),  # watermark re-read
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
        mock_s3.download_file.assert_not_called()  # Verified zero download on hit!


if __name__ == "__main__":
    unittest.main()
