# SPDX-License-Identifier: AGPL-3.0-or-later
"""Offline tests for `import-channels` (and restore_channel_members' user
selection). No Postgres, no S3, no network: a synthetic backup.db in a temp
dir plus an in-memory KvStore with the same method surface.

Run: PYTHONUTF8=1 .venv/Scripts/python test_import_channels.py
"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from discord_db import DiscordBackup  # noqa: E402
from migrate import Migrator  # noqa: E402
from snowflake import make_bucket  # noqa: E402
import restore_channel_members as rcm  # noqa: E402

GUILD_ID = 124029625755107328
CH = 1135367800337207336  # #pulitzer-candidates
CH2 = 1202514209582219294  # #replays-and-videos
AUTHOR_DID = 111
AUTHOR_FID = 900001
OTHER_DID = 222
OTHER_FID = 900002


def make_backup_db(path):
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE guild_profile (id INTEGER, name TEXT);
        CREATE TABLE channels (id INTEGER, type INTEGER, name TEXT,
                               category_id INTEGER, position INTEGER);
        CREATE TABLE threads (id INTEGER, parent_id INTEGER, name TEXT);
        CREATE TABLE users (id INTEGER, username TEXT, type INTEGER);
        CREATE TABLE messages (id INTEGER, channel_id INTEGER,
                               author_id INTEGER, content TEXT, type INTEGER,
                               timestamp TEXT, is_pinned INTEGER,
                               message_reference INTEGER);
        CREATE TABLE attachments (id INTEGER, message_id INTEGER,
                                  filename TEXT, local_hash TEXT, size INTEGER,
                                  content_type TEXT);
        CREATE TABLE media_pool (hash TEXT, local_path TEXT, content_type TEXT);
        CREATE TABLE embeds (message_id INTEGER, data TEXT);
        CREATE TABLE message_stickers (message_id INTEGER);
        CREATE TABLE reactions (message_id INTEGER, emoji_id INTEGER,
                                emoji_name TEXT, user_ids TEXT);
        CREATE TABLE permissions (channel_id INTEGER, target_id INTEGER,
                                  target_type TEXT, allow INTEGER, deny INTEGER);
        CREATE TABLE server_assets (id INTEGER, type INTEGER, filename TEXT,
                                    name TEXT, content_type TEXT);
        CREATE TABLE roles (id INTEGER, name TEXT, permissions INTEGER,
                            position INTEGER, color INTEGER, hoist INTEGER,
                            mentionable INTEGER);
    """)
    db.execute("INSERT INTO guild_profile VALUES (?, '4cc')", (GUILD_ID,))
    db.execute("INSERT INTO channels VALUES (?,0,'pulitzer-candidates',0,0)",
               (CH,))
    db.execute("INSERT INTO channels VALUES (?,0,'replays-and-videos',0,1)",
               (CH2,))
    db.execute("INSERT INTO users VALUES (?,'alice',0)", (AUTHOR_DID,))
    db.commit()
    db.close()


def add_message(db_path, mid, cid=CH, author=AUTHOR_DID, content="hi",
                ts=None):
    ts = ts or "2024-06-01T12:00:00+00:00"
    db = sqlite3.connect(db_path)
    db.execute("INSERT INTO messages VALUES (?,?,?,?,0,?,0,NULL)",
               (mid, cid, author, content, ts))
    db.commit()
    db.close()


def make_relay_db(path, rows):
    """rows: [(discord_id, bonfire_ids list, channel_id)]"""
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE messages (discord_id INTEGER PRIMARY KEY, "
               "bonfire_ids TEXT, channel_id INTEGER)")
    for did, bids, cid in rows:
        db.execute("INSERT INTO messages VALUES (?,?,?)",
                   (did, json.dumps(bids), cid))
    db.commit()
    db.close()


