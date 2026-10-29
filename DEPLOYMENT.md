# Safe deployment and recovery

The deployment script is intentionally fail-closed. It has no built-in server
address. Before a separately authorized deployment, the owner must put the
exact approved SSH destination (one `user@host` or `host` per line) in the
ignored local file `.deploy-allowlist` and pass the same value explicitly as
`DEPLOY_REMOTE`. The script rejects malformed and non-allowlisted destinations.
It relies on normal SSH host-key verification; do not disable it.
If the running container was created with one additional Compose override,
pass its verified absolute path with `DEPLOY_COMPOSE_OVERRIDE`; the script
requires it to match the running container's Compose labels and uses the same
file set for preflight and restart.

The package is a source-only overlay: `bot/`, `database/`, `main.py`,
`config_loader.py`, and `requirements.txt`. It does not contain or replace
`.env`, any active or example configuration, the database, logs, Compose, the
Dockerfile, or user files. The install step validates each archive member
before extraction. The script does not use `docker compose down`, remove
containers, delete the uploaded archive, or remove volumes. The uploaded
source archive is retained as
`<application-directory>/.rc-215-deploy-<timestamp>.tar.gz`.

## Required pre-deployment backup

Before upload or overlay, the remote script creates a unique, mode-700
directory at:

```text
$HOME/backups/rc-215/<timestamp>/
```

It refuses to reuse an existing backup directory. `predeploy-files.tar.gz`
contains the existing `config/`, `data/`, `logs/`, `.env`, Compose/Docker build
files, documentation, tests, and source files that could be overlaid. This
preserves active YAML files and the full data volume. The archive is listed
and extracted into an isolated restore-check directory before continuing.
`predeploy-source.tar.gz` contains only the code paths needed for a source-only
rollback. If an active external Compose override is used, it is saved separately
as `predeploy-compose-override.tar.gz` and also tested by extraction.
`sqlite-bot.db` is a separate SQLite online backup made with the SQLite backup
API; source, backup, and an isolated restore rehearsal must all pass
`PRAGMA integrity_check`, or the deployment stops before upload. Backup files
are created with restrictive permissions. They remain on that server; arrange
an owner-approved off-host backup separately if required.
Automatic database backup is deliberately limited to the default
`data/bot.db` SQLite database and the single Compose `.env` file. A `DATABASE_URL`
override or additional Compose env-file source causes the script to abort; use a
separately reviewed backup procedure for any external database or env file.
Before creating backups, the script checks the effective Compose configuration
without printing its expanded environment. The `bot` service must mount the
backed-up `config/`, roster, `data/`, and `logs/` paths at the expected container
locations. Unexpected, named, or external volumes cause an abort rather than
assuming that `data/bot.db` is the active database.

If any prerequisite, archive check, or SQLite backup fails, `set -e` aborts
before upload, extraction, or container restart.
Symlinked backup sources are rejected so a backup cannot silently save only a
link instead of the active configuration or data.

## Schema and rollback

This release adds AI tables through SQLAlchemy `create_all()` and adds the
`users.is_banned` column with a false default. These are additive changes; the
existing application version can ignore the additional tables and column.
The existing homework de-duplication migration is unchanged by this release.
No automatic schema rollback is attempted.

The normal rollback is a code rollback, not a restore of user data or active
configuration. After separate owner authorization and after verifying the
selected backup, extract only the source members from
`predeploy-files.tar.gz` into the application directory, then rebuild/start the
bot with the existing Compose file. Do not extract `config/`, `data/`, `.env`,
or `logs/` as part of a routine code rollback.

Restoring `sqlite-bot.db` would discard database writes made after its backup.
Only do so after owner approval, stopping database writers under an approved
maintenance procedure, preserving a fresh copy of the current database, and
verifying both source and restored databases with `PRAGMA integrity_check`.
For a restore rehearsal, use an isolated copy of the application database, never
the production path. The equivalent SQLite operation is:

```python
import sqlite3

with sqlite3.connect("isolated-target.db") as target:
    with sqlite3.connect("sqlite-bot.db") as backup:
        backup.backup(target)
    assert target.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
```

Prefer a forward fix when possible. This repository does not run that restore
automatically.

No production connection or deployment is part of release preparation. The
owner must separately review the approved destination, backup location, and
maintenance/rollback decision before running `deploy.sh`.
