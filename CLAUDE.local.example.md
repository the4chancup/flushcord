# CLAUDE.local.md — your machine-specific context (template)

Copy this to `CLAUDE.local.md` (gitignored, auto-loaded by Claude Code) and fill
in the blanks. It gives your agent the per-machine facts the shared `CLAUDE.md`
can't hardcode.

## Local Fluxer checkout (reference implementation)

Path to your local clone of the Fluxer source — the reference implementation this
tool mirrors. The agent should read from it (read-only) before making any
parity-affecting change; see the source-of-truth map in `CLAUDE.md`.

```
FLUXER_SRC = /absolute/path/to/fluxer
```

Your checked-out Fluxer revision (so the agent can spot drift from the ref that
`CLAUDE.md` pins):

```
FLUXER_REV = <output of: git -C <FLUXER_SRC> describe --tags --always>
```

## Config file for this server

Which per-server config you pass as `--config` (holds **live secrets** — keep it
private; never commit, echo, or paste its contents):

```
CONFIG = myserver.json
```

## Anything else agent-relevant to your setup

e.g. a non-default schema path, whether `ffprobe`/`ffmpeg` are installed, or that
you only run against a scratch instance.
