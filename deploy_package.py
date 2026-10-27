from __future__ import annotations

from pathlib import Path, PurePosixPath
import tarfile


PACKAGE_PATHS = (
    "bot",
    "database",
    "main.py",
    "config_loader.py",
    "requirements.txt",
)
_EXCLUDED_NAMES = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "data",
    "dist",
    "logs",
    "node_modules",
    "site-packages",
    "students.yaml",
    "venv",
}
_ALLOWED_FILES = {"main.py", "config_loader.py", "requirements.txt"}
_ALLOWED_DIRECTORIES = {"bot", "database"}


def _include_member(member: tarfile.TarInfo) -> tarfile.TarInfo | None:
    path = PurePosixPath(member.name)
    if any(
        part in _EXCLUDED_NAMES or part.startswith(".")
        for part in path.parts
    ):
        return None
    if member.isdir():
        return member
    if not member.isfile():
        return None
    if path.parts == ("requirements.txt",):
        return member
    if path.suffix != ".py":
        return None
    return member


def validate_deploy_archive(archive_path: Path) -> None:
    """Reject archive members outside the runtime-code overlay allowlist."""
    with tarfile.open(archive_path, "r:gz") as archive:
        for member in archive.getmembers():
            path = PurePosixPath(member.name)
            if (
                path.is_absolute()
                or not path.parts
                or any(part in {"", ".", ".."} for part in path.parts)
                or not (member.isfile() or member.isdir())
            ):
                raise ValueError(f"Unsafe deployment archive member: {member.name}")

            if path.parts[0] in _ALLOWED_DIRECTORIES:
                if any(part.startswith(".") for part in path.parts[1:]):
                    raise ValueError(
                        f"Hidden deployment archive member is not allowed: {member.name}"
                    )
                if member.isfile() and path.suffix != ".py":
                    raise ValueError(
                        f"Deployment archive member is not an allowed Python file: "
                        f"{member.name}"
                    )
                continue
            if len(path.parts) == 1 and path.parts[0] in _ALLOWED_FILES:
                continue
            raise ValueError(f"Deployment archive member is not allowlisted: {member.name}")


def build_deploy_archive(project_dir: Path, archive_path: Path) -> None:
    """Create a runtime-code overlay without configuration or persistent data."""
    project_dir = project_dir.resolve()
    with tarfile.open(archive_path, "w:gz") as archive:
        for relative_path in PACKAGE_PATHS:
            source = project_dir / relative_path
            if not source.exists() or source.is_symlink():
                raise FileNotFoundError(f"Required deployment path is unavailable: {relative_path}")
            archive.add(
                source,
                arcname=relative_path,
                recursive=source.is_dir(),
                filter=_include_member,
            )
    validate_deploy_archive(archive_path)


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 3:
        raise SystemExit("Usage: deploy_package.py PROJECT_DIR ARCHIVE_PATH")
    build_deploy_archive(Path(sys.argv[1]), Path(sys.argv[2]))
