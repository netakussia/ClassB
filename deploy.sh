#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REMOTE="${DEPLOY_REMOTE:-neta@192.168.2.67}"
REMOTE_DIR="${DEPLOY_REMOTE_DIR:-/home/neta/server/rc-215}"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
REMOTE_ARCHIVE="/home/neta/.cache/rc-215-deploy/${TIMESTAMP}.tar.gz"
TEMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/rc-215-deploy.XXXXXX")"
ARCHIVE="${TEMP_DIR}/rc-215-deploy.tar.gz"

cleanup() {
    rm -rf -- "$TEMP_DIR"
}
trap cleanup EXIT

for command_name in ssh scp tar python3; do
    if ! command -v "$command_name" >/dev/null 2>&1; then
        printf 'Required command not found: %s\n' "$command_name" >&2
        exit 1
    fi
done

printf 'Creating server-side backups before deployment...\n'
ssh "$REMOTE" bash -s -- "$REMOTE_DIR" "$TIMESTAMP" <<'REMOTE_BACKUP'
set -Eeuo pipefail
umask 077

app_dir="$1"
timestamp="$2"
database_path="${app_dir}/data/bot.db"
students_path="${app_dir}/config/students.yaml"
backup_dir="${HOME}/backups/rc-215/${timestamp}"

test -d "$app_dir" || { printf 'Application directory not found: %s\n' "$app_dir" >&2; exit 1; }
test -f "$database_path" || { printf 'Database not found: %s\n' "$database_path" >&2; exit 1; }
test -f "$students_path" || { printf 'Server roster not found: %s\n' "$students_path" >&2; exit 1; }
test -r "$database_path" || { printf 'Database is not readable by the SSH user: %s\n' "$database_path" >&2; exit 1; }
test -r "$students_path" || { printf 'Roster is not readable by the SSH user: %s\n' "$students_path" >&2; exit 1; }

mkdir -p "$backup_dir"
mkdir -p "${HOME}/.cache/rc-215-deploy"
chmod 700 "${HOME}/.cache/rc-215-deploy"
tar -czf "${backup_dir}/data.tar.gz" -C "$app_dir" data
tar -tzf "${backup_dir}/data.tar.gz" | grep -Fxq 'data/'
tar -tzf "${backup_dir}/data.tar.gz" | grep -Fxq 'data/bot.db'
python3 - "$database_path" "${backup_dir}/bot.db" <<'PYTHON_BACKUP'
import sqlite3
import sys

source = sqlite3.connect(sys.argv[1], timeout=30)
destination = sqlite3.connect(sys.argv[2])
try:
    source.backup(destination)
    result = destination.execute("PRAGMA integrity_check").fetchone()[0]
    if result != "ok":
        raise SystemExit(f"SQLite backup integrity check failed: {result}")
finally:
    destination.close()
    source.close()
PYTHON_BACKUP
cp -p -- "$students_path" "${backup_dir}/students.yaml"
cmp -s -- "$students_path" "${backup_dir}/students.yaml"
printf 'Backups created: %s\n' "$backup_dir"
REMOTE_BACKUP

printf 'Creating source archive...\n'
tar -czf "$ARCHIVE" \
    --exclude='data' \
    --exclude='data/*' \
    --exclude='logs' \
    --exclude='logs/*' \
    --exclude='__pycache__' \
    --exclude='*/__pycache__' \
    --exclude='.env' \
    --exclude='config/students.yaml' \
    --exclude='.git' \
    -C "$PROJECT_DIR" \
    bot database tests config main.py config_loader.py requirements.txt \
    Dockerfile docker-compose.yml .dockerignore .gitignore AI_CONTEXT.md

if tar -tzf "$ARCHIVE" | grep -Eq '(^|/)(data|logs|__pycache__)(/|$)|(^|/)\.env$|(^|/)students\.yaml$'; then
    printf 'Protected files unexpectedly found in the deployment archive. Aborting.\n' >&2
    exit 1
fi

printf 'Uploading archive to %s...\n' "$REMOTE"
scp "$ARCHIVE" "${REMOTE}:${REMOTE_ARCHIVE}"

printf 'Installing source overlay on the server...\n'
ssh "$REMOTE" bash -s -- "$REMOTE_DIR" "$REMOTE_ARCHIVE" <<'REMOTE_INSTALL'
set -Eeuo pipefail

app_dir="$1"
archive_path="$2"
test -d "$app_dir" || { printf 'Application directory not found: %s\n' "$app_dir" >&2; exit 1; }
test -f "$archive_path" || { printf 'Uploaded archive not found: %s\n' "$archive_path" >&2; exit 1; }

if tar -tzf "$archive_path" | grep -Eq '(^|/)(data|logs|__pycache__)(/|$)|(^|/)\.env$|(^|/)students\.yaml$'; then
    printf 'Protected files found in the uploaded archive. Refusing extraction.\n' >&2
    exit 1
fi

tar -xzf "$archive_path" -C "$app_dir"
rm -f -- "$archive_path"
printf 'Source overlay installed. Archive removed.\n'
REMOTE_INSTALL

printf 'Rebuilding and restarting the bot...\n'
ssh "$REMOTE" bash -s -- "$REMOTE_DIR" <<'REMOTE_RESTART'
set -Eeuo pipefail

app_dir="$1"
cd "$app_dir"
docker compose up -d --build --force-recreate bot
docker compose ps --status running --services | grep -Fxq bot || {
    printf 'Bot container did not reach the running state.\n' >&2
    docker compose ps
    exit 1
}
printf 'Bot container is running.\n'
REMOTE_RESTART

printf 'Deployment completed successfully.\n'