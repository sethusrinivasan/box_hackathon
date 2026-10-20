"""
agent/cdc/wal_parser.py — PostgreSQL Logical Replication Message Parser.

Implements decoders for PostgreSQL's native `pgoutput` binary replication stream
as well as structured mutation events, converting WAL operations into normalized
WalChange records (INSERT, UPDATE, DELETE).
"""
from __future__ import annotations

import datetime
import struct
from dataclasses import dataclass, field
from typing import Any

# PostgreSQL epoch is 2000-01-01 00:00:00 UTC
PG_EPOCH = datetime.datetime(2000, 1, 1, tzinfo=datetime.timezone.utc)


@dataclass
class ColumnInfo:
    name: str
    type_oid: int
    is_key: bool


@dataclass
class RelationMetadata:
    relation_id: int
    namespace: str
    name: str
    columns: list[ColumnInfo] = field(default_factory=list)

    @property
    def key_columns(self) -> list[str]:
        keys = [c.name for c in self.columns if c.is_key]
        # If no explicit replica identity key is flagged, default to all columns or first
        return keys if keys else ([self.columns[0].name] if self.columns else [])


@dataclass
class WalChange:
    op: str  # 'INSERT', 'UPDATE', 'DELETE'
    schema: str
    table: str
    keys: dict[str, Any]
    values: dict[str, Any]
    lsn: int
    commit_time: datetime.datetime | None = None
    tx_id: int | None = None


