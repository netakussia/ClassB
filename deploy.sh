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

COMPOSE_OVERRIDE="${DEPLOY_COMPOSE_OVERRIDE:-}"
if [[ -n "$COMPOSE_OVERRIDE" ]] \
    && { [[ ! "$COMPOSE_OVERRIDE" =~ ^/[A-Za-z0-9._/-]+$ ]] \
        || [[ "$COMPOSE_OVERRIDE" == *"//"* || "$COMPOSE_OVERRIDE" == */ \
            || "$COMPOSE_OVERRIDE" == *"/../"* || "$COMPOSE_OVERRIDE" == */.. \
            || "$COMPOSE_OVERRIDE" == *"/./"* || "$COMPOSE_OVERRIDE" == */. ]]; }; then
    printf 'DEPLOY_COMPOSE_OVERRIDE must be a normalized absolute path.\n' >&2
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
ssh "$REMOTE" bash -s -- "$REMOTE_DIR" "$TIMESTAMP" "$REMOTE_ARCHIVE" "$COMPOSE_OVERRIDE" <<'REMOTE_BACKUP'
set -Eeuo pipefail
umask 077

app_dir="$1"
timestamp="$2"
remote_archive="$3"
compose_override="${4:-}"
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
compose_files=("${app_dir}/docker-compose.yml")
if [[ -n "$compose_override" ]]; then
    test -f "$compose_override" || { printf 'Active Compose override not found.\n' >&2; exit 1; }
    test ! -L "$compose_override" || { printf 'Active Compose override must not be a symlink.\n' >&2; exit 1; }
    compose_files+=("$compose_override")
fi
for compose_file in "${compose_files[@]}"; do
    [[ "$compose_file" != *:* ]] || {
        printf 'Compose file paths containing colons are unsupported.\n' >&2
        exit 1
    }
done
export COMPOSE_FILE="$(IFS=:; printf '%s' "${compose_files[*]}")"

container_status="$(docker inspect --format '{{.State.Status}}' rc-215-bot-1)"
test "$container_status" = "running" || {
    printf 'Expected running container rc-215-bot-1; refusing deployment.\n' >&2
    exit 1
}
active_compose_files="$(docker inspect --format '{{index .Config.Labels "com.docker.compose.project.config_files"}}' rc-215-bot-1)"
expected_compose_files="${app_dir}/docker-compose.yml"
if [[ -n "$compose_override" ]]; then
    test -f "$compose_override" || { printf 'Active Compose override not found.\n' >&2; exit 1; }
    test ! -L "$compose_override" || { printf 'Active Compose override must not be a symlink.\n' >&2; exit 1; }
    expected_compose_files+=",${compose_override}"
fi
test "$active_compose_files" = "$expected_compose_files" || {
    printf 'Active Compose files differ from the requested deployment configuration.\n' >&2
    exit 1
}
for compose_file in "${compose_files[@]}"; do
    if grep -q 'DATABASE_URL' "$compose_file"; then
        printf 'A DATABASE_URL override is present; automatic backup only supports data/bot.db. Aborting.\n' >&2
        exit 1
    fi
done
if grep -Eq '^[[:space:]]*(export[[:space:]]+)?DATABASE_URL[[:space:]]*=' "${app_dir}/.env"; then
    printf 'A DATABASE_URL override is present; automatic backup only supports data/bot.db. Aborting.\n' >&2
    exit 1
fi
python3 - "${compose_files[@]}" <<'PYTHON_ENV_FILE_CHECK'
import re
import sys

env_files = []
for compose_path in sys.argv[1:]:
    lines = open(compose_path, encoding="utf-8").read().splitlines()
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
if normalized_env_files not in ([], [(app_dir / ".env").resolve()]):
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
tar -tzf "${backup_dir}/predeploy-files.tar.gz" | grep -Fx 'config/' >/dev/null
tar -tzf "${backup_dir}/predeploy-files.tar.gz" | grep -Fx 'config/students.yaml' >/dev/null
tar -tzf "${backup_dir}/predeploy-files.tar.gz" | grep -Fx 'data/bot.db' >/dev/null
tar -tzf "${backup_dir}/predeploy-files.tar.gz" | grep -Fx '.env' >/dev/null
tar -tzf "${backup_dir}/predeploy-files.tar.gz" | grep -Fx 'docker-compose.yml' >/dev/null
chmod 600 "${backup_dir}/predeploy-files.tar.gz"

source_paths=(bot database main.py config_loader.py requirements.txt)
for relative_path in "${source_paths[@]}"; do
    test -e "${app_dir}/${relative_path}" || {
        printf 'Source rollback path is missing: %s\n' "$relative_path" >&2
        exit 1
    }
