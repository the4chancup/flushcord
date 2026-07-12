# SPDX-License-Identifier: AGPL-3.0-or-later
"""Read-only access to the Discord backup SQLite database."""

import json
import sqlite3
from datetime import datetime, timezone

# Discord epoch == Fluxer epoch (2015-01-01). Discord snowflakes are therefore
# directly reusable as Fluxer snowflakes (same 42-bit ms timestamp at bit 22).
DISCORD_EPOCH = 1_420_070_400_000


def snowflake_to_ms(snowflake):
    return (int(snowflake) >> 22) + DISCORD_EPOCH


def parse_ts(ts):
    """Parse a Discord ISO timestamp string into an aware UTC datetime."""
    if ts is None:
        return None
    return datetime.fromisoformat(ts).astimezone(timezone.utc)


class DiscordBackup:
    def __init__(self, path):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row

    def close(self):
        self.conn.close()

    def _all(self, sql, params=()):
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def guild(self):
        rows = self._all("SELECT * FROM guild_profile LIMIT 1")
        return rows[0] if rows else None

    def roles(self):
        return self._all("SELECT * FROM roles ORDER BY position")

    def channels(self):
        return self._all("SELECT * FROM channels ORDER BY position")

    def threads(self):
        return self._all("SELECT * FROM threads")

    def users(self):
        return self._all("SELECT * FROM users")

    def permissions(self, channel_id):
        return self._all(
            "SELECT * FROM permissions WHERE channel_id = ?", (channel_id,)
        )

    def messages(self, channel_id):
        return self._all(
            "SELECT * FROM messages WHERE channel_id = ? ORDER BY id", (channel_id,)
        )

    def message_count(self):
        return self.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]

    def attachments(self, message_id):
        return self._all(
            "SELECT * FROM attachments WHERE message_id = ?", (message_id,)
        )

    def embeds(self, message_id):
        return self._all("SELECT * FROM embeds WHERE message_id = ?", (message_id,))

    def stickers(self, message_id):
        return self._all(
            "SELECT * FROM message_stickers WHERE message_id = ?", (message_id,)
        )

    def reactions(self, message_id):
        return self._all(
            "SELECT * FROM reactions WHERE message_id = ?", (message_id,)
        )

    def media(self, file_hash):
        rows = self._all("SELECT * FROM media_pool WHERE hash = ?", (file_hash,))
        return rows[0] if rows else None

    def server_assets(self, asset_type=None):
        if asset_type:
            return self._all(
                "SELECT * FROM server_assets WHERE type = ?", (asset_type,)
            )
        return self._all("SELECT * FROM server_assets")

    def distinct_message_channels(self):
        return [
            r[0]
            for r in self.conn.execute(
                "SELECT DISTINCT channel_id FROM messages"
            ).fetchall()
        ]

    def earliest_message_ms_by_author(self):
        """Map author_id -> earliest message timestamp (ms), for join times."""
        out = {}
        for r in self.conn.execute(
            "SELECT author_id, MIN(id) mid FROM messages GROUP BY author_id"
        ).fetchall():
            if r[0] is not None and r[1] is not None:
                out[int(r[0])] = snowflake_to_ms(r[1])
        return out
