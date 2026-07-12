# SPDX-License-Identifier: AGPL-3.0-or-later
"""Loads Fluxer's authoritative Cassandra schema and exposes table specs.

Source of truth: tools/dev/cassandra_target_schema.json in the Fluxer repo.
Each table's full primary key and partition key are parsed from the CQL
`primary_key` string, matching the TS DSL semantics where `primaryKey` is the
ordered full key and `partitionKey` defaults to the first key column unless an
explicit composite partition `((a, b), c)` is given.
"""

import json


def parse_primary_key(pk_string):
    """Parse a CQL primary key string into (primary_key, partition_key) lists.

    Examples:
        '((channel_id, bucket), message_id)' -> (
            ['channel_id', 'bucket', 'message_id'], ['channel_id', 'bucket'])
        '((guild_id), user_id)' -> (['guild_id', 'user_id'], ['guild_id'])
        '(channel_id)'          -> (['channel_id'], ['channel_id'])
        '(guild_id, user_id)'   -> (['guild_id', 'user_id'], ['guild_id'])
    """
    s = pk_string.strip()
    if s.startswith("(") and s.endswith(")"):
        s = s[1:-1].strip()

    partition_cols = []
    rest = s
    if s.startswith("("):
        depth, end = 0, -1
        for i, ch in enumerate(s):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        inner = s[1:end]
        partition_cols = [c.strip() for c in inner.split(",") if c.strip()]
        rest = s[end + 1:].lstrip(", ").strip()
    else:
        first, _, tail = s.partition(",")
        partition_cols = [first.strip()]
        rest = tail.strip()

    clustering_cols = [c.strip() for c in rest.split(",") if c.strip()] if rest else []
    primary_key = partition_cols + clustering_cols
    return primary_key, partition_cols


class TableSpec:
    def __init__(self, name, columns, primary_key, partition_key, options=""):
        self.name = name
        self.columns = columns  # dict: col_name -> cql_type
        self.primary_key = primary_key  # ordered list
        self.partition_key = partition_key  # ordered list
        self.options = options


class Schema:
    def __init__(self, path):
        with open(path) as f:
            data = json.load(f)
        self.user_types = {
            ut["name"]: [(f["name"], f["type"]) for f in ut["fields"]]
            for ut in data["user_types"]
        }
        self.tables = {}
        for t in data["tables"]:
            cols = {c["name"]: c["type"] for c in t["columns"]}
            pk, partition = parse_primary_key(t["primary_key"])
            self.tables[t["name"]] = TableSpec(
                t["name"], cols, pk, partition, t.get("options", "")
            )

    def table(self, name):
        spec = self.tables.get(name)
        if spec is None:
            raise KeyError(f"Unknown Fluxer table: {name}")
        return spec