done
tar -czf "${backup_dir}/predeploy-source.tar.gz" -C "$app_dir" -- "${source_paths[@]}"
tar -tzf "${backup_dir}/predeploy-source.tar.gz" >/dev/null
chmod 600 "${backup_dir}/predeploy-source.tar.gz"
source_restore_dir="$(mktemp -d "${backup_dir}/.source-restore-check.XXXXXX")"
tar -xzf "${backup_dir}/predeploy-source.tar.gz" -C "$source_restore_dir"
for required_path in main.py config_loader.py requirements.txt bot database; do
    test -e "${source_restore_dir}/${required_path}" || {
        printf 'Source rollback restore check failed for %s.\n' "$required_path" >&2
        exit 1
    }
done
rm -rf -- "$source_restore_dir"

restore_check_dir="$(mktemp -d "${backup_dir}/.restore-check.XXXXXX")"
tar -xzf "${backup_dir}/predeploy-files.tar.gz" -C "$restore_check_dir"
for required_path in config/students.yaml data/bot.db .env docker-compose.yml; do
    test -f "${restore_check_dir}/${required_path}" || {
        printf 'Backup restore check failed for %s.\n' "$required_path" >&2
        exit 1
    }
done
rm -rf -- "$restore_check_dir"

if [[ -n "$compose_override" ]]; then
    case "$compose_override" in
        "${HOME}/"*) ;;
        *)
            printf 'Active Compose override must be under the server home directory.\n' >&2
            exit 1
            ;;
    esac
    override_relative="${compose_override#${HOME}/}"
    test -n "$override_relative" || {
        printf 'Active Compose override path is invalid.\n' >&2
        exit 1
    }
    tar -czf "${backup_dir}/predeploy-compose-override.tar.gz" \
        -C "$HOME" -- "$override_relative"
    tar -tzf "${backup_dir}/predeploy-compose-override.tar.gz" \
        | grep -Fx "$override_relative" >/dev/null
    override_restore_dir="$(mktemp -d "${backup_dir}/.compose-restore-check.XXXXXX")"
    tar -xzf "${backup_dir}/predeploy-compose-override.tar.gz" -C "$override_restore_dir"
    test -f "${override_restore_dir}/${override_relative}" || {
        printf 'Compose override backup restore check failed.\n' >&2
        exit 1
    }
    rm -rf -- "$override_restore_dir"
    chmod 600 "${backup_dir}/predeploy-compose-override.tar.gz"
fi

python3 - "$database_path" "${backup_dir}/sqlite-bot.db" <<'PYTHON_BACKUP'
import os
import sqlite3
import sys

source = sqlite3.connect(sys.argv[1], timeout=30)
destination = sqlite3.connect(sys.argv[2])
restore_path = f"{sys.argv[2]}.restore-check"
restore = sqlite3.connect(restore_path)
try:
    source_check = source.execute("PRAGMA integrity_check").fetchone()[0]
    if source_check != "ok":
        raise SystemExit("Source SQLite integrity check failed")
    source.backup(destination)
    backup_check = destination.execute("PRAGMA integrity_check").fetchone()[0]
    if backup_check != "ok":
        raise SystemExit("SQLite backup integrity check failed")
    destination.backup(restore)
    restore_check = restore.execute("PRAGMA integrity_check").fetchone()[0]
    if restore_check != "ok":
        raise SystemExit("SQLite backup restore check failed")
finally:
    restore.close()
    destination.close()
    source.close()
if os.path.exists(restore_path):
    os.remove(restore_path)
PYTHON_BACKUP
chmod 600 "${backup_dir}/sqlite-bot.db"

printf 'Backup created and restore-verified: %s\n' "$backup_dir"
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
ssh "$REMOTE" bash -s -- "$REMOTE_DIR" "$COMPOSE_OVERRIDE" <<'REMOTE_RESTART'
set -Eeuo pipefail

app_dir="$1"
compose_override="${2:-}"
cd "$app_dir"
compose_files=("${app_dir}/docker-compose.yml")
if [[ -n "$compose_override" ]]; then
    test -f "$compose_override" || { printf 'Active Compose override not found.\n' >&2; exit 1; }
    test ! -L "$compose_override" || { printf 'Active Compose override must not be a symlink.\n' >&2; exit 1; }
    compose_files+=("$compose_override")
fi
for compose_file in "${compose_files[@]}"; do
    [[ "$compose_file" != *:* ]] || {
        printf 'Compose file paths containing colons are unsupported.\n' >&2
        exit 1
    }
done
export COMPOSE_FILE="$(IFS=:; printf '%s' "${compose_files[*]}")"
docker compose up -d --build bot
docker compose ps --status running --services | grep -Fxq bot || {
    printf 'Bot container did not reach the running state.\n' >&2
    docker compose ps
    exit 1
}
printf 'Bot container is running.\n'
REMOTE_RESTART

printf 'Deployment completed successfully.\n'
