# CLAUDE.md — context for AI agents working on flushcord

flushcord migrates a Discord backup (`backup.db`, SQLite) into a self-hosted
**Fluxer** instance by writing **directly** into Fluxer's Postgres KV table
(`fluxer_kv`). The output must be *indistinguishable* from rows the Fluxer app
itself writes. See [`README.md`](README.md) for how to run it — this file is
about how to change it safely.

## ⚠️ Governing rule: parity, don't guess

Almost every module here mirrors a specific piece of Fluxer's source. The
correctness bar is **byte-for-byte parity** with Fluxer (KV encoding, key
building, snowflake buckets, schema, emoji canonicalization, embed shapes, media
URLs). None of this is guessable.

**Before you change anything touching encoding, keys/IDs, schema, emojis,
embeds, or media URLs: open the Fluxer source file that owns that behavior (see
the map below) and match it.** Do not infer the format from examples alone.

Cautionary tale: migrated GIFs once rendered as bare links because their embeds
lacked a signed `proxy_url`. The client silently drops media that fails
`isValidMedia` — a rule only visible in Fluxer's source, not in any example.

## 🔎 First: secure the reference implementation

This tool was written by reading Fluxer's source. You need a **local Fluxer
checkout** to make correct changes.

**Before doing any parity-affecting work, confirm where the contributor's local
Fluxer checkout is.** If it isn't recorded in `CLAUDE.local.md`, **ask the user
for its path** and offer to record it there. Treat that checkout as read-only
reference — never modify it.

## Validated against Fluxer

Commit **`e958cfe3`** (nearest tag `2026.710.164001`, branch `main`,
2026-07-10). If the contributor's Fluxer is newer, re-read the encoding/schema/
embed files below and check for drift before trusting parity.

## Fluxer source-of-truth map

Paths are relative to the local Fluxer checkout.

| Concern | flushcord | Fluxer source of truth |
|---|---|---|
| KV value + key tag-encoding | `kv_encoding.py`, `kvstore.py` | `fluxer_api/src/api/database/PostgresKvQueryExecutor.ts`; `fluxer_svc/src/postgres.rs` |
| Snowflake IDs + 10-day buckets | `snowflake.py` | `packages/snowflake/src/SnowflakeBuckets.ts` |
| Schema / table specs | `schema.py` | `tools/dev/cassandra_target_schema.json` |
| ThumbHash placeholder | `thumbhash.py` | `fluxer_media_proxy/src/thumbhash.rs` (MAX_DIM=100) |
| Attachment CDN key | `s3uploader.py` | `makeAttachmentCdnKey()` in `fluxer_api/src/api/channel/services/message/MessageHelpers.ts` |
| Allowed message types | `migrate.py` (`ALLOWED_MESSAGE_TYPES`) | `MessageTypeSchema`; search serializer calls `contentType.trim()` (so content type must never be null) |
| Unicode emoji FE0F form | `unicode_emojis.py` | `fluxer_app/src/media/data/emojis.json` |
| GIF → KLIPY + proxy-URL signing | `klipy.py` | `fluxer_gifs/src/media_proxy.rs` (`external_proxy_url`); `fluxer_gifs/src/klipy.rs` |
| YouTube link → `video` embed | `youtube.py` | `fluxer_unfurl/src/resolvers/youtube.rs`; limits in `fluxer_unfurl/src/{embed_normalizer,text_limits}.rs`; thumbnail metadata via `/_metadata` (`fluxer_unfurl/src/media_proxy.rs`) |
| **Embed render contract** (easy to miss) | embeds built in `migrate.py` / `klipy.py` / `youtube.py` | `fluxer_app/src/features/channel/components/embeds/channel_embed/ChannelEmbedShared.ts` — `isValidMedia` requires `proxy_url` **and** `url` **and** `width` **and** `height`; `.../EmbedMediaRenderer.tsx`; `packages/schema/src/domains/message/EmbedSchemas.ts` |

`README.md`'s "Module layout" has a one-line summary of each module; this table
adds the exact Fluxer file to read when touching it.

### Persisted embed ≠ served embed

The row you write is **not** what the client sees, and conflating the two will
send you chasing the wrong field:

- **`proxy_url` is never persisted.** `EmbedService.mapResponseMedia`
  (`fluxer_api/.../infrastructure/EmbedService.ts`) strips it before the row is
  written, and `fluxer_messages` re-signs it from `media.url` on *every* read
  (`map_embed_media` → `external_media_proxy_url` in `src/shard_impl.rs`). So a
  `proxy_url` you store is inert — the server overwrites it. What a stored media
  object actually needs to render is **`url` + `width` + `height`**; the server
  supplies the `proxy_url` half of `isValidMedia` on its own.
- Verify embed shape against a **real row Fluxer wrote** (post the link in the
  instance, then read `fluxer_kv`), not against the resolver source — a couple of
  fields get normalized on the way in (e.g. `provider.url` gains a trailing slash).

## Safety invariants

- **`backup.db` is read-only.** Never write to it.
- **Never create users.** Discord users are mapped to admin-created Fluxer user
  ids via `user_map`. Author/mentions/membership all go through that map.
- **Reuse Discord snowflake IDs verbatim** (Discord and Fluxer share the
  2015-01-01 epoch) — this preserves timestamps, ordering, and bucket assignment.
- **Secrets live in gitignored per-server `*.json`** (e.g. `myserver.json`):
  Postgres password, S3 secret, KLIPY key, media-proxy secret. **Treat them as
  private — never echo, quote, paste into chat, or commit them.**
  `config.example.json` is the only tracked JSON.
- **Don't invent fields Fluxer's schema won't accept.** A bad embed/message can
  500 an entire channel fetch (this is why message types are filtered).

## Dev workflow

- Run everything with `.venv/bin/python` (deps in `requirements.txt`).
- **There is no test suite.** Verify against a scratch/dev Fluxer instance:
  `preflight` → `migrate --dry-run` → `verify`. Never run `migrate`/`purge`
  against production without a `--dry-run` first.
- **New conversion? Ship the repair command with it.** Anything that enriches
  rows (GIFs, member index, YouTube embeds) needs an in-place fixer for guilds
  already migrated — re-migrating is not an option once people are using the
  instance. See `fix-gifs`, `reindex-members`, `fix-youtube` in `migrate.py`, and
  README's "Repairing an already-migrated guild". They rewrite one field of the
  affected rows and must be idempotent.

## Machine-specific context

Personal paths (your Fluxer checkout, which config file you use) go in
`CLAUDE.local.md`, which Claude Code also auto-loads and which is gitignored.
Create it once: `cp CLAUDE.local.example.md CLAUDE.local.md` and fill it in.
