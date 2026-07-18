# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


pytestmark = pytest.mark.skipif(os.name != "posix", reason="Bash installer is POSIX-only")


def _installer_repo(tmp_path: Path) -> tuple[Path, Path, Path]:
    source_root = Path(__file__).parents[1]
    repo = tmp_path / "repository with spaces"
    (repo / "cmpunlock").mkdir(parents=True)
    shutil.copy2(source_root / "install.sh", repo / "install.sh")
    shutil.copy2(source_root / "pyproject.toml", repo / "pyproject.toml")
    shutil.copy2(source_root / "cmpunlock" / "cli.py", repo / "cmpunlock" / "cli.py")
    installer = repo / "install.sh"
    installer.chmod(0o755)

    log = tmp_path / "fake-python.log"
    fake_python = tmp_path / "fake-python"
    fake_python.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "${CMPUNLOCK_FAKE_LOG}"
if [[ "${1:-}" == "-c" ]]; then
    printf '3.12.0\\n'
elif [[ "${1:-}" == "-m" && "${2:-}" == "venv" ]]; then
    target="$3"
    mkdir -p "${target}/bin"
    printf 'home = fake\\n' > "${target}/pyvenv.cfg"
    cp "$0" "${target}/bin/python"
    chmod +x "${target}/bin/python"
    printf '#!/usr/bin/env bash\\n[[ "$*" == "profile list" ]]\\n' > "${target}/bin/cmpunlock"
    chmod +x "${target}/bin/cmpunlock"
elif [[ "${1:-}" == "-m" && "${2:-}" == "pip" ]]; then
    exit 0
else
    exit 91
fi
""",
        encoding="utf-8",
        newline="\n",
    )
    fake_python.chmod(0o755)
    return repo, fake_python, log


def _run_installer(repo: Path, fake_python: Path, log: Path, cwd: Path):
    environment = os.environ.copy()
    environment["CMPUNLOCK_PYTHON"] = str(fake_python)
    environment["CMPUNLOCK_FAKE_LOG"] = str(log)
    return subprocess.run(
        ["bash", str(repo / "install.sh")],
        cwd=cwd,
        env=environment,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def test_installer_is_valid_bash_and_idempotent_from_another_directory(
    tmp_path: Path,
) -> None:
    repo, fake_python, log = _installer_repo(tmp_path)
    subprocess.run(["bash", "-n", str(repo / "install.sh")], check=True)

    first = _run_installer(repo, fake_python, log, tmp_path)
    second = _run_installer(repo, fake_python, log, tmp_path)

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert "Installed successfully" in first.stdout
    assert "Reusing" in second.stdout
    assert (repo / ".venv" / "bin" / "cmpunlock").is_file()
    invocations = log.read_text(encoding="utf-8").splitlines()
    assert sum(line.startswith("-m venv ") for line in invocations) == 1
    assert sum("-m pip install" in line for line in invocations) == 2
    assert all("sudo" not in line and "modprobe" not in line for line in invocations)


def test_installer_preserves_incomplete_existing_venv(tmp_path: Path) -> None:
    repo, fake_python, log = _installer_repo(tmp_path)
    incomplete = repo / ".venv"
    incomplete.mkdir()
    sentinel = incomplete / "keep-me"
    sentinel.write_text("preserve", encoding="utf-8")

    result = _run_installer(repo, fake_python, log, tmp_path)

    assert result.returncode == 1
    assert "not a complete POSIX virtual environment" in result.stderr
    assert sentinel.read_text(encoding="utf-8") == "preserve"


def test_installer_uses_lf_line_endings() -> None:
    data = (Path(__file__).parents[1] / "install.sh").read_bytes()
    assert b"\r\n" not in data
