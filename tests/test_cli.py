# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cmpunlock.cli import main


def test_cli_lists_profiles_as_json(capsys: pytest.CaptureFixture[str]) -> None:
    main(["profile", "list"])

    document = json.loads(capsys.readouterr().out)
    assert {item["driver_version"] for item in document} == {"580.105.08", "580.126.09"}


def test_cli_rejects_unknown_profile(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["profile", "show", "unknown-profile"])

    captured = capsys.readouterr()
    assert raised.value.code == 2
    assert captured.out == ""
    assert "error: profile not found" in captured.err


def test_cli_rejects_invalid_firmware(
    local_tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    firmware = local_tmp_path / "not-elf.bin"
    firmware.write_bytes(b"not firmware")

    with pytest.raises(SystemExit) as raised:
        main(["firmware", "inspect", str(firmware)])

    captured = capsys.readouterr()
    assert raised.value.code == 2
    assert "error: firmware is shorter than an ELF64 header" in captured.err


def test_cli_refuses_to_overwrite_payload_without_force(
    local_tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = local_tmp_path / "payload.bin"
    output.write_bytes(b"keep me")

    with pytest.raises(SystemExit) as raised:
        main(
            [
                "payload",
                "build",
                str(output),
                "--profile",
                "580.105.08",
                "--mode",
                "proof",
            ]
        )

    captured = capsys.readouterr()
    assert raised.value.code == 2
    assert output.read_bytes() == b"keep me"
    assert "pass --force to replace it" in captured.err


def test_cli_system_apply_is_inert_without_execute(
    local_tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    nonexistent = local_tmp_path / "gsp_tu10x.bin"

    with pytest.raises(SystemExit) as raised:
        main(
            [
                "system",
                "apply",
                "0000:01:00.0",
                "--firmware",
                str(nonexistent),
            ]
        )

    captured = capsys.readouterr()
    assert raised.value.code == 2
    assert "inert unless --execute is present" in captured.err


def test_cli_reports_missing_input_file(
    local_tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = local_tmp_path / "missing.bin"

    with pytest.raises(SystemExit) as raised:
        main(["firmware", "inspect", str(missing)])

    captured = capsys.readouterr()
    assert raised.value.code == 2
    assert "error:" in captured.err
    assert "missing.bin" in captured.err