PKS = {
    "guilds": ("guild_id",),
    "channels": ("channel_id", "soft_deleted"),
    "channel_state": ("channel_id",),
    "messages": ("channel_id", "bucket", "message_id"),
    "messages_by_author_id_v2": ("author_id", "channel_id", "message_id"),
    "channel_pins": ("channel_id", "message_id"),
    "attachment_lookup": ("channel_id", "attachment_id"),
    "message_reactions": ("channel_id", "bucket", "message_id",
                        "emoji_id", "user_id"),
    "channel_message_buckets": ("channel_id", "bucket"),
    "channel_empty_buckets": ("channel_id", "bucket"),
    "guild_members": ("guild_id", "user_id"),
    "guild_members_by_user_id": ("user_id", "guild_id"),
    "guild_bans": ("guild_id", "user_id"),
}


class FakeKv:
    """In-memory KvStore stand-in with the same method surface."""

    def __init__(self):
        self.rows = {}  # (table, pk tuple) -> row dict
        self.commits = 0
        self.rollbacks = 0
        self.deleted = []  # (table, key) log

    @staticmethod
    def _pk(table, row):
        return tuple(row[c] for c in PKS[table])

    def upsert(self, table, row):
        self.rows[(table, self._pk(table, row))] = dict(row)

    def patch(self, table, key, patch):
        pk = self._pk(table, key)
        merged = dict(self.rows.get((table, pk), dict(key)))
        merged.update(patch)
        self.rows[(table, pk)] = merged

    def get(self, table, key):
        return self.rows.get((table, self._pk(table, key)))

    def exists(self, table, key):
        return (table, self._pk(table, key)) in self.rows

    def delete(self, table, key):
        self.deleted.append((table, dict(key)))
        return int(self.rows.pop((table, self._pk(table, key)), None) is not None)

    def rows_by_field(self, table, field, value):
        return [dict(r) for (t, _), r in self.rows.items()
                if t == table and int(r.get(field) or 0) == int(value)]

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        pass


def make_migrator(backup_dir, kv, user_map, s3=None):
    m = Migrator.__new__(Migrator)
    db_path = os.path.join(backup_dir, "backup.db")
    m.cfg = SimpleNamespace(
        backup_db=db_path, backup_dir=backup_dir,
        unmapped_author_mode="skip", reaction_mode="skip",
        guild_owner_user_id=0, upload_attachments=bool(s3),
    )
    m.db = DiscordBackup(db_path)
    m.kv = kv
    m.user_map = user_map
    m.s3 = s3
    m.guild_id = None
    m.owner_id = None
    m.channel_last = {}
    m.channel_buckets = {}
    m._animated_emoji_ids = set()
    m._emoji = SimpleNamespace(qualify=lambda s: s, surrogate=lambda s: s)
    m._klipy = None
    m._youtube = None
    m._ffmpeg_ok = False
    m._gif_misses = []
    m.stats = {"messages": 0, "attachments": 0, "reactions": 0, "skipped": 0,
               "gifs": 0, "gif_misses": 0, "youtube": 0, "youtube_misses": 0}
    return m


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "backup.db")
        make_backup_db(self.db_path)
        self.kv = FakeKv()
        self.kv.upsert("guilds", {"guild_id": GUILD_ID, "owner_id": 7,
                                  "member_count": 100,
                                  "members_indexed_at": "2025-01-01"})
        for cid in (CH, CH2):
            self.kv.upsert("channels", {"channel_id": cid,
                                        "soft_deleted": False,
                                        "guild_id": GUILD_ID,
                                        "indexed_at": "2025-01-01"})
            self.kv.upsert("channel_state", {"channel_id": cid,
                                             "created_bucket": 1,
                                             "has_messages": False})
        self.user_map = {AUTHOR_DID: AUTHOR_FID, OTHER_DID: OTHER_FID}
        self.m = make_migrator(self.tmp, self.kv, self.user_map)

    def tearDown(self):
        self.m.db.close()


