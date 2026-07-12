# SPDX-License-Identifier: AGPL-3.0-or-later
"""Writes rows into Fluxer's Postgres KV table, mirroring the TS executor.

Each logical Cassandra table is stored in the single `fluxer_kv` table keyed by
(table_name, row_key). We replicate the executor's `upsert` (full row, replacing
row_data) and `patch` (merge into existing row_data) semantics.
"""

import json

import psycopg2
import psycopg2.extras

from kv_encoding import TypeCodec


class KvStore:
    def __init__(self, schema, dsn, kv_table="fluxer_kv"):
        self.schema = schema
        self.codec = TypeCodec(schema.user_types)
        self.kv_table = kv_table
        self.conn = psycopg2.connect(dsn)
        self.conn.autocommit = False
        self._ident = psycopg2.extensions.quote_ident(kv_table, self.conn)

    def close(self):
        self.conn.close()

    def commit(self):
        self.conn.commit()

    def rollback(self):
        self.conn.rollback()

    # -- key/row helpers -----------------------------------------------------

    def _row_key(self, spec, row):
        return self.codec.key_from_columns(spec.primary_key, spec.columns, row)

    def _partition_key(self, spec, row):
        return self.codec.key_from_columns(spec.partition_key, spec.columns, row)

    def _encoded_row_json(self, spec, row):
        # Only encode columns that the table actually declares.
        filtered = {c: row[c] for c in spec.columns if c in row}
        return json.dumps(self.codec.encode_row(spec.columns, filtered), ensure_ascii=False)

    # -- write operations ----------------------------------------------------

    def upsert(self, table_name, row):
        """Insert or fully replace a row (mirrors executor `upsert`).

        `row` must contain all primary-key columns. Missing non-key columns are
        simply absent from row_data (treated as null on read).
        """
        spec = self.schema.table(table_name)
        for col in spec.primary_key:
            if row.get(col) is None:
                raise ValueError(f"{table_name}: primary key column {col!r} is required")
        row_key = self._row_key(spec, row)
        partition_key = self._partition_key(spec, row)
        row_json = self._encoded_row_json(spec, row)
        with self.conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO {self._ident} "
                "(table_name, partition_key, row_key, row_data, expires_at, updated_at) "
                "VALUES (%s, %s, %s, %s::jsonb, NULL, now()) "
                "ON CONFLICT (table_name, row_key) DO UPDATE SET "
                "partition_key = EXCLUDED.partition_key, row_data = EXCLUDED.row_data, "
                "expires_at = EXCLUDED.expires_at, updated_at = now()",
                (table_name, partition_key, row_key, row_json),
            )

    def patch(self, table_name, key, patch):
        """Merge `patch` columns into an existing row (mirrors executor `patch`).

        `key` holds the primary-key columns; `patch` holds columns to set.
        """
        spec = self.schema.table(table_name)
        merged = dict(key)
        existing = self._get_raw(table_name, self._row_key(spec, key))
        if existing is not None:
            merged.update(self._decode_known_columns(spec, existing))
        merged.update(patch)
        self.upsert(table_name, merged)

    def exists(self, table_name, key):
        spec = self.schema.table(table_name)
        return self._get_raw(table_name, self._row_key(spec, key)) is not None

    def _get_raw(self, table_name, row_key):
        with self.conn.cursor() as cur:
            cur.execute(
                f"SELECT row_data FROM {self._ident} "
                "WHERE table_name = %s AND row_key = %s "
                "AND (expires_at IS NULL OR expires_at > now()) LIMIT 1",
                (table_name, row_key),
            )
            r = cur.fetchone()
            return r[0] if r else None

    def _decode_known_columns(self, spec, row_data):
        """Decode stored row_data back into Python values for re-encoding.

        Only used by patch() to preserve existing columns. We decode the tagged
        forms shallowly into values our codec can re-encode.
        """
        from kv_encoding import ENCODED_TYPE_KEY

        def decode(v):
            if isinstance(v, list):
                return [decode(x) for x in v]
            if isinstance(v, dict):
                t = v.get(ENCODED_TYPE_KEY)
                if t == "bigint":
                    return int(v["value"])
                if t == "date":
                    return v["value"]  # ISO string; re-encoded via js_iso
                if t == "local_date":
                    return v["value"]
                if t == "buffer":
                    import base64
                    return base64.b64decode(v["value"])
                if t == "set":
                    return [decode(x) for x in v.get("value", [])]
                if t == "map":
                    return [[decode(k), decode(val)] for k, val in v.get("value", [])]
                return {k: decode(val) for k, val in v.items()}
            return v

        return {c: decode(row_data[c]) for c in spec.columns if c in row_data}
