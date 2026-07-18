# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import errno
from pathlib import Path

import pytest

from cmpunlock import firmware
from cmpunlock.errors import FirmwareError


def test_atomic_replace_bytes_works_on_current_platform(local_tmp_path: Path) -> None:
    output = local_tmp_path / "output.bin"
    output.write_bytes(b"before")

    firmware.atomic_replace_bytes(output, b"after", metadata_from=output)

    assert output.read_bytes() == b"after"


def test_xattr_copy_fails_closed_on_metadata_loss(
    local_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = local_tmp_path / "source.bin"
    destination = local_tmp_path / "destination.bin"
    source.write_bytes(b"source")
    destination.write_bytes(b"destination")
    monkeypatch.setattr(firmware.os, "listxattr", lambda _path: ["security.test"], raising=False)
    monkeypatch.setattr(
        firmware.os,
        "getxattr",
        lambda _path, _name: b"label",
        raising=False,
    )

    def reject_xattr(*_args: object) -> None:
        raise OSError(errno.EACCES, "permission denied")

    monkeypatch.setattr(firmware.os, "setxattr", reject_xattr, raising=False)

    with pytest.raises(FirmwareError, match="cannot preserve metadata attribute"):
        firmware._copy_xattrs(source, destination)


def test_atomic_replace_fsyncs_after_metadata_before_rename(
    local_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = local_tmp_path / "output.bin"
    output.write_bytes(b"before")
    events: list[str] = []
    real_chmod = firmware.os.chmod
    real_fsync = firmware.os.fsync
    real_replace = firmware.os.replace

    def tracked_chmod(path: Path, mode: int) -> None:
        events.append("chmod")
        real_chmod(path, mode)

    def tracked_fsync(descriptor: int) -> None:
        events.append("fsync")
        real_fsync(descriptor)

    def tracked_replace(source: Path, destination: Path) -> None:
        events.append("replace")
        real_replace(source, destination)

    monkeypatch.setattr(firmware.os, "chmod", tracked_chmod)
    monkeypatch.setattr(firmware.os, "fsync", tracked_fsync)
    monkeypatch.setattr(firmware.os, "replace", tracked_replace)

    firmware.atomic_replace_bytes(output, b"after", metadata_from=output)

    replace_index = events.index("replace")
    fsyncs_before_replace = [
        index for index, event in enumerate(events[:replace_index]) if event == "fsync"
    ]
    assert len(fsyncs_before_replace) == 2
    assert events.index("chmod") < fsyncs_before_replace[-1] < replace_index