class PgoutputParser:
    """Parser for PostgreSQL pgoutput binary logical replication messages."""

    def __init__(self) -> None:
        self.relations: dict[int, RelationMetadata] = {}
        self.current_tx_id: int | None = None
        self.current_commit_time: datetime.datetime | None = None
        self.current_lsn: int = 0

    def parse_message(self, data: bytes, lsn: int = 0) -> WalChange | None:
        """Parse a single raw replication message payload.

        Returns a WalChange if the message is a row mutation (I, U, D), or None
        for control messages (Begin, Commit, Relation, Type).
        """
        if not data:
            return None

        msg_type = chr(data[0])
        offset = 1

        if msg_type == "B":  # Begin
            # 8 bytes final LSN, 8 bytes commit timestamp (microseconds since 2000-01-01), 4 bytes XID
            final_lsn, ts_micros, xid = struct.unpack_from(">QQI", data, offset)
            self.current_lsn = lsn or final_lsn
            self.current_commit_time = PG_EPOCH + datetime.timedelta(microseconds=ts_micros)
            self.current_tx_id = xid
            return None

        elif msg_type == "C":  # Commit
            # 1 byte flags, 8 bytes commit LSN, 8 bytes end LSN, 8 bytes commit timestamp
            flags, commit_lsn, end_lsn, ts_micros = struct.unpack_from(">BQQQ", data, offset)
            self.current_lsn = commit_lsn
            self.current_commit_time = PG_EPOCH + datetime.timedelta(microseconds=ts_micros)
            return None

        elif msg_type == "R":  # Relation
            rel_id, = struct.unpack_from(">I", data, offset)
            offset += 4
            namespace, offset = self._read_null_string(data, offset)
            relname, offset = self._read_null_string(data, offset)
            _replica_identity = chr(data[offset])
            offset += 1
            num_cols, = struct.unpack_from(">H", data, offset)
            offset += 2

            cols: list[ColumnInfo] = []
            for _ in range(num_cols):
                flags = data[offset]
                offset += 1
                col_name, offset = self._read_null_string(data, offset)
                type_oid, _type_mod = struct.unpack_from(">Ii", data, offset)
                offset += 8
                cols.append(ColumnInfo(
                    name=col_name,
                    type_oid=type_oid,
                    is_key=(flags & 1) != 0,
                ))

            self.relations[rel_id] = RelationMetadata(
                relation_id=rel_id,
                namespace=namespace,
                name=relname,
                columns=cols,
            )
            return None

        elif msg_type == "I":  # Insert
            rel_id, = struct.unpack_from(">I", data, offset)
            offset += 4
            tuple_type = chr(data[offset])  # 'N' (New)
            offset += 1
            rel = self.relations.get(rel_id)
            if not rel:
                return None
            new_vals, _ = self._read_tuple_data(data, offset, rel)
            keys = {k: new_vals[k] for k in rel.key_columns if k in new_vals}
            return WalChange(
                op="INSERT",
                schema=rel.namespace,
                table=rel.name,
                keys=keys,
                values=new_vals,
                lsn=lsn or self.current_lsn,
                commit_time=self.current_commit_time,
                tx_id=self.current_tx_id,
            )

        elif msg_type == "U":  # Update
            rel_id, = struct.unpack_from(">I", data, offset)
            offset += 4
            rel = self.relations.get(rel_id)
            if not rel:
                return None

            sub_type = chr(data[offset])
            old_keys: dict[str, Any] = {}
            if sub_type in ("K", "O"):  # Key or Old tuple precedes New
                offset += 1
                old_vals, offset = self._read_tuple_data(data, offset, rel)
                old_keys = {k: old_vals[k] for k in rel.key_columns if k in old_vals}
                sub_type = chr(data[offset])

            if sub_type == "N":  # New tuple
                offset += 1
                new_vals, _ = self._read_tuple_data(data, offset, rel)
                keys = old_keys or {k: new_vals[k] for k in rel.key_columns if k in new_vals}
                return WalChange(
                    op="UPDATE",
                    schema=rel.namespace,
                    table=rel.name,
                    keys=keys,
                    values=new_vals,
                    lsn=lsn or self.current_lsn,
                    commit_time=self.current_commit_time,
                    tx_id=self.current_tx_id,
                )
            return None

        elif msg_type == "D":  # Delete
            rel_id, = struct.unpack_from(">I", data, offset)
            offset += 4
            rel = self.relations.get(rel_id)
            if not rel:
                return None

            sub_type = chr(data[offset])  # 'K' (Key) or 'O' (Old)
            offset += 1
            old_vals, _ = self._read_tuple_data(data, offset, rel)
            keys = {k: old_vals[k] for k in rel.key_columns if k in old_vals}
            return WalChange(
                op="DELETE",
                schema=rel.namespace,
                table=rel.name,
                keys=keys,
                values=old_vals,
                lsn=lsn or self.current_lsn,
                commit_time=self.current_commit_time,
                tx_id=self.current_tx_id,
            )

        return None

    @staticmethod
    def _read_null_string(data: bytes, offset: int) -> tuple[str, int]:
        end = data.find(b"\x00", offset)
        if end == -1:
            return data[offset:].decode("utf-8", errors="replace"), len(data)
        val = data[offset:end].decode("utf-8", errors="replace")
        return val, end + 1

    @staticmethod
    def _read_tuple_data(
        data: bytes,
        offset: int,
        relation: RelationMetadata,
    ) -> tuple[dict[str, Any], int]:
        """Decode TupleData segment according to PostgreSQL replication protocol."""
        num_cols, = struct.unpack_from(">H", data, offset)
        offset += 2
        values: dict[str, Any] = {}

        for i in range(num_cols):
            col_name = relation.columns[i].name if i < len(relation.columns) else f"col_{i}"
            col_kind = chr(data[offset])
            offset += 1

            if col_kind == "n":  # NULL
                values[col_name] = None
            elif col_kind == "u":  # Unchanged TOAST
                continue
            elif col_kind == "t":  # Text-formatted value
                length, = struct.unpack_from(">I", data, offset)
                offset += 4
                raw_bytes = data[offset:offset + length]
                offset += length
                values[col_name] = raw_bytes.decode("utf-8", errors="replace")
            elif col_kind == "b":  # Binary-formatted value
                length, = struct.unpack_from(">I", data, offset)
                offset += 4
                values[col_name] = data[offset:offset + length]
                offset += length

        return values, offset
