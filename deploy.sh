#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
: "${DEPLOY_REMOTE:?Set DEPLOY_REMOTE explicitly to an allowlisted SSH destination}"
REMOTE="$DEPLOY_REMOTE"
ALLOWLIST="${PROJECT_DIR}/.deploy-allowlist"

if [[ ! "$REMOTE" =~ ^([A-Za-z_][A-Za-z0-9._-]*@)?[A-Za-z0-9][A-Za-z0-9.-]*$ ]]; then
    printf 'DEPLOY_REMOTE has an invalid SSH destination format.\n' >&2
    exit 2
fi
if [[ ! -f "$ALLOWLIST" || -L "$ALLOWLIST" || ! -r "$ALLOWLIST" ]]; then
    printf 'Missing readable deployment allowlist: %s\n' "$ALLOWLIST" >&2
    exit 2
fi
if ! grep -Fqx -- "$REMOTE" "$ALLOWLIST"; then
    printf 'DEPLOY_REMOTE is not present in the deployment allowlist.\n' >&2
    exit 2
fi

REMOTE_DIR="${DEPLOY_REMOTE_DIR:-/home/neta/server/rc-215}"
if [[ ! "$REMOTE_DIR" =~ ^/[A-Za-z0-9._/-]+$ ]] \
    || [[ "$REMOTE_DIR" == "/" || "$REMOTE_DIR" == */ \
        || "$REMOTE_DIR" == *"//"* || "$REMOTE_DIR" == *"/../"* \
        || "$REMOTE_DIR" == */.. || "$REMOTE_DIR" == *"/./"* \
        || "$REMOTE_DIR" == */. ]]; then
    printf 'DEPLOY_REMOTE_DIR must be a normalized absolute path without shell metacharacters.\n' >&2
    exit 2
fi

TIMESTAMP="$(date +%Y%m%d_%H%M%S_%N)"
REMOTE_ARCHIVE="${REMOTE_DIR}/.rc-215-deploy-${TIMESTAMP}.tar.gz"
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

printf 'Creating and validating the runtime source archive...\n'
python3 "$PROJECT_DIR/deploy_package.py" "$PROJECT_DIR" "$ARCHIVE"
if tar -tzf "$ARCHIVE" | grep -Eq '(^|/)(data|logs|__pycache__)(/|$)|(^|/)\.env([^/]*)(/|$)|(^|/)students\.yaml$|^config/|^docker-compose\.yml$|^Dockerfile$'; then
    printf 'Protected or operational files unexpectedly found in the deployment archive. Aborting.\n' >&2
    exit 1
fi

printf 'Creating server-side backups before upload or overlay...\n'
ssh "$REMOTE" bash -s -- "$REMOTE_DIR" "$TIMESTAMP" "$REMOTE_ARCHIVE" <<'REMOTE_BACKUP'
set -Eeuo pipefail
umask 077

app_dir="$1"
timestamp="$2"
remote_archive="$3"
database_path="${app_dir}/data/bot.db"
backup_root="${HOME}/backups/rc-215"
backup_dir="${backup_root}/${timestamp}"

test -d "$app_dir" || { printf 'Application directory not found.\n' >&2; exit 1; }
test ! -L "$app_dir" || { printf 'Application directory must not be a symlink.\n' >&2; exit 1; }
test -d "${app_dir}/config" || { printf 'Application config directory not found.\n' >&2; exit 1; }
test ! -L "${app_dir}/config" || { printf 'Application config directory must not be a symlink.\n' >&2; exit 1; }
test -d "${app_dir}/data" || { printf 'Application data directory not found.\n' >&2; exit 1; }
test ! -L "${app_dir}/data" || { printf 'Application data directory must not be a symlink.\n' >&2; exit 1; }
test -f "$database_path" || { printf 'Application database not found.\n' >&2; exit 1; }
test ! -L "$database_path" || { printf 'Application database must not be a symlink.\n' >&2; exit 1; }
test -f "${app_dir}/config/students.yaml" || { printf 'Server roster not found.\n' >&2; exit 1; }
test ! -L "${app_dir}/config/students.yaml" || { printf 'Server roster must not be a symlink.\n' >&2; exit 1; }
test -f "${app_dir}/docker-compose.yml" || { printf 'Server Compose file not found.\n' >&2; exit 1; }
test ! -L "${app_dir}/docker-compose.yml" || { printf 'Server Compose file must not be a symlink.\n' >&2; exit 1; }
test -f "${app_dir}/.env" || { printf 'Server environment file not found.\n' >&2; exit 1; }
test ! -L "${app_dir}/.env" || { printf 'Server environment file must not be a symlink.\n' >&2; exit 1; }
test -r "$database_path" || { printf 'Application database is not readable.\n' >&2; exit 1; }
test -r "${app_dir}/config" || { printf 'Application config is not readable.\n' >&2; exit 1; }
test -r "${app_dir}/.env" || { printf 'Server environment file is not readable.\n' >&2; exit 1; }
if grep -Eq '^[[:space:]]*(export[[:space:]]+)?DATABASE_URL[[:space:]]*=' "${app_dir}/.env" \
    || grep -q 'DATABASE_URL' "${app_dir}/docker-compose.yml"; then
    printf 'A DATABASE_URL override is present; automatic backup only supports data/bot.db. Aborting.\n' >&2
    exit 1
