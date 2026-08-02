from __future__ import annotations

import gzip
import hashlib
import os
import subprocess
from pathlib import Path

import pytest


pytestmark = pytest.mark.unit
REPO_ROOT = Path(__file__).resolve().parents[3]


def _executable(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\nset -eu\n" + body, encoding="utf-8")
    path.chmod(0o700)


def test_backup_is_private_compressed_and_checksummed(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _executable(bin_dir / "mysqldump", "printf '%s\\n' 'CREATE TABLE evidence(id INT);'\n")
    defaults = tmp_path / "mysql.cnf"
    defaults.write_text("[client]\nuser=backup\npassword=secret-from-mounted-file\n", encoding="utf-8")
    defaults.chmod(0o600)
    output_dir = tmp_path / "backups"
    environment = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "MYSQL_BACKUP_DEFAULTS_FILE": str(defaults),
        "MYSQL_BACKUP_DATABASE": "myclaw_test",
        "MYSQL_BACKUP_OUTPUT_DIR": str(output_dir),
    }

    completed = subprocess.run(
        [str(REPO_ROOT / "deploy/scripts/backup_mysql.sh")],
        check=True, capture_output=True, text=True, env=environment,
    )
    backup = Path(completed.stdout.strip())
    assert gzip.decompress(backup.read_bytes()) == b"CREATE TABLE evidence(id INT);\n"
    assert backup.stat().st_mode & 0o077 == 0
    assert Path(f"{backup}.sha256").is_file()


def test_restore_requires_exact_confirmation_and_verified_checksum(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    capture = tmp_path / "restored.sql"
    _executable(bin_dir / "mysql", 'cp /dev/stdin "$RESTORE_CAPTURE"\n')
    defaults = tmp_path / "mysql.cnf"
    defaults.write_text("[client]\nuser=restore\npassword=secret-from-mounted-file\n", encoding="utf-8")
    backup = tmp_path / "myclaw.sql.gz"
    backup.write_bytes(gzip.compress(b"CREATE TABLE restored(id INT);\n"))
    digest = hashlib.sha256(backup.read_bytes()).hexdigest()
    Path(f"{backup}.sha256").write_text(f"{digest}  {backup.name}\n", encoding="utf-8")
    environment = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "MYSQL_RESTORE_DEFAULTS_FILE": str(defaults),
        "MYSQL_RESTORE_DATABASE": "myclaw_restore",
        "RESTORE_CAPTURE": str(capture),
    }
    script = str(REPO_ROOT / "deploy/scripts/restore_mysql.sh")

    rejected = subprocess.run(
        [script, str(backup)], capture_output=True, text=True,
        env={**environment, "MYCLAW_RESTORE_CONFIRM": "restore:wrong"},
    )
    assert rejected.returncode == 2
    assert not capture.exists()

    subprocess.run(
        [script, str(backup)], check=True, capture_output=True, text=True,
        env={**environment, "MYCLAW_RESTORE_CONFIRM": "restore:myclaw_restore"},
    )
    assert capture.read_bytes() == b"CREATE TABLE restored(id INT);\n"
