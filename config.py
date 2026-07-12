# SPDX-License-Identifier: AGPL-3.0-or-later
"""Configuration for the Discord -> Fluxer migration tool.

Everything needed to migrate one Discord backup into one Fluxer instance lives
in a single JSON file, passed with ``--config <file>``. Keep one file per
server. See ``config.example.json`` for the full shape.
"""

import json
import os
import sys
from dataclasses import dataclass, field


@dataclass
class Config:
    # --- Source ---
    backup_db: str = "backup.db"
    backup_dir: str = ""  # defaults to dirname(backup_db) when empty

    # --- Fluxer schema (set in config.json; from the Fluxer repo) ---
    schema_path: str = ""
    # Fluxer's emoji table (fluxer_app/src/media/data/emojis.json), used to
    # normalise unicode emojis (content + reactions) to Fluxer's canonical
    # surrogate. Empty = derive from schema_path.
    emoji_data_path: str = ""

    # --- Target Postgres (KV backend) ---
    pg_host: str = "127.0.0.1"
    pg_port: int = 5432
    pg_database: str = "fluxer"
    pg_user: str = "fluxer"
    pg_password: str = "fluxer"
    kv_table: str = "fluxer_kv"

    # --- Target S3 (attachments) ---
    s3_endpoint: str = "http://127.0.0.1:8333"
    s3_region: str = "us-east-1"
    s3_access_key: str = "fluxer"
    s3_secret_key: str = "fluxer-secret"
    s3_bucket_cdn: str = "fluxer"
    s3_force_path_style: bool = True

    # --- Migration target identifiers ---
    guild_owner_user_id: int = 0  # Fluxer user id that owns the imported guild

    # --- Strategies ---
    thread_mode: str = "channel"
    thread_name_prefix: str = "🧵 "
    reaction_mode: str = "real"  # 'real' | 'owner' | 'skip'
    upload_attachments: bool = True
    # author not in the user map: 'skip' the message, or attribute to 'owner'
    unmapped_author_mode: str = "skip"
    # Convert Tenor GIF embeds into KLIPY gifv embeds (Fluxer's active GIF
    # provider) via KLIPY search. The media stays on KLIPY's CDN, so nothing is
    # uploaded to the instance. Requires a KLIPY API key.
    convert_gifs: bool = False
    klipy_api_key: str = ""       # KLIPY API key (inline in the config)
    klipy_customer_id: str = ""   # KLIPY analytics customer id; defaults to owner id
    # Disk cache of resolved query -> embed (dedupes reposts, makes re-runs free).
    # Defaults to <backup_dir>/klipy_cache.json.
    klipy_cache_path: str = ""
    # Fluxer media-proxy signing (FLUXER_MEDIA_PROXY_PUBLIC_ENDPOINT / _SECRET_KEY
    # in the deployment). Required so converted GIF embeds carry a signed
    # proxy_url; without it Fluxer's client won't render the media.
    media_proxy_endpoint: str = ""
    media_proxy_secret: str = ""

    # --- User map ---
    # Parsed mapping discord_id -> fluxer_user_id (only entries with a non-zero id).
    user_map: dict = field(default_factory=dict)
    # Raw user_map object from the config (with names), for gen-user-map rewrites.
    user_map_raw: dict = field(default_factory=dict)
    # Path of the loaded config file (so commands can rewrite it in place).
    config_path: str = ""

    @property
    def pg_dsn(self):
        return (
            f"host={self.pg_host} port={self.pg_port} dbname={self.pg_database} "
            f"user={self.pg_user} password={self.pg_password}"
        )


def _section(data, name):
    val = data.get(name, {})
    return val if isinstance(val, dict) else {}