fi
python3 - "${app_dir}/docker-compose.yml" <<'PYTHON_ENV_FILE_CHECK'
import re
import sys

lines = open(sys.argv[1], encoding="utf-8").read().splitlines()
env_files = []
for index, line in enumerate(lines):
    match = re.match(r"^(\s*)env_file\s*:\s*(.*?)\s*(?:#.*)?$", line)
    if match is None:
        continue
    indentation = len(match.group(1))
    inline_value = match.group(2).strip().strip("\"'")
    if inline_value:
        env_files.append(inline_value)
        continue
    for candidate in lines[index + 1:]:
        stripped = candidate.strip()
        if not stripped or stripped.startswith("#"):
            continue
        candidate_indent = len(candidate) - len(candidate.lstrip())
        if candidate_indent <= indentation:
            break
        entry = re.match(r"^\s*-\s*(.*?)\s*(?:#.*)?$", candidate)
        if entry is None:
            raise SystemExit(
                "Compose environment-file format cannot be verified; refusing deployment."
            )
        env_files.append(entry.group(1).strip().strip("\"'"))
if env_files != [".env"]:
    raise SystemExit(
        "Compose must use only the backed-up .env file; refusing deployment."
    )
PYTHON_ENV_FILE_CHECK

python3 - "$app_dir" <<'PYTHON_COMPOSE_CHECK'
import json
from pathlib import Path
import subprocess
import sys