class TestImportChannels(Base):
    def test_channel_missing_on_bonfire_aborts(self):
        self.kv.rows.clear()
        self.kv.upsert("guilds", {"guild_id": GUILD_ID, "owner_id": 7})
        add_message(self.db_path, 1000)
        with self.assertRaises(SystemExit):
            self.m.import_channels([CH])

    def test_existing_non_relayed_message_aborts(self):
        add_message(self.db_path, 1000)
        # Channel already contains a Bonfire message that isn't a relay copy.
        self.kv.upsert("messages", {"channel_id": CH, "bucket": make_bucket(999),
                                    "message_id": 999, "author_id": 5,
                                    "content": "hi", "version": 1})
        with self.assertRaises(SystemExit):
            self.m.import_channels([CH])
        self.assertEqual(self.kv.commits, 0)

    def test_relayed_message_skipped_and_copy_allowed(self):
        relayed_id, bonfire_copy = 1000, 5500
        add_message(self.db_path, relayed_id)          # relay already posted it
        add_message(self.db_path, 1001, content="new")  # to be imported
        relay_db = os.path.join(self.tmp, "relay.db")
        make_relay_db(relay_db, [(relayed_id, [bonfire_copy], CH)])
        # the relay copy exists in KV; guard must accept it
        self.kv.upsert("messages", {"channel_id": CH, "bucket": 1,
                                    "message_id": bonfire_copy,
                                    "author_id": 5, "content": "r",
                                    "version": 1})
        self.m.import_channels([CH], relay_state_path=relay_db)
        imported = {r["message_id"]
                    for (t, _), r in self.kv.rows.items() if t == "messages"}
        self.assertEqual(imported, {bonfire_copy, 1001})  # copy kept, 1000 skipped

    def test_forward_only_last_message_id(self):
        small, big = 1000, 2000
        add_message(self.db_path, small)
        # existing state already ahead -> kept
        self.kv.patch("channel_state", {"channel_id": CH},
                      {"last_message_id": 9999, "last_message_bucket": 9})
        self.kv.patch("channels", {"channel_id": CH, "soft_deleted": False},
                      {"last_message_id": 9999})
        self.m.import_channels([CH])
        st = self.kv.get("channel_state", {"channel_id": CH})
        self.assertEqual(st["last_message_id"], 9999)
        self.assertTrue(st["has_messages"])
        self.assertEqual(self.kv.get("channels",
                                     {"channel_id": CH,
                                      "soft_deleted": False})
                         ["last_message_id"], 9999)
        # a second channel whose existing last_message_id is older moves forward
        add_message(self.db_path, big, cid=CH2)
        self.m.import_channels([CH2])
        st = self.kv.get("channel_state", {"channel_id": CH2})
        self.assertEqual(st["last_message_id"], big)
        self.assertEqual(st["last_message_bucket"], make_bucket(big))
        ch = self.kv.get("channels", {"channel_id": CH2, "soft_deleted": False})
        self.assertEqual(ch["last_message_id"], big)

    def test_empty_buckets_deleted_and_indexed_at_cleared(self):
        mid = 1000
        add_message(self.db_path, mid)
        self.kv.upsert("channel_empty_buckets",
                       {"channel_id": CH, "bucket": make_bucket(mid)})
        self.m.import_channels([CH])
        self.assertFalse(self.kv.exists(
            "channel_empty_buckets",
            {"channel_id": CH, "bucket": make_bucket(mid)}))
        self.assertTrue(self.kv.exists(
            "channel_message_buckets",
            {"channel_id": CH, "bucket": make_bucket(mid)}))
        ch = self.kv.get("channels", {"channel_id": CH, "soft_deleted": False})
        self.assertIsNone(ch["indexed_at"])

    def test_members_added_and_skipped(self):
        add_message(self.db_path, 1000)
        self.kv.upsert("guild_members", {"guild_id": GUILD_ID,
                                         "user_id": 900003})
        self.kv.upsert("guild_bans", {"guild_id": GUILD_ID,
                                      "user_id": 900004})
        # map the skip-case fids so they reach the member/ban checks
        self.m.user_map = dict(self.user_map)
        self.m.user_map.update({301: 900003, 302: 900004})
        members = os.path.join(self.tmp, "members.txt")
        with open(members, "w") as f:
            f.write(f"{OTHER_FID}\n900003\n900004\n12345\n")
        self.m.import_channels([CH], add_members_path=members)
        gm = self.kv.get("guild_members",
                         {"guild_id": GUILD_ID, "user_id": OTHER_FID})
        self.assertIsNotNone(gm)
        self.assertIsNone(gm["role_ids"])
        self.assertIsNone(gm["nick"])
        self.assertTrue(self.kv.exists(
            "guild_members_by_user_id",
            {"user_id": OTHER_FID, "guild_id": GUILD_ID}))
        skipped = dict(self.m.__dict__.get("_last_member_skips", {}))
        _ = skipped  # reasons are printed; assert the DB state instead:
        self.assertFalse(self.kv.exists(
            "guild_members", {"guild_id": GUILD_ID, "user_id": 900004}))
        self.assertFalse(self.kv.exists(
            "guild_members", {"guild_id": GUILD_ID, "user_id": 12345}))
        g = self.kv.get("guilds", {"guild_id": GUILD_ID})
        self.assertEqual(g["member_count"], 101)
        self.assertIsNone(g["members_indexed_at"])

    def test_no_commit_before_end(self):
        add_message(self.db_path, 1000)
        self.m.import_channels([CH])
        self.assertEqual(self.kv.commits, 0)

    def test_dry_run_no_commit_no_upload(self):
        # mirrors main(): --dry-run forces upload_attachments False -> s3 None
        self.assertIsNone(self.m.s3)
        add_message(self.db_path, 1000)
        self.m.import_channels([CH])
        self.m.kv.rollback()  # what main() does on --dry-run
        self.assertEqual(self.kv.commits, 0)
        self.assertEqual(self.kv.rollbacks, 1)

    def test_unmapped_author_aborts(self):
        add_message(self.db_path, 1000, author=333)
        with self.assertRaises(SystemExit):
            self.m.import_channels([CH])

    def test_thread_in_backup_under_channel_aborts(self):
        db = sqlite3.connect(self.db_path)
        db.execute("INSERT INTO threads VALUES (555, ?, 't')", (CH,))
        db.commit()
        db.close()
        with self.assertRaises(SystemExit):
            self.m.import_channels([CH])