def load_config(config_path):
    """Load a single JSON config file into a Config. Missing keys fall back to
    dataclass defaults; the inside-compose host 'postgres' is rewritten to
    localhost for host-side runs."""
    if not config_path:
        sys.exit("No config file given. Pass --config <file>.")
    if not os.path.exists(config_path):
        sys.exit(f"Config file not found: {config_path}")
    try:
        with open(config_path) as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        sys.exit(f"Config file {config_path} is not valid JSON: {e}")

    c = Config()
    c.config_path = config_path

    backup = _section(data, "backup")
    c.backup_db = backup.get("db", c.backup_db)
    c.backup_dir = backup.get("dir") or os.path.dirname(c.backup_db) or c.backup_dir

    c.schema_path = data.get("schema_path", c.schema_path)
    c.emoji_data_path = data.get("emoji_data_path", c.emoji_data_path)

    pg = _section(data, "postgres")
    c.pg_host = pg.get("host", c.pg_host)
    if c.pg_host == "postgres":  # inside-compose hostname -> localhost for host runs
        c.pg_host = "127.0.0.1"
    c.pg_port = int(pg.get("port", c.pg_port))
    c.pg_database = pg.get("database", c.pg_database)
    c.pg_user = pg.get("username", c.pg_user)
    c.pg_password = pg.get("password", c.pg_password)
    c.kv_table = pg.get("kv_table", c.kv_table)

    s3 = _section(data, "s3")
    c.s3_endpoint = s3.get("endpoint", c.s3_endpoint)
    c.s3_region = s3.get("region", c.s3_region)
    c.s3_access_key = s3.get("access_key", c.s3_access_key)
    c.s3_secret_key = s3.get("secret_key", c.s3_secret_key)
    c.s3_bucket_cdn = s3.get("bucket_cdn", c.s3_bucket_cdn)
    c.s3_force_path_style = bool(s3.get("force_path_style", c.s3_force_path_style))

    if data.get("owner_user_id"):
        c.guild_owner_user_id = int(data["owner_user_id"])

    opts = _section(data, "options")
    c.thread_mode = opts.get("thread_mode", c.thread_mode)
    c.thread_name_prefix = opts.get("thread_name_prefix", c.thread_name_prefix)
    c.reaction_mode = opts.get("reaction_mode", c.reaction_mode)
    c.unmapped_author_mode = opts.get("unmapped_author_mode", c.unmapped_author_mode)
    c.upload_attachments = bool(opts.get("upload_attachments", c.upload_attachments))

    # GIF (Tenor -> KLIPY) conversion.
    c.convert_gifs = bool(opts.get("convert_gifs", c.convert_gifs))
    c.klipy_customer_id = opts.get("klipy_customer_id", c.klipy_customer_id) \
        or (str(c.guild_owner_user_id) if c.guild_owner_user_id else "")
    c.klipy_cache_path = opts.get("klipy_cache_path", c.klipy_cache_path) \
        or os.path.join(c.backup_dir, "klipy_cache.json")
    c.klipy_api_key = (data.get("klipy_api_key", "") or "").strip()

    # Media-proxy signing: top-level config keys, falling back to the same env
    # vars the Fluxer deployment uses.
    c.media_proxy_endpoint = (data.get("media_proxy_endpoint", "") or ""
                              or os.environ.get("FLUXER_MEDIA_PROXY_PUBLIC_ENDPOINT", "")
                              or os.environ.get("FLUXER_MEDIA_ENDPOINT", "")).strip()
    c.media_proxy_secret = (data.get("media_proxy_secret", "") or ""
                            or os.environ.get("FLUXER_MEDIA_PROXY_SECRET_KEY", "")).strip()

    # user_map: { "<discord_id>": {"name": "...", "fluxer_user_id": <int>} }
    # Also accept the shorthand { "<discord_id>": <fluxer_user_id> }.
    c.user_map_raw = _section(data, "user_map")
    mapping = {}
    for did, entry in c.user_map_raw.items():
        if not str(did).isdigit():
            continue  # skip _comment and other non-id keys
        fid = entry.get("fluxer_user_id", 0) if isinstance(entry, dict) else entry
        if fid:
            mapping[int(did)] = int(fid)
    c.user_map = mapping

    return c


def write_user_map(config_path, user_map_obj):
    """Merge a user_map object into an existing config file, in place,
    preserving everything else and key order."""
    with open(config_path) as f:
        data = json.load(f)
    data["user_map"] = user_map_obj
    with open(config_path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