app_dir = Path(sys.argv[1]).resolve()
try:
    result = subprocess.run(
        ["docker", "compose", "config", "--format", "json"],
        cwd=app_dir,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    compose = json.loads(result.stdout)
except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
    raise SystemExit(
        "Could not validate effective Compose configuration; refusing deployment."
    ) from None

service = compose.get("services", {}).get("bot")
if not isinstance(service, dict):
    raise SystemExit("Expected Compose bot service is missing; refusing deployment.")

def resolve_path(value):
    path = Path(value)
    return (path if path.is_absolute() else app_dir / path).resolve()

env_files = service.get("env_file", [])
normalized_env_files = []
for entry in env_files:
    path = entry.get("path") if isinstance(entry, dict) else entry
    if not isinstance(path, str):
        raise SystemExit("Compose environment-file format is unsupported; refusing deployment.")
    normalized_env_files.append(resolve_path(path))
if normalized_env_files != [(app_dir / ".env").resolve()]:
    raise SystemExit("Compose must use only the backed-up .env file; refusing deployment.")

environment = service.get("environment", {})
if isinstance(environment, dict) and "DATABASE_URL" in environment:
    raise SystemExit(
        "A DATABASE_URL override is present; automatic backup only supports data/bot.db."
    )
if isinstance(environment, list) and any(
    str(value).split("=", maxsplit=1)[0] == "DATABASE_URL" for value in environment
):
    raise SystemExit(
        "A DATABASE_URL override is present; automatic backup only supports data/bot.db."
    )

expected_mounts = {
    "/app/config": ((app_dir / "config").resolve(), True),
    "/app/config/students.yaml": ((app_dir / "config/students.yaml").resolve(), False),
    "/app/data": ((app_dir / "data").resolve(), False),
    "/app/logs": ((app_dir / "logs").resolve(), False),
}
actual_mounts = {}
for mount in service.get("volumes", []):
    if not isinstance(mount, dict) or mount.get("type") != "bind":
        raise SystemExit("Unexpected Compose volume; refusing deployment.")
    target = mount.get("target")
    source = mount.get("source")
    if not isinstance(target, str) or not isinstance(source, str):
        raise SystemExit("Unverifiable Compose volume; refusing deployment.")
    if target in actual_mounts:
        raise SystemExit("Duplicate Compose volume target; refusing deployment.")
    actual_mounts[target] = (
        resolve_path(source),
        bool(mount.get("read_only", False)),
    )

if actual_mounts != expected_mounts:
    raise SystemExit(
        "Compose volumes do not match the backed-up configuration and data paths; "
        "refusing deployment."
    )
PYTHON_COMPOSE_CHECK

mkdir -p "$backup_root"
test ! -L "$backup_root" || { printf 'Backup root must not be a symlink.\n' >&2; exit 1; }
test ! -e "$backup_dir" || { printf 'Backup destination already exists; refusing to overwrite it.\n' >&2; exit 1; }
test ! -e "$remote_archive" && test ! -L "$remote_archive" || {
    printf 'Deployment archive destination already exists or is a symlink.\n' >&2
    exit 1
}
mkdir -m 700 -- "$backup_dir"

backup_paths=(
    config data logs .env bot database tests main.py config_loader.py
    requirements.txt Dockerfile docker-compose.yml .dockerignore .gitignore
    AI_CONTEXT.md
)
present_paths=()
for relative_path in "${backup_paths[@]}"; do
    source_path="${app_dir}/${relative_path}"
    if [[ -L "$source_path" ]]; then
        printf 'Backup source must not be a symlink: %s\n' "$relative_path" >&2
        exit 1
    fi
    if [[ -e "$source_path" ]]; then
        if [[ -d "$source_path" ]] && find "$source_path" -type l -print -quit | grep -q .; then
            printf 'Backup source contains a symlink: %s\n' "$relative_path" >&2
            exit 1
        fi
        present_paths+=("$relative_path")
    fi
done

tar -czf "${backup_dir}/predeploy-files.tar.gz" -C "$app_dir" -- "${present_paths[@]}"
tar -tzf "${backup_dir}/predeploy-files.tar.gz" >/dev/null
tar -tzf "${backup_dir}/predeploy-files.tar.gz" | grep -Fxq 'config/'
tar -tzf "${backup_dir}/predeploy-files.tar.gz" | grep -Fxq 'config/students.yaml'
tar -tzf "${backup_dir}/predeploy-files.tar.gz" | grep -Fxq 'data/bot.db'
tar -tzf "${backup_dir}/predeploy-files.tar.gz" | grep -Fxq '.env'
tar -tzf "${backup_dir}/predeploy-files.tar.gz" | grep -Fxq 'docker-compose.yml'
chmod 600 "${backup_dir}/predeploy-files.tar.gz"

python3 - "$database_path" "${backup_dir}/sqlite-bot.db" <<'PYTHON_BACKUP'
import sqlite3
import sys

source = sqlite3.connect(sys.argv[1], timeout=30)
destination = sqlite3.connect(sys.argv[2])
try:
    source_check = source.execute("PRAGMA integrity_check").fetchone()[0]
    if source_check != "ok":
        raise SystemExit("Source SQLite integrity check failed")
    source.backup(destination)
    backup_check = destination.execute("PRAGMA integrity_check").fetchone()[0]
    if backup_check != "ok":
        raise SystemExit("SQLite backup integrity check failed")
finally:
    destination.close()
    source.close()
PYTHON_BACKUP
chmod 600 "${backup_dir}/sqlite-bot.db"

printf 'Backup created and verified: %s\n' "$backup_dir"
REMOTE_BACKUP

printf 'Uploading the validated source archive to the allowlisted destination...\n'
scp "$ARCHIVE" "${REMOTE}:${REMOTE_ARCHIVE}"

printf 'Validating and installing the source-only overlay...\n'
ssh "$REMOTE" bash -s -- "$REMOTE_DIR" "$REMOTE_ARCHIVE" <<'REMOTE_INSTALL'
set -Eeuo pipefail

app_dir="$1"
archive_path="$2"
test -d "$app_dir" || { printf 'Application directory not found.\n' >&2; exit 1; }
test -f "$archive_path" || { printf 'Uploaded archive not found.\n' >&2; exit 1; }

python3 - "$app_dir" "$archive_path" <<'PYTHON_VALIDATE'
from pathlib import Path, PurePosixPath
import sys
import tarfile

root = Path(sys.argv[1]).resolve(strict=True)
archive_path = Path(sys.argv[2])
allowed_directories = {"bot", "database"}
allowed_files = {"main.py", "config_loader.py", "requirements.txt"}

with tarfile.open(archive_path, "r:gz") as archive:
    for member in archive.getmembers():
        path = PurePosixPath(member.name)
        if (
            path.is_absolute()
            or not path.parts
            or any(part in {"", ".", ".."} for part in path.parts)
            or not (member.isfile() or member.isdir())
        ):
            raise SystemExit("Unsafe deployment archive member; refusing extraction.")
        if path.parts[0] not in allowed_directories and not (
            len(path.parts) == 1 and path.parts[0] in allowed_files
        ):
            raise SystemExit("Non-allowlisted deployment archive member; refusing extraction.")
        if path.parts[0] in allowed_directories and (
            any(part.startswith(".") for part in path.parts[1:])
            or (member.isfile() and path.suffix != ".py")
        ):
            raise SystemExit("Non-allowlisted runtime file; refusing extraction.")
        destination = (root / Path(*path.parts)).resolve()
        try:
            destination.relative_to(root)
        except ValueError:
            raise SystemExit("Deployment archive would escape the application directory.") from None
PYTHON_VALIDATE

tar -xzf "$archive_path" -C "$app_dir"
printf 'Source overlay installed. The uploaded archive is retained for inspection.\n'
REMOTE_INSTALL

printf 'Rebuilding and restarting the bot without removing volumes...\n'
ssh "$REMOTE" bash -s -- "$REMOTE_DIR" <<'REMOTE_RESTART'
set -Eeuo pipefail

app_dir="$1"
cd "$app_dir"
docker compose up -d --build bot
docker compose ps --status running --services | grep -Fxq bot || {
    printf 'Bot container did not reach the running state.\n' >&2
    docker compose ps
    exit 1
}
printf 'Bot container is running.\n'
REMOTE_RESTART

printf 'Deployment completed successfully.\n'
