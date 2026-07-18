# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import errno
import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .elf import Elf64, expand_section_preserving_elf
from .errors import FirmwareError
from .payload import PayloadReport
from .profile import FirmwareProfile


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_stock_firmware(data: bytes, profile: FirmwareProfile) -> Elf64:
    actual_hash = sha256_bytes(data)
    if len(data) != profile.firmware_size:
        raise FirmwareError(
            f"firmware size mismatch: expected {profile.firmware_size}, got {len(data)}"
        )
    if actual_hash != profile.firmware_sha256:
        raise FirmwareError(
            "firmware SHA-256 mismatch: "
            f"expected {profile.firmware_sha256}, got {actual_hash}"
        )
    elf = Elf64.parse(data)
    signature = elf.section(profile.signature_section)
    if signature.offset != profile.signature_offset or signature.size != profile.signature_size:
        raise FirmwareError(
            f"unexpected {profile.signature_section} layout: "
            f"offset=0x{signature.offset:x}, size=0x{signature.size:x}"
        )
    signature_hash = sha256_bytes(elf.section_bytes(profile.signature_section))
    if signature_hash != profile.signature_sha256:
        raise FirmwareError(
            f"{profile.signature_section} SHA-256 mismatch: {signature_hash}"
        )
    version = elf.section_bytes(".fwversion").rstrip(b"\x00")
    try:
        version_text = version.decode("ascii")
    except UnicodeDecodeError as exc:
        raise FirmwareError(".fwversion is not ASCII") from exc
    if version_text != profile.driver_version:
        raise FirmwareError(
            f"firmware version mismatch: expected {profile.driver_version}, got {version_text}"
        )
    return elf


@dataclass(frozen=True)
class PatchReport:
    stock_sha256: str
    patched_sha256: str
    stock_size: int
    patched_size: int
    signature_offset: int
    stock_signature_size: int
    patched_signature_size: int
    relocated_sections: tuple[str, ...]
    payload: PayloadReport

    def as_dict(self) -> dict[str, Any]:
        return {
            "stock_sha256": self.stock_sha256,
            "patched_sha256": self.patched_sha256,
            "stock_size": self.stock_size,
            "patched_size": self.patched_size,
            "signature_offset": f"0x{self.signature_offset:x}",
            "stock_signature_size": self.stock_signature_size,
            "patched_signature_size": self.patched_signature_size,
            "relocated_sections": list(self.relocated_sections),
            "payload": self.payload.as_dict(),
        }


def patch_firmware(
    source: bytes,
    payload: bytes,
    payload_report: PayloadReport,
    profile: FirmwareProfile,
) -> tuple[bytes, PatchReport]:
    validate_stock_firmware(source, profile)
    if len(payload) != profile.payload_size:
        raise FirmwareError(
            f"payload size mismatch: expected {profile.payload_size}, got {len(payload)}"
        )
    patched, relocated = expand_section_preserving_elf(
        source, profile.signature_section, payload
    )
    elf = Elf64.parse(patched)
    signature = elf.section(profile.signature_section)
    report = PatchReport(
        stock_sha256=sha256_bytes(source),
        patched_sha256=sha256_bytes(patched),
        stock_size=len(source),
        patched_size=len(patched),
        signature_offset=signature.offset,
        stock_signature_size=profile.signature_size,
        patched_signature_size=signature.size,
        relocated_sections=relocated,
        payload=payload_report,
    )
    return patched, report


def _copy_xattrs(source: Path, destination: Path) -> None:
    if not all(hasattr(os, name) for name in ("listxattr", "getxattr", "setxattr")):
        return
    try:
        names = os.listxattr(source)
    except OSError as exc:
        unsupported = {
            errno.ENOSYS,
            getattr(errno, "ENOTSUP", -1),
            getattr(errno, "EOPNOTSUPP", -1),
        }
        if exc.errno in unsupported:
            return
        raise FirmwareError(f"cannot list metadata attributes on {source}: {exc}") from exc
    for name in names:
        try:
            os.setxattr(destination, name, os.getxattr(source, name))
        except OSError as exc:
            raise FirmwareError(
                f"cannot preserve metadata attribute {name!r} from {source}: {exc}"
            ) from exc


def atomic_replace_bytes(
    path: Path,
    data: bytes,
    *,
    metadata_from: Path | None = None,
    mode: int | None = None,
) -> None:
    path = path.resolve()
    source = metadata_from.resolve() if metadata_from is not None else path
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if source.exists():
            stat = source.stat()
            os.chmod(temporary, stat.st_mode)
            try:
                os.chown(temporary, stat.st_uid, stat.st_gid)
            except (AttributeError, PermissionError):
                pass
            _copy_xattrs(source, temporary)
        if mode is not None:
            os.chmod(temporary, mode)
        # chmod/chown/xattr updates dirty the inode after the data fsync above.
        # Sync again so a durable rename cannot expose content without its final
        # permissions or security metadata.
        # Windows rejects fsync on a read-only descriptor; r+b works on both
        # supported host families without changing the file contents.
        with temporary.open("r+b") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
        except OSError:
            if os.name == "nt":
                return
            raise
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def inspect_firmware(data: bytes) -> dict[str, Any]:
    elf = Elf64.parse(data)
    version = elf.section_bytes(".fwversion").rstrip(b"\x00").decode("ascii", "replace")
    return {
        "size": len(data),
        "sha256": sha256_bytes(data),
        "elf_class": "ELF64 little-endian",
        "firmware_version": version,
        "section_header_offset": f"0x{elf.section_header_offset:x}",
        "sections": [
            {
                "index": section.index,
                "name": section.name,
                "offset": f"0x{section.offset:x}",
                "size": f"0x{section.size:x}",
                "sha256": (
                    sha256_bytes(elf.section_bytes(section.name))
                    if section.file_backed and section.name
                    else None
                ),
            }
            for section in elf.sections
        ],
    }