class TestRestoreMembersSelection(unittest.TestCase):
    def test_select_users(self):
        tmp = tempfile.mkdtemp()
        db_path = os.path.join(tmp, "backup.db")
        db = sqlite3.connect(db_path)
        db.execute("CREATE TABLE users (id INTEGER, username TEXT, type INTEGER)")
        db.executemany(
            "INSERT INTO users VALUES (?,?,?)",
            [(1, "newbie", 0), (2, "oldpal", 0), (3, "helperbot", 1),
             (4, "Deleted User", 0), (5, "nomap", 0)])
        db.commit()
        db.close()
        accounts = {"1": {"fluxer_user_id": 501, "username": "newbie",
                          "password": "x"},
                    "5": {"fluxer_user_id": 505, "username": "nomap",
                          "password": "x"}}
        sel, already, missing = rcm.select_users(db_path, {2}, accounts)
        self.assertEqual(already, [(2, "oldpal")])
        self.assertEqual(sorted(d for d, _, _ in sel), [1, 5])
        self.assertEqual(missing, [])  # 5 has an account in `accounts`
        # without 5 mapped it becomes missing
        _, _, missing = rcm.select_users(db_path, {2}, {"1": accounts["1"]})
        self.assertEqual(missing, [(5, "nomap")])


if __name__ == "__main__":
    unittest.main(verbosity=2)
