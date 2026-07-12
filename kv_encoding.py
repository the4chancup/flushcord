# SPDX-License-Identifier: AGPL-3.0-or-later
"""Replicates Fluxer's PostgresKvQueryExecutor value encoding exactly.

The Postgres KV backend stores every Cassandra row as one row in the `fluxer_kv`
table: (table_name, partition_key, row_key, row_data jsonb). The TypeScript
executor encodes values with a tagged-JSON scheme (`__fluxer_type`). The
`row_key` and `partition_key` text columns are built by JSON-stringifying each
key column's encoded value and joining with U+001F, so our Python output MUST be
byte-for-byte identical to JS `JSON.stringify(encodeValue(...))`.

See fluxer_api/src/api/database/PostgresKvQueryExecutor.ts (encodeValue,
valueKey, keyFromColumns) and fluxer_svc/src/postgres.rs (kv_key).
"""

import base64
import json
from datetime import date, datetime, timezone

VALUE_SEPARATOR = ""
ENCODED_TYPE_KEY = "__fluxer_type"

# CQL scalar types that map to a JS BigInt -> tagged "bigint".
_BIGINT_TYPES = {"bigint", "counter", "varint"}
# CQL scalar types that map to a plain JS number.
_NUMBER_TYPES = {"int", "smallint", "tinyint", "double", "float", "decimal"}
# CQL scalar types that map to a JS string.
_TEXT_TYPES = {"text", "varchar", "ascii", "inet", "uuid", "timeuuid"}


def js_iso(value):
    """Format a datetime/epoch-ms as JS Date.toISOString() does.

    Always UTC, always 3-digit milliseconds, trailing 'Z'. E.g.
    '2025-10-23T17:34:16.409Z'.
    """
    if isinstance(value, str):
        # Accept an existing ISO string (e.g. a preserved row_data date).
        iso = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso)
    elif isinstance(value, (int, float)):
        dt = datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    elif isinstance(value, datetime):
        dt = value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    else:
        raise TypeError(f"Cannot format timestamp from {type(value)}")
    millis = dt.microsecond // 1000
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + f".{millis:03d}Z"


def _local_date_str(value):
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _strip_frozen(ctype):
    ctype = ctype.strip()
    while ctype.startswith("frozen<") and ctype.endswith(">"):
        ctype = ctype[len("frozen<"):-1].strip()
    return ctype


def _split_generic_args(inner):
    """Split 'K, V' or 'list<x>, int' respecting nested angle brackets."""
    args, depth, current = [], 0, ""
    for ch in inner:
        if ch == "<":
            depth += 1
            current += ch
        elif ch == ">":
            depth -= 1
            current += ch
        elif ch == "," and depth == 0:
            args.append(current.strip())
            current = ""
        else:
            current += ch
    if current.strip():
        args.append(current.strip())
    return args


class TypeCodec:
    """Encodes Python values to Fluxer's tagged-JSON form, driven by CQL type.

    `user_types` maps UDT name -> list of (field_name, field_type).
    """

    def __init__(self, user_types):
        self.user_types = user_types

    def encode(self, ctype, value):
        if value is None:
            return None
        ctype = _strip_frozen(ctype)
        low = ctype.lower()

        if low.startswith("set<"):
            elem = _split_generic_args(ctype[4:-1])[0]
            return {ENCODED_TYPE_KEY: "set", "value": [self.encode(elem, v) for v in value]}
        if low.startswith("list<"):
            elem = _split_generic_args(ctype[5:-1])[0]
            return [self.encode(elem, v) for v in value]
        if low.startswith("map<"):
            kt, vt = _split_generic_args(ctype[4:-1])
            items = value.items() if isinstance(value, dict) else value
            return {
                ENCODED_TYPE_KEY: "map",
                "value": [[self.encode(kt, k), self.encode(vt, v)] for k, v in items],
            }

        if low in _BIGINT_TYPES:
            return {ENCODED_TYPE_KEY: "bigint", "value": str(int(value))}
        if low in _NUMBER_TYPES:
            if low in ("double", "float", "decimal"):
                return float(value)
            return int(value)
        if low == "boolean":
            return bool(value)
        if low == "timestamp":
            return {ENCODED_TYPE_KEY: "date", "value": js_iso(value)}
        if low == "date":
            return {ENCODED_TYPE_KEY: "local_date", "value": _local_date_str(value)}
        if low == "blob":
            raw = value if isinstance(value, (bytes, bytearray)) else bytes(value)
            return {ENCODED_TYPE_KEY: "buffer", "value": base64.b64encode(raw).decode("ascii")}
        if low in _TEXT_TYPES:
            return str(value)

        # User-defined type: encode as a plain object of its fields.
        if ctype in self.user_types:
            fields = self.user_types[ctype]
            if not isinstance(value, dict):
                raise TypeError(f"UDT {ctype} expects a dict, got {type(value)}")
            return {fname: self.encode(ftype, value.get(fname)) for fname, ftype in fields}

        raise ValueError(f"Unsupported CQL type for encoding: {ctype!r}")

    def value_key(self, ctype, value):
        """JS valueKey(): JSON.stringify(encodeValue(value)), compact, UTF-8."""
        return json.dumps(self.encode(ctype, value), separators=(",", ":"), ensure_ascii=False)

    def key_from_columns(self, columns, types, row):
        """Join per-column value_keys with U+001F (keyFromColumns)."""
        return VALUE_SEPARATOR.join(self.value_key(types[c], row.get(c)) for c in columns)

    def encode_row(self, types, row):
        """Encode a full row dict -> JSON-able dict for the row_data jsonb column."""
        return {col: self.encode(types[col], val) for col, val in row.items()}
