# flushcord — Discord → Fluxer migration tool

> [!Warning]
> This entire project was "vibe-coded" (this is likely the only paragraph I actually wrote (AI did fix my grammar since
> English is not my native language)). I'm normally pretty anti-AI and should be ashamed of doing such a thing, but
> Claude carried this one purely out of respect for my laziness (and my need to get this done fast). I am quite a
> privacy-aware person, and between Discord rolling out age verification and the EU pushing Chat Control, I just wanted
> to stay as private as humanly possible (which is where Fluxer comes in).

Migrates a Discord server backup (SQLite) into a self-hosted **Fluxer** instance,
preserving original message timestamps, ordering, authorship, channels,
threads, reactions, and attachments.

> **Generate the backup with the [flushcord fork of disco-reaper](https://github.com/flushcord/disco-reaper).**
> It records extra metadata this migration needs; a backup from the
> [official disco-reaper](https://github.com/rambros3d/disco-reaper) will be
> missing those fields. See [Prerequisites](#prerequisites).

## TL;DR

All configuration for one server lives in a single `config.json` (connection,
backup paths, owner, options, and the user map). Keep one file per server.

```bash
# 0. install
python -m venv .venv && .venv/bin/pip install -r requirements.txt

# 1. one config file per server: copy, then edit connection + backup paths + owner
cp config.example.json myserver.json

# 2. create the Fluxer users in the admin dashboard, then map them
.venv/bin/python migrate.py --config myserver.json gen-user-map   # fills user_map
#    edit myserver.json: set each user_map fluxer_user_id (0 = skip)

# 3. validate everything BEFORE writing (connectivity, schema, users, owner)
.venv/bin/python migrate.py --config myserver.json preflight

# 4. migrate  (preflight runs automatically and aborts on any problem)
.venv/bin/python migrate.py --config myserver.json migrate

# 5. confirm
.venv/bin/python migrate.py --config myserver.json verify

# reset for a clean re-run (DB + uploaded files). The Fluxer GUI "delete
# community" is incomplete and leaves orphan rows, so use this instead:
.venv/bin/python migrate.py --config myserver.json purge --s3
```

The owner (`owner_user_id` in the config, overridable with `--owner`) is the
Fluxer user id that owns the imported guild — one of the ids you mapped. It also
receives any messages whose Discord author wasn't mapped.

## How it works

Fluxer stores all data in a Cassandra-style data model. This deployment runs the
**Postgres KV backend** (`FLUXER_DATABASE_BACKEND=postgres`), where every logical
table is stored in a single `fluxer_kv` table as
`(table_name, partition_key, row_key, row_data jsonb)`. This tool writes rows
directly into that table, replicating Fluxer's exact value encoding
(`PostgresKvQueryExecutor.ts`), so migrated data is indistinguishable from data
written by the app.

Attachments are uploaded to Fluxer's S3/SeaweedFS CDN bucket under the same key
scheme the app uses (`attachments/{channel_id}/{attachment_id}/{filename}`).

### Key design decisions

- **IDs are reused, not regenerated.** Discord and Fluxer share the same
  snowflake epoch (2015-01-01), so Discord IDs are already valid Fluxer
  snowflakes. Reusing them preserves timestamps, ordering, and bucket
  assignment *exactly*. Guild, channel, role, message, and attachment IDs are
  reused verbatim.
- **Users are mapped, not created.** An admin creates the Fluxer users in the
  admin dashboard, then maps Discord user IDs → Fluxer user IDs in the config's
  `user_map`. Author, mentions, and membership all go through this map.
  **The mapped Fluxer users must exist** for members/messages to render
  (preflight checks this).
- **Threads → channels.** Fluxer has no native threads. Each Discord thread
  becomes a text channel (name prefixed with `🧵`), nested under the same
  category as its parent channel (`--thread`/`FLUSHCORD_THREAD_MODE`).
- **Reactions.** If the backup records the reacting users (`reactions.user_ids`),
  the default `real` mode reproduces them faithfully — one row per (emoji, user),
  mapping each reacting Discord user through the user map (unmapped reactors are
  dropped). For older backups that only have an aggregate count, `owner` mode
  records one reaction per emoji attributed to the guild owner, and `skip` omits
  reactions entirely. Custom-emoji reactions reuse their Discord emoji id
  verbatim.
- **Unicode emojis.** Discord stores built-in emojis as the bare surrogate
  (e.g. `🗣`), but Fluxer only renders the FE0F-qualified form (`🗣️`) — a bare
  one shows as plain, uncoloured text. Both message content and reactions are
  rewritten to Fluxer's exact canonical surrogate using Fluxer's own emoji table
  (`emoji_data_path`, derived from `schema_path` by default), so they render as
  Twemoji sprites and match what the native picker stores. Custom-emoji syntax
  (`<:name:id>`) and mentions are never touched.
- **GIFs (Tenor → KLIPY).** Discord embeds bare `tenor.com/view/...` links as
  animated GIFs. Tenor is shutting down, and Fluxer's active GIF provider is
  KLIPY, so with `convert_gifs` on each such embed is turned into a KLIPY `gifv`
  embed — exactly what a natively-sent GIF looks like, with the media served from
  KLIPY's CDN (nothing is uploaded to your instance). Tenor and KLIPY don't share
  ids, so the match is by KLIPY search using the text baked into the Tenor slug
  (kept in `backup.db`, so this works after Tenor is gone), re-ranked by
  positional-weighted token overlap. Resolutions are cached on disk
  (`klipy_cache_path`), so a GIF reposted many times costs one API call and
  re-runs are free. Unmatched GIFs keep their original Tenor link and are listed
  at the end. Requires a KLIPY API key (`klipy_api_key`,
  free from [partner.klipy.com](https://partner.klipy.com)). Use
  `gif-report` to preview the match rate without writing anything. Fluxer's
  client only renders embed media that carries a signed `proxy_url`, so
  `convert_gifs` also needs `media_proxy_endpoint` and `media_proxy_secret`
  (your deployment's `FLUXER_MEDIA_PROXY_PUBLIC_ENDPOINT` /
  `FLUXER_MEDIA_PROXY_SECRET_KEY`). If you migrated GIFs before setting these,
  run `fix-gifs` to backfill `proxy_url` on the existing embeds in place.
- **YouTube embeds.** Discord's backup keeps a YouTube link as a flat `rich`
  embed — a title, a description, and a thumbnail *url* with no dimensions —
  which Fluxer renders as a title card with no player and no image (the client
  drops media it can't size). Setting `youtube_api_key` (a [YouTube Data API v3
  key](https://console.cloud.google.com/apis/library/youtube.googleapis.com))
  makes the migration rebuild each one into the real `video` embed Fluxer's own
  unfurler would have written: inline player, sized thumbnail with a ThumbHash
  placeholder, channel as author. The thumbnail's metadata comes from the same
  media-proxy call Fluxer makes, so `media_proxy_endpoint` / `media_proxy_secret`
  should be set too. Resolutions are cached on disk (`youtube_cache_path`), so a
  video linked many times costs one API call (1 quota unit of the default
  10,000/day) and re-runs are free. Videos that no longer resolve (deleted,
  private) keep their original rich embed. Only links Discord itself unfurled are
  converted — one Discord suppressed (`<url>`, a code block) stays bare, as it was.
  If you migrated before setting the key, run `fix-youtube` to convert the
  existing embeds in place.
- **Join times.** The backup has no per-member join timestamp, so `joined_at`
  is set to each member's earliest message time (falling back to guild creation
  time).
- **Member search index.** Fluxer's *members page* is served from a search
  index, not from `guild_members` — the app indexes each member as it joins, a
  step direct KV writes bypass. Fluxer rebuilds the index by itself, but only
  while the guild still looks un-indexed, so the migration clears the guild's
  `members_indexed_at` marker to re-arm that. (The member *sidebar* is separate:
  the gateway builds it from `guild_members`, which is why members can show up
  there but be missing from the members page.) If a guild was indexed
  before/during its migration — e.g. someone opened the members page mid-run —
  its index is pinned to that stale snapshot and members appear missing or
  without roles; run `reindex-members` to fix it.

## Prerequisites

1. A Discord backup (`backup.db`) generated with the **flushcord fork** of
   disco-reaper: <https://github.com/flushcord/disco-reaper>. The fork writes
   additional metadata into the backup that flushcord relies on (which the
   [official disco-reaper](https://github.com/rambros3d/disco-reaper) does not
   capture), so a backup from the upstream tool will be missing fields this
   migration needs. Point `backup.db`/`dir` at the fork's output.
2. A running Fluxer instance using the Postgres KV backend, with Postgres and
   S3/SeaweedFS reachable from where you run this tool.
3. The Fluxer repo checked out, for its schema file
   (`tools/dev/cassandra_target_schema.json`) — point `schema_path` at it.
4. Admin-created Fluxer users for each Discord user you want to migrate.
5. Optional: `ffprobe`/`ffmpeg` on `PATH`, for inline video parity (dimensions,
   duration, placeholder). Without them, videos migrate as plain file rows.

## Setup

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Configuration

Everything for one server lives in a single JSON file passed with `--config`.
Copy `config.example.json` and edit it:

```bash
cp config.example.json myserver.json
```

Sections: `backup` (`db`, `dir`), `schema_path`, `klipy_api_key`,
`youtube_api_key`, `media_proxy_endpoint`, `media_proxy_secret`,
`postgres`, `s3`, `owner_user_id`, `options` (`thread_mode`,
`thread_name_prefix`, `reaction_mode`, `unmapped_author_mode`,
`upload_attachments`, `convert_gifs`), and `user_map`. The file holds
secrets (Postgres password, S3 key), so keep it private / out of version
control. Keep one file per server.

## Usage

### 1. Fill in the user map

After creating the Fluxer accounts in the admin dashboard:

```bash
.venv/bin/python migrate.py --config myserver.json gen-user-map
```

This populates the `user_map` section with every Discord user in the backup
(re-running preserves ids you've already filled in). Edit it to set each
`fluxer_user_id` — leave `0` to skip a user. The shorthand
`"<discord_id>": <fluxer_user_id>` is also accepted. Two Discord ids may map to
the same Fluxer id (e.g. someone with an old + new account).

### 2. Preflight

Validate everything *before* writing anything — connectivity, schema, backup,
user map, that every mapped/owner Fluxer user actually exists, and that an owner
is set:

```bash
.venv/bin/python migrate.py --config myserver.json preflight
```

It prints a checklist and exits non-zero on any fatal problem (e.g. a mapped
user id that doesn't exist in the instance). The same checks run automatically
at the start of `migrate` and abort it on failure.

### 3. Preview GIF conversion (optional)

With `convert_gifs` on, check how many Tenor links resolve to a KLIPY GIF
before migrating. This writes nothing to the database — it just resolves every
Tenor link and fills the cache, so the real migration is faster:

```bash
.venv/bin/python migrate.py --config myserver.json gif-report
```

### 4. Dry run, then migrate

```bash
# Dry run: does everything, then rolls back all DB writes (S3 uploads still happen).
.venv/bin/python migrate.py --config myserver.json migrate --dry-run

# Real run (runs preflight first)
.venv/bin/python migrate.py --config myserver.json migrate

# Options
#   --owner <id>            override config's owner_user_id
#   --no-attachments        skip S3 uploads (records only available files)
#   --reactions skip|real|owner  reaction strategy (default: real)
#   --dry-run               roll back DB writes at the end
#   --skip-preflight        bypass the pre-migration validation checks
```

### 5. Verify

```bash
.venv/bin/python migrate.py --config myserver.json verify
```

Prints Discord backup counts next to the migrated row counts in `fluxer_kv`.

## Repairing an already-migrated guild

**A normal `migrate` does all of this as it goes** — these commands exist to fix a
guild that was migrated *before* the relevant config was in place (or, for
members, one whose index got built mid-run). They're safe to re-run: each is
idempotent, and none of them touch message ids, timestamps, authorship or
content.

| Symptom in the app | Command |
|---|---|
| GIFs show as bare links instead of playing | `fix-gifs` |
| Members page shows too few members, or members without roles | `reindex-members` |
| YouTube links show as a title card with no player or image | `fix-youtube` |
| Guild has no icon (or still shows a manually-uploaded one) | `fix-icon` |

### GIFs render as bare links

```bash
.venv/bin/python migrate.py --config myserver.json fix-gifs
```

Repairs converted GIF embeds whose media lacks the dimensions Fluxer's client
needs to render it, and re-resolves degraded `rich` embeds back into proper
`gifv` ones. Needed only if `media_proxy_endpoint` / `media_proxy_secret` weren't
set at migration time.

### Members missing (or role-less) on the members page

```bash
.venv/bin/python migrate.py --config myserver.json reindex-members
```

Clears the guild's `members_indexed_at` and resets `member_count` from the rows
actually present, so Fluxer queues its own member-indexing job the next time
someone opens the members page. Needed only if the guild's member index was built
before/during the migration — e.g. someone opened the members page mid-run, which
pins the index to that partial snapshot permanently.

### YouTube links showing as a bare title card

```bash
.venv/bin/python migrate.py --config myserver.json fix-youtube --dry-run
.venv/bin/python migrate.py --config myserver.json fix-youtube
```

Rebuilds migrated YouTube embeds into real Fluxer `video` embeds (inline player,
sized thumbnail with a ThumbHash), rewriting only the `embeds` field of each
affected message. Needed only if the guild was migrated before `youtube_api_key`
was set. Videos that no longer resolve (deleted, private) keep their existing
embed. `--dry-run` reports what it would change without writing, and fills the
resolution cache, so the real run costs no extra API quota.

### Guild icon missing

```bash
.venv/bin/python migrate.py --config myserver.json fix-icon --dry-run
.venv/bin/python migrate.py --config myserver.json fix-icon
```

Uploads the backup's `server_assets/server_icon.png` the way Fluxer itself does
(metadata-stripped PNG at `icons/<guild>/<md5[:8]>`), sets `icon_hash` on the
guild row, and deletes the previously uploaded icon object if it differs. Needed
for guilds migrated before icons were imported, or to replace a hand-uploaded
temporary icon. `--dry-run` reports the old/new hash without writing. Reload the
guild afterwards — the gateway caches guild state.

## Resetting for a clean re-run

```bash
.venv/bin/python migrate.py --config myserver.json purge --s3        # delete DB rows + uploaded files
.venv/bin/python migrate.py --config myserver.json purge --dry-run   # preview what would be deleted
```

`purge` is scoped to this backup's guild: it leaves other guilds, user accounts,
and manually-created emojis/stickers untouched. Prefer it over the Fluxer GUI's
"delete community", which is incomplete and leaves orphan channel/state/sticker
rows behind (there is no background worker that cleans them up later).

## What gets migrated

| Discord          | Fluxer table(s)                                                                    |
|------------------|------------------------------------------------------------------------------------|
| guild_profile    | `guilds`                                                                           |
| roles            | `guild_roles`                                                                      |
| users (mapped)   | `guild_members`, `guild_members_by_user_id`                                        |
| channels         | `channels`, `channels_by_guild_id`                                                 |
| threads          | `channels` (as text channels)                                                      |
| permissions      | `channels.permission_overwrites`                                                   |
| messages         | `messages`, `messages_by_author_id_v2`, `channel_message_buckets`, `channel_state` |
| attachments      | S3 CDN object + `attachment_lookup` + `messages.attachments`                       |
| embeds           | `messages.embeds`                                                                  |
| message_stickers | `messages.sticker_items`                                                           |
| reactions        | `message_reactions` (see reaction note)                                            |
| pinned messages  | `channel_pins`                                                                     |
| custom emojis    | `guild_emojis`, `guild_emojis_by_emoji_id` + S3 `emojis/{id}`                      |
| stickers         | `guild_stickers`, `guild_stickers_by_sticker_id` + S3 `stickers/{id}`              |

### Image & video parity

Migrated media attachments reach full parity with natively-uploaded ones:

- **Dimensions** (`width`/`height`) are read from the file on disk — Pillow for
  images, ffprobe for video — so the client renders an inline preview / video
  player instead of a plain file row.
- **Placeholder** is a ThumbHash (`thumbhash.py`, base64) — computed from the
  image, or from a representative video frame (via ffmpeg) — giving the blur-up
  effect while the media loads, matching Fluxer's media-proxy pipeline (downscaled
  to max-dim 100, RGBA).
- **Video duration** is read with ffprobe so the player shows the length.
- **Animated** images (GIF/APNG/animated WebP) get the `IS_ANIMATED` flag.

Video parity needs `ffprobe`/`ffmpeg` on `PATH`; without them, videos still
migrate but render as plain file attachments (a one-time warning is printed).

Custom emojis and stickers come from the backup's `server_assets` table (files
in the `server_assets/` folder). Their original Discord IDs are reused, images
are uploaded to the CDN at `emojis/{id}` / `stickers/{id}` (raw bytes; the media
proxy transcodes to `.webp` on serve), and `creator_id` is set to the guild
owner (the backup has no per-asset creator).

## Module layout

- `thumbhash.py` — ThumbHash placeholder generation (mirrors `fluxer_media_proxy`)
- `snowflake.py` — snowflake ID + bucket math (mirrors `packages/snowflake`)
- `kv_encoding.py` — tagged-JSON value encoding (mirrors `PostgresKvQueryExecutor`)
- `schema.py` — loads `cassandra_target_schema.json`, parses primary/partition keys
- `kvstore.py` — psycopg2 upsert/patch into `fluxer_kv`
- `unicode_emojis.py` — normalises unicode emojis (content + reactions) to Fluxer's canonical surrogate
- `discord_db.py` — read-only Discord backup access
- `klipy.py` — Tenor → KLIPY GIF conversion (mirrors `fluxer_gifs`)
- `youtube.py` — YouTube link → `video` embed (mirrors `fluxer_unfurl`'s resolver)
- `s3uploader.py` — attachment uploads to the CDN bucket
- `config.py` — loads the per-server `config.json`
- `migrate.py` — orchestrator + CLI

## Contributing (and working with an AI agent)

This tool's correctness rests on **byte-for-byte parity with Fluxer's internals**,
so development needs a **local Fluxer checkout** as the reference implementation
(not just for the schema file). [`CLAUDE.md`](CLAUDE.md) captures the parity rule,
a Fluxer source-of-truth map (which Fluxer file owns each behavior), the safety
invariants, and the dev/verify workflow — Claude Code loads it automatically.

Before your first change, record your machine-specific paths so your agent knows
where your Fluxer checkout is:

```bash
cp CLAUDE.local.example.md CLAUDE.local.md   # gitignored; fill in FLUXER_SRC etc.
```

There is no test suite; verify changes against a scratch instance with
`preflight` → `migrate --dry-run` → `verify`.

## Limitations

- Guild icon/banner are not uploaded (avoids broken asset references).
- Reaction reactors are only recovered when the backup includes `user_ids`;
  otherwise fall back to `owner`/`skip`.
- Emoji/sticker `creator_id` is set to the guild owner (not in the backup).
- Cross-channel reply references assume the reply target is in the same channel.
- MIDI files are tagged `audio/*` by Discord but can't play in a browser, so
  they're served as plain downloadable files (content type demoted) rather than
  a dead audio player.

## License

Licensed under the **GNU Affero General Public License v3.0 or later**
(AGPL-3.0-or-later) — see [`LICENSE`](LICENSE). This tool replicates Fluxer's
data encoding by design and tracks its (AGPL) schema, so it carries the same
license for consistency. flushcord is an independent project and is not
affiliated with or endorsed by Fluxer or Discord.
