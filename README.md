# AI-token-analyzer

Lightweight Python CLI for snapshotting Codex token usage with `ccusage` and analyzing it from SQLite.

Run commands from this repository root:

```bash
cd /iopsstor/scratch/cscs/anunay/swissai/apertus_integration/codex_usage/AI-token-analyzer
uv sync
uv run ai-token-analyzer --help
```

By default, storage is relative to the current directory (`.`). When run from the repo root, generated runtime data lives in:

```text
./raw/codex_session_snapshots.jsonl
./latest/latest_codex_session.json
./latest/latest_codex_session.sha256
./db/codex_usage.sqlite
./logs/snapshot.log
./logs/collector.log
./locks/collector.lock
./locks/collector.pid
```

## Usage

Initialize storage:

```bash
uv run ai-token-analyzer init-storage
```

Take one immediate snapshot:

```bash
uv run ai-token-analyzer snapshot --print-status
```

Run the collector in the foreground, sleeping 60 seconds between snapshots:

```bash
uv run ai-token-analyzer run-loop --interval-seconds 60
```

Start the collector as a background process:

```bash
uv run ai-token-analyzer start-collector --interval-seconds 60
```

The background process stores its PID in `./locks/collector.pid` and appends loop output to `./logs/collector.log`. Each iteration runs:

```text
npx -y ccusage@latest codex session --json
```

Check the background collector and recent logs:

```bash
uv run ai-token-analyzer collector-status
```

Stop the background collector:

```bash
uv run ai-token-analyzer stop-collector
```

Analyze data:

```bash
uv run ai-token-analyzer analyze summary
uv run ai-token-analyzer analyze sessions --top 20 --sort cost
uv run ai-token-analyzer analyze models
uv run ai-token-analyzer analyze timeline --bucket hour
uv run ai-token-analyzer analyze timeline --bucket month
uv run ai-token-analyzer analyze latest
```

All user-facing analysis uses session activity timestamps from the stored data (`last_activity`, then `last_seen`, then `first_seen`) rather than snapshot ingestion time. Snapshot timestamps remain in storage only for audit/history.

`analyze timeline` supports `minute`, `hour`, `day`, and `month` buckets. It prints one row per bucket and model using session activity time, so historical imports group into the correct month instead of the current snapshot month.

Export data:

```bash
uv run ai-token-analyzer export --table sessions --format csv --out sessions.csv
uv run ai-token-analyzer export --table snapshot_deltas --format json --out deltas.json
```

Use `--out-dir PATH` on commands to store data somewhere else.

## Notes

The collector stores only changed snapshots unless `--force` is passed. Every stored snapshot is appended to JSONL evidence and written to SQLite. Unchanged snapshots record a `snapshot_events` row and a short log line without appending duplicate raw evidence.

Because Codex logs can be ephemeral in containers, start the background collector at the beginning of each container or session.
