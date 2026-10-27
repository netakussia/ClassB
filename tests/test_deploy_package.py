from __future__ import annotations

import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import unittest

from deploy_package import (
    PACKAGE_PATHS,
    build_deploy_archive,
    validate_deploy_archive,
)


class DeployPackageTests(unittest.TestCase):
    def test_archive_contains_only_allowlisted_config_and_excludes_local_data(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "project"
            project.mkdir()
            (project / "config").mkdir()
            for relative_path in PACKAGE_PATHS:
                path = project / relative_path
                if relative_path in {"bot", "database", "tests"}:
                    path.mkdir(parents=True, exist_ok=True)
                    (path / "module.py").write_text("pass\n", encoding="utf-8")
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text("fixture", encoding="utf-8")

            (project / "config/bot.yaml").write_text("secret config", encoding="utf-8")
            (project / "config/roles.yaml").write_text("private roles", encoding="utf-8")
            (project / "config/students.yaml").write_text("private roster", encoding="utf-8")
            (project / "config/ai.yaml").write_text("enabled: false", encoding="utf-8")
            (project / "config/emojis.yaml").write_text("private emoji config", encoding="utf-8")
            (project / ".env").write_text("private key", encoding="utf-8")
            (project / "bot/.env.local").write_text("private key", encoding="utf-8")
            (project / "bot/.env.local-dir").mkdir()
            (project / "bot/.env.local-dir/credentials.py").write_text(
                "private local code", encoding="utf-8"
            )
            (project / "bot/local-settings.json").write_text(
                "private local settings", encoding="utf-8"
            )
            (project / "database/api-token.txt").write_text(
                "private local token", encoding="utf-8"
            )
            (project / "bot/__pycache__").mkdir()
            (project / "bot/__pycache__/module.pyc").write_bytes(b"compiled")
            (project / "data").mkdir()
            (project / "data/bot.db").write_bytes(b"database")
            (project / "logs").mkdir()
            (project / "logs/bot.log").write_text("private log", encoding="utf-8")
            (project / "docker-compose.yml").write_text("preserve compose", encoding="utf-8")
            (project / "Dockerfile").write_text("preserve build config", encoding="utf-8")

            archive_path = Path(directory) / "deployment.tar.gz"
            build_deploy_archive(project, archive_path)

            with tarfile.open(archive_path, "r:gz") as archive:
                names = set(archive.getnames())

        self.assertEqual(
            {name.split("/", 1)[0] for name in names},
            {"bot", "database", "config_loader.py", "main.py", "requirements.txt"},
        )
        self.assertIn("bot/module.py", names)
        self.assertNotIn(".env", names)
        self.assertFalse(any(".env" in name for name in names))
        self.assertNotIn("bot/local-settings.json", names)
        self.assertNotIn("database/api-token.txt", names)
        self.assertFalse(any("__pycache__" in name for name in names))
        self.assertFalse(any(name.startswith("data/") for name in names))
        self.assertFalse(any(name.startswith("logs/") for name in names))
        self.assertFalse(any(name.endswith("students.yaml") for name in names))
        self.assertFalse(any(name.startswith("config/") for name in names))
        self.assertNotIn("docker-compose.yml", names)
        self.assertNotIn("Dockerfile", names)
        self.assertFalse(any(name.startswith("tests/") for name in names))

    def test_archive_validator_rejects_traversal_and_non_allowlisted_paths(self) -> None:
        for member_name in (
            "../.env",
            "config/ai.yaml.example",
            "docker-compose.yml",
            "bot/credentials.txt",
            "database/.local.py",
        ):
            with self.subTest(member_name=member_name), tempfile.TemporaryDirectory() as directory:
                archive_path = Path(directory) / "unsafe.tar.gz"
                with tarfile.open(archive_path, "w:gz") as archive:
                    info = tarfile.TarInfo(member_name)
                    info.size = 1
                    archive.addfile(info, fileobj=io.BytesIO(b"x"))
                with self.assertRaises(ValueError):
                    validate_deploy_archive(archive_path)

    def _deployment_fixture(self, directory: str) -> tuple[Path, Path, Path]:
        root = Path(directory)
        project = root / "project"
        project.mkdir()
        source_root = Path(__file__).resolve().parents[1]
        shutil.copy2(source_root / "deploy.sh", project / "deploy.sh")
        shutil.copy2(source_root / "deploy_package.py", project / "deploy_package.py")
        for relative_path in PACKAGE_PATHS:
            path = project / relative_path
            if relative_path in {"bot", "database"}:
                path.mkdir(parents=True, exist_ok=True)
                (path / "module.py").write_text("pass\n", encoding="utf-8")
            else:
                path.write_text("fixture\n", encoding="utf-8")

        allowlist = project / ".deploy-allowlist"
        allowlist.write_text("approved@staging.example\n", encoding="utf-8")
        fake_bin = root / "bin"
        fake_bin.mkdir()
        ssh_marker = root / "ssh-called"
        scp_marker = root / "scp-called"
        ssh_capture = root / "ssh-script"
        for command, marker in (("ssh", ssh_marker), ("scp", scp_marker)):
            executable = fake_bin / command
            executable.write_text(
                "#!/usr/bin/env bash\n"
                f"printf 'called\\n' >> {str(marker)!r}\n"
                + (
                    f"cat > {str(ssh_capture)!r}\n"
                    "exit \"${FAKE_SSH_EXIT:-97}\"\n"
                    if command == "ssh"
                    else "exit 0\n"
                ),
                encoding="utf-8",
            )
            executable.chmod(0o755)
        return project / "deploy.sh", ssh_marker, scp_marker

    def _run_deploy(
        self,
        script: Path,
        remote: str | None,
        environment_overrides: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment.pop("DEPLOY_REMOTE", None)
        environment["PATH"] = f"{script.parent.parent / 'bin'}:{environment['PATH']}"
        if remote is not None:
            environment["DEPLOY_REMOTE"] = remote
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["TMPDIR"] = str(script.parent.parent)
        if environment_overrides:
            environment.update(environment_overrides)
        return subprocess.run(
            ["/bin/bash", str(script)],
            cwd=script.parent,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

    def _prepare_fake_remote(
        self, directory: str, *, corrupt_database: bool = False
    ) -> tuple[Path, Path, Path, Path, Path, Path]:
        script, ssh_marker, scp_marker = self._deployment_fixture(directory)
        root = Path(directory)
        fake_bin = root / "bin"
        app_dir = root / "server" / "app"
        home = root / "server-home"
        for relative_path in ("config", "data", "logs", "bot", "database", "tests"):
            (app_dir / relative_path).mkdir(parents=True, exist_ok=True)
        (app_dir / "config/students.yaml").write_text("fixture roster\n", encoding="utf-8")
        (app_dir / "config/active.yaml").write_text("fixture config\n", encoding="utf-8")
        (app_dir / ".env").write_text("fixture environment\n", encoding="utf-8")
        (app_dir / "docker-compose.yml").write_text(
            "services:\n"
            "  bot:\n"
            "    env_file:\n"
            "      - .env\n"
            "    volumes:\n"
            "      - ./config:/app/config:ro\n"
            "      - ./config/students.yaml:/app/config/students.yaml\n"
            "      - ./data:/app/data\n"
            "      - ./logs:/app/logs\n",
            encoding="utf-8",
        )
        (app_dir / "logs/bot.log").write_text("fixture log\n", encoding="utf-8")
        (app_dir / "bot/old.py").write_text("old source\n", encoding="utf-8")
        (app_dir / "user-file.dat").write_text("fixture user data\n", encoding="utf-8")
        database_path = app_dir / "data/bot.db"
        if corrupt_database:
            database_path.write_bytes(b"not a sqlite database")
        else:
            with sqlite3.connect(database_path) as connection:
                connection.execute("CREATE TABLE fixture (value TEXT)")
                connection.execute("INSERT INTO fixture VALUES ('preserve me')")

        (fake_bin / "ssh").write_text(
            "#!/usr/bin/env bash\n"
            f"printf 'called\\n' >> {str(ssh_marker)!r}\n"
            "shift\n"
            'exec "$@"\n',
            encoding="utf-8",
        )
        (fake_bin / "ssh").chmod(0o755)
        (fake_bin / "scp").write_text(
            "#!/usr/bin/env bash\n"
            f"printf 'called\\n' >> {str(scp_marker)!r}\n"
            'destination="${2#*:}"\n'
            'cp -- "$1" "$destination"\n',
            encoding="utf-8",
        )
        (fake_bin / "scp").chmod(0o755)
        docker_marker = root / "docker-called"
        compose_config_path = root / "compose-config.json"
        compose_config_path.write_text(
            json.dumps(
                {
                    "services": {
                        "bot": {
                            "env_file": [{"path": str(app_dir / ".env")}],
                            "environment": {},
                            "volumes": [
                                {
                                    "type": "bind",
                                    "source": str(app_dir / "config"),
                                    "target": "/app/config",
                                    "read_only": True,
                                },
                                {
                                    "type": "bind",
                                    "source": str(app_dir / "config/students.yaml"),
                                    "target": "/app/config/students.yaml",
                                },
                                {
                                    "type": "bind",
                                    "source": str(app_dir / "data"),
                                    "target": "/app/data",
                                },
                                {
                                    "type": "bind",
                                    "source": str(app_dir / "logs"),
                                    "target": "/app/logs",
                                },
                            ],
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        (fake_bin / "docker").write_text(
            "#!/usr/bin/env bash\n"
            f'if [[ "$*" == "compose config --format json" ]]; then cat {str(compose_config_path)!r}; exit 0; fi\n'
            f"printf '%s\\n' \"$*\" >> {str(docker_marker)!r}\n"
            'if [[ "$*" == "compose up -d --build bot" ]]; then exit 0; fi\n'
            'if [[ "$*" == "compose ps --status running --services" ]]; then printf "bot\\n"; exit 0; fi\n'
            "exit 97\n",
            encoding="utf-8",
        )
        (fake_bin / "docker").chmod(0o755)
        return script, app_dir, home, database_path, scp_marker, docker_marker

    def test_unset_empty_malformed_and_unallowlisted_targets_fail_before_ssh(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            script, ssh_marker, _scp_marker = self._deployment_fixture(directory)
            for remote in (None, "", "approved@staging.example;echo unsafe", "other@staging.example"):
                with self.subTest(remote=remote):
                    result = self._run_deploy(script, remote)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertFalse(ssh_marker.exists())

    def test_invalid_remote_directories_fail_before_ssh(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            script, ssh_marker, _scp_marker = self._deployment_fixture(directory)
            for remote_dir in ("/", "/tmp/app/", "/tmp//app", "/tmp/../app", "/tmp/./app"):
                with self.subTest(remote_dir=remote_dir):
                    result = self._run_deploy(
                        script,
                        "approved@staging.example",
                        {"DEPLOY_REMOTE_DIR": remote_dir},
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertFalse(ssh_marker.exists())

    def test_deployment_stops_on_backup_failure_before_upload_or_overlay(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            script, ssh_marker, scp_marker = self._deployment_fixture(directory)
            environment = os.environ.copy()
            environment["PATH"] = f"{script.parent.parent / 'bin'}:{environment['PATH']}"
            environment["DEPLOY_REMOTE"] = "approved@staging.example"
            environment["TMPDIR"] = str(script.parent.parent)
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            environment["FAKE_SSH_EXIT"] = "23"

            result = subprocess.run(
                ["/bin/bash", str(script)],
                cwd=script.parent,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 23)
            self.assertTrue(ssh_marker.exists())
            self.assertFalse(scp_marker.exists())
            backup_script = (script.parent.parent / "ssh-script").read_text(
                encoding="utf-8"
            )
            for required_backup in (
                'backup_paths=(',
                'config data logs .env bot database tests',
                'predeploy-files.tar.gz',
                'sqlite-bot.db',
                'PRAGMA integrity_check',
                'config/students.yaml',
                'docker-compose.yml',
            ):
                self.assertIn(required_backup, backup_script)
            self.assertNotIn("docker compose down", backup_script)
            self.assertNotIn("--volumes", backup_script)
            deployment_script = (script.parent / "deploy.sh").read_text(
                encoding="utf-8"
            )
            self.assertNotIn("docker compose down", deployment_script)
            self.assertNotIn("--volumes", deployment_script)
            self.assertNotIn("rm -f -- \"$archive_path\"", deployment_script)
            self.assertIn("docker compose up -d --build bot", deployment_script)

    def test_fake_deployment_backs_up_and_preserves_existing_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            script, app_dir, home, database_path, scp_marker, docker_marker = (
                self._prepare_fake_remote(directory)
            )
            protected_files = (
                app_dir / "config/active.yaml",
                app_dir / "config/students.yaml",
                app_dir / ".env",
                app_dir / "docker-compose.yml",
                app_dir / "logs/bot.log",
                database_path,
                app_dir / "user-file.dat",
            )
            before = {path: path.read_bytes() for path in protected_files}
            environment = {
                "DEPLOY_REMOTE_DIR": str(app_dir),
                "HOME": str(home),
                "PATH": f"{script.parent.parent / 'bin'}:{os.environ['PATH']}",
            }

            result = self._run_deploy(
                script, "approved@staging.example", environment
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(scp_marker.exists())
            self.assertEqual((app_dir / "bot/module.py").read_text(encoding="utf-8"), "pass\n")
            for path, contents in before.items():
                self.assertEqual(path.read_bytes(), contents, str(path))
            self.assertIn(
                "compose up -d --build bot",
                docker_marker.read_text(encoding="utf-8"),
            )

            backup_dirs = list((home / "backups/rc-215").iterdir())
            self.assertEqual(len(backup_dirs), 1)
            backup_dir = backup_dirs[0]
            with tarfile.open(backup_dir / "predeploy-files.tar.gz", "r:gz") as archive:
                backup_names = set(archive.getnames())
            self.assertTrue(
                {
                    "config/active.yaml",
                    "config/students.yaml",
                    ".env",
                    "docker-compose.yml",
                    "logs/bot.log",
                    "data/bot.db",
                }.issubset(backup_names)
            )
            with sqlite3.connect(backup_dir / "sqlite-bot.db") as backup_connection:
                self.assertEqual(
                    backup_connection.execute("PRAGMA integrity_check").fetchone()[0],
                    "ok",
                )
                self.assertEqual(
                    backup_connection.execute("SELECT value FROM fixture").fetchone()[0],
                    "preserve me",
                )
            restored_path = Path(directory) / "restored.sqlite"
            with sqlite3.connect(restored_path) as restored:
                restored.execute("CREATE TABLE disposable (value TEXT)")
                with sqlite3.connect(backup_dir / "sqlite-bot.db") as backup:
                    backup.backup(restored)
                self.assertEqual(
                    restored.execute("PRAGMA integrity_check").fetchone()[0],
                    "ok",
                )
                self.assertEqual(
                    restored.execute("SELECT value FROM fixture").fetchone()[0],
                    "preserve me",
                )

    def test_corrupt_database_aborts_before_upload_overlay_or_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            script, app_dir, home, database_path, scp_marker, docker_marker = (
                self._prepare_fake_remote(directory, corrupt_database=True)
            )
            environment = {
                "DEPLOY_REMOTE_DIR": str(app_dir),
                "HOME": str(home),
                "PATH": f"{script.parent.parent / 'bin'}:{os.environ['PATH']}",
            }

            result = self._run_deploy(
                script, "approved@staging.example", environment
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(scp_marker.exists())
            self.assertFalse(docker_marker.exists())
            self.assertEqual((app_dir / "bot/old.py").read_text(encoding="utf-8"), "old source\n")
            self.assertTrue(database_path.exists())
            self.assertFalse(list(app_dir.glob(".rc-215-deploy-*.tar.gz")))

    def test_external_database_configuration_aborts_before_upload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            script, app_dir, home, _database_path, scp_marker, docker_marker = (
                self._prepare_fake_remote(directory)
            )
            (app_dir / ".env").write_text(
                "DATABASE_URL=sqlite:///outside/bot.db\n",
                encoding="utf-8",
            )
            environment = {
                "DEPLOY_REMOTE_DIR": str(app_dir),
                "HOME": str(home),
                "PATH": f"{script.parent.parent / 'bin'}:{os.environ['PATH']}",
            }

            result = self._run_deploy(
                script, "approved@staging.example", environment
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("DATABASE_URL override", result.stderr)
            self.assertFalse(scp_marker.exists())
            self.assertFalse(docker_marker.exists())
            self.assertEqual(
                (app_dir / "bot/old.py").read_text(encoding="utf-8"),
                "old source\n",
            )

    def test_external_compose_env_file_aborts_before_upload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            script, app_dir, home, _database_path, scp_marker, docker_marker = (
                self._prepare_fake_remote(directory)
            )
            compose_path = app_dir / "docker-compose.yml"
            compose_path.write_text(
                compose_path.read_text(encoding="utf-8").replace(
                    "      - .env\n",
                    "      - .env\n      - /etc/another-env\n",
                ),
                encoding="utf-8",
            )
            environment = {
                "DEPLOY_REMOTE_DIR": str(app_dir),
                "HOME": str(home),
                "PATH": f"{script.parent.parent / 'bin'}:{os.environ['PATH']}",
            }

            result = self._run_deploy(
                script, "approved@staging.example", environment
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("only the backed-up .env file", result.stderr)
            self.assertFalse(scp_marker.exists())
            self.assertFalse(docker_marker.exists())
            self.assertEqual(
                (app_dir / "bot/old.py").read_text(encoding="utf-8"),
                "old source\n",
            )

    def test_unbacked_compose_database_volume_aborts_before_upload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            script, app_dir, home, _database_path, scp_marker, docker_marker = (
                self._prepare_fake_remote(directory)
            )
            compose_config_path = Path(directory) / "compose-config.json"
            compose_config = json.loads(compose_config_path.read_text(encoding="utf-8"))
            compose_config["services"]["bot"]["volumes"][2]["source"] = str(
                Path(directory) / "external-data"
            )
            compose_config_path.write_text(
                json.dumps(compose_config),
                encoding="utf-8",
            )
            environment = {
                "DEPLOY_REMOTE_DIR": str(app_dir),
                "HOME": str(home),
                "PATH": f"{script.parent.parent / 'bin'}:{os.environ['PATH']}",
            }

            result = self._run_deploy(
                script, "approved@staging.example", environment
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Compose volumes do not match", result.stderr)
            self.assertFalse(scp_marker.exists())
            self.assertFalse(docker_marker.exists())
            self.assertEqual(
                (app_dir / "bot/old.py").read_text(encoding="utf-8"),
                "old source\n",
            )


if __name__ == "__main__":
    unittest.main()
