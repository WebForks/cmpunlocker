# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import ProfileError


def parse_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise ProfileError(f"{field} must be an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value, 0)
        except ValueError as exc:
            raise ProfileError(f"{field} is not an integer: {value!r}") from exc
    raise ProfileError(f"{field} must be an integer")


def _require_hex_digest(value: Any, field: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise ProfileError(f"{field} must be a 64-character SHA-256 digest")
    try:
        bytes.fromhex(value)
    except ValueError as exc:
        raise ProfileError(f"{field} is not hexadecimal") from exc
    return value.lower()


@dataclass(frozen=True)
class RegisterWrite:
    address: int
    value: int
    name: str

    @classmethod
    def from_dict(cls, value: Any, field: str) -> "RegisterWrite":
        if not isinstance(value, dict):
            raise ProfileError(f"{field} must be an object")
        name = value.get("name")
        if not isinstance(name, str) or not name:
            raise ProfileError(f"{field}.name must be a non-empty string")
        address = parse_int(value.get("address"), f"{field}.address")
        register_value = parse_int(value.get("value"), f"{field}.value")
        if address < 0 or address > 0xFFFFFFFF or address % 4:
            raise ProfileError(f"{field}.address must be an aligned 32-bit BAR0 offset")
        if register_value < 0 or register_value > 0xFFFFFFFF:
            raise ProfileError(f"{field}.value must fit in 32 bits")
        return cls(address=address, value=register_value, name=name)


@dataclass(frozen=True)
class FirmwareProfile:
    profile_id: str
    evidence: str
    driver_version: str
    firmware_size: int
    firmware_sha256: str
    signature_section: str
    signature_offset: int
    signature_size: int
    signature_sha256: str
    booter_size: int
    booter_compressed_size: int
    booter_compressed_prefix: bytes
    booter_sha256: str
    accepted_device_ids: tuple[str, ...]
    dma_target: int
    payload_size: int
    guard_address: int
    proof_fill: int
    canary_replacement: int
    frame_start: int
    frame_stride: int
    frame_fields: tuple[int, int, int, int, int, int]
    bar0_write_gadget: int
    tail_return: int
    hs_writes: tuple[RegisterWrite, ...]
    host_writes: tuple[RegisterWrite, ...]
    plm_readback_address: int
    plm_open_value: int

    @classmethod
    def from_dict(cls, raw: Any) -> "FirmwareProfile":
        if not isinstance(raw, dict):
            raise ProfileError("profile must be a JSON object")
        if raw.get("schema_version") != 1:
            raise ProfileError("unsupported profile schema_version")

        def text(name: str) -> str:
            value = raw.get(name)
            if not isinstance(value, str) or not value:
                raise ProfileError(f"{name} must be a non-empty string")
            return value

        firmware = raw.get("firmware")
        booter = raw.get("booter")
        device = raw.get("device")
        exploit = raw.get("exploit")
        compute = raw.get("compute")
        for name, value in (
            ("firmware", firmware),
            ("booter", booter),
            ("device", device),
            ("exploit", exploit),
            ("compute", compute),
        ):
            if not isinstance(value, dict):
                raise ProfileError(f"{name} must be an object")

        ids = device.get("accepted_pci_device_ids")
        if not isinstance(ids, list) or not ids:
            raise ProfileError("device.accepted_pci_device_ids must be a non-empty list")
        normalized_ids: list[str] = []
        for value in ids:
            if not isinstance(value, str) or len(value) != 4:
                raise ProfileError("PCI device IDs must contain four hex characters")
            try:
                int(value, 16)
            except ValueError as exc:
                raise ProfileError(f"invalid PCI device ID: {value!r}") from exc
            normalized_ids.append(value.lower())
        if "20b0" in normalized_ids:
            raise ProfileError("PCI ID 20b0 is an A100, not a CMP 170HX")

        offsets = exploit.get("frame_field_offsets")
        if not isinstance(offsets, list) or len(offsets) != 6:
            raise ProfileError("exploit.frame_field_offsets must contain six entries")
        parsed_offsets = tuple(
            parse_int(value, f"exploit.frame_field_offsets[{index}]")
            for index, value in enumerate(offsets)
        )

        hs_raw = exploit.get("hs_writes")
        host_raw = compute.get("host_writes")
        if not isinstance(hs_raw, list) or not hs_raw:
            raise ProfileError("exploit.hs_writes must be a non-empty list")
        if not isinstance(host_raw, list) or not host_raw:
            raise ProfileError("compute.host_writes must be a non-empty list")

        prefix = booter.get("compressed_prefix_hex")
        if not isinstance(prefix, str) or len(prefix) < 32 or len(prefix) % 2:
            raise ProfileError("booter.compressed_prefix_hex must contain at least 16 bytes")
        try:
            prefix_bytes = bytes.fromhex(prefix)
        except ValueError as exc:
            raise ProfileError("booter.compressed_prefix_hex is not hexadecimal") from exc

        profile = cls(
            profile_id=text("profile_id"),
            evidence=text("evidence"),
            driver_version=text("driver_version"),
            firmware_size=parse_int(firmware.get("size"), "firmware.size"),
            firmware_sha256=_require_hex_digest(firmware.get("sha256"), "firmware.sha256"),
            signature_section=str(firmware.get("signature_section", "")),
            signature_offset=parse_int(
                firmware.get("signature_offset"), "firmware.signature_offset"
            ),
            signature_size=parse_int(firmware.get("signature_size"), "firmware.signature_size"),
            signature_sha256=_require_hex_digest(
                firmware.get("signature_sha256"), "firmware.signature_sha256"
            ),
            booter_size=parse_int(booter.get("size"), "booter.size"),
            booter_compressed_size=parse_int(
                booter.get("compressed_size"), "booter.compressed_size"
            ),
            booter_compressed_prefix=prefix_bytes,
            booter_sha256=_require_hex_digest(booter.get("sha256"), "booter.sha256"),
            accepted_device_ids=tuple(normalized_ids),
            dma_target=parse_int(exploit.get("dma_target"), "exploit.dma_target"),
            payload_size=parse_int(exploit.get("payload_size"), "exploit.payload_size"),
            guard_address=parse_int(exploit.get("guard_address"), "exploit.guard_address"),
            proof_fill=parse_int(exploit.get("proof_fill"), "exploit.proof_fill"),
            canary_replacement=parse_int(
                exploit.get("canary_replacement"), "exploit.canary_replacement"
            ),
            frame_start=parse_int(exploit.get("frame_start"), "exploit.frame_start"),
            frame_stride=parse_int(exploit.get("frame_stride"), "exploit.frame_stride"),
            frame_fields=parsed_offsets,
            bar0_write_gadget=parse_int(
                exploit.get("bar0_write_gadget"), "exploit.bar0_write_gadget"
            ),
            tail_return=parse_int(exploit.get("tail_return"), "exploit.tail_return"),
            hs_writes=tuple(
                RegisterWrite.from_dict(value, f"exploit.hs_writes[{index}]")
                for index, value in enumerate(hs_raw)
            ),
            host_writes=tuple(
                RegisterWrite.from_dict(value, f"compute.host_writes[{index}]")
                for index, value in enumerate(host_raw)
            ),
            plm_readback_address=parse_int(
                compute.get("plm_readback_address"), "compute.plm_readback_address"
            ),
            plm_open_value=parse_int(compute.get("plm_open_value"), "compute.plm_open_value"),
        )
        profile.validate()
        return profile

    def validate(self) -> None:
        if self.evidence not in {"paper-proof", "community-unverified"}:
            raise ProfileError(f"unknown evidence level: {self.evidence}")
        if not self.signature_section:
            raise ProfileError("firmware.signature_section must be non-empty")
        if self.firmware_size <= 0 or self.signature_size <= 0 or self.signature_offset < 0:
            raise ProfileError("firmware and signature sizes must be positive")
        if self.signature_offset + self.signature_size > self.firmware_size:
            raise ProfileError("signature section lies outside the firmware")
        if self.booter_size <= 0 or self.booter_compressed_size <= 0:
            raise ProfileError("booter sizes must be positive")
        if self.booter_compressed_size < len(self.booter_compressed_prefix):
            raise ProfileError("booter compressed size is shorter than its search prefix")
        if len(set(self.accepted_device_ids)) != len(self.accepted_device_ids):
            raise ProfileError("accepted PCI device IDs must be unique")
        if self.payload_size != 0xF800:
            raise ProfileError("this implementation only supports the paper's 0xF800 payload")
        if self.dma_target != 0x800 or self.guard_address != 0x6340:
            raise ProfileError("DMEM layout does not match the published paper")
        for name, value in (
            ("exploit.proof_fill", self.proof_fill),
            ("exploit.canary_replacement", self.canary_replacement),
            ("exploit.bar0_write_gadget", self.bar0_write_gadget),
            ("exploit.tail_return", self.tail_return),
            ("compute.plm_open_value", self.plm_open_value),
        ):
            if value < 0 or value > 0xFFFFFFFF:
                raise ProfileError(f"{name} must fit in 32 bits")
        for name, value in (
            ("exploit.dma_target", self.dma_target),
            ("exploit.guard_address", self.guard_address),
            ("exploit.frame_start", self.frame_start),
            ("compute.plm_readback_address", self.plm_readback_address),
        ):
            if value < 0 or value > 0xFFFFFFFF or value % 4:
                raise ProfileError(f"{name} must be an aligned 32-bit address")
        if self.payload_size % 4:
            raise ProfileError("payload size must be dword-aligned")
        if any(value < 0 for value in self.frame_fields):
            raise ProfileError("frame field offsets must be nonnegative")
        if self.frame_stride <= 0 or self.frame_stride % 4:
            raise ProfileError("frame stride must be a positive dword multiple")
        if sorted(self.frame_fields) != list(self.frame_fields):
            raise ProfileError("frame field offsets must be increasing")
        if self.frame_fields[-1] + 4 > self.frame_stride:
            raise ProfileError("frame fields exceed the frame stride")
        payload_end = self.dma_target + self.payload_size
        final_frame_end = self.frame_start + self.frame_stride * (len(self.hs_writes) + 1)
        if not self.dma_target <= self.guard_address < payload_end:
            raise ProfileError("guard address lies outside the DMA payload")
        if final_frame_end > payload_end:
            raise ProfileError("ROP frames lie outside the DMA payload")

    def to_summary(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "evidence": self.evidence,
            "driver_version": self.driver_version,
            "firmware_sha256": self.firmware_sha256,
            "booter_sha256": self.booter_sha256,
            "accepted_pci_device_ids": list(self.accepted_device_ids),
        }


def bundled_profile_paths() -> list[Path]:
    return sorted((Path(__file__).parent / "profiles").glob("*.json"))


def load_profile(path_or_id: str | Path | None = None) -> FirmwareProfile:
    candidates = bundled_profile_paths()
    if path_or_id is None:
        if len(candidates) != 1:
            raise ProfileError("select a profile explicitly")
        selected = candidates[0]
    else:
        requested = Path(path_or_id)
        if requested.is_file():
            selected = requested
        else:
            selected = next(
                (
                    path
                    for path in candidates
                    if path.stem == str(path_or_id)
                    or json.loads(path.read_text(encoding="utf-8")).get("profile_id")
                    == str(path_or_id)
                ),
                None,
            )
            if selected is None:
                raise ProfileError(f"profile not found: {path_or_id}")
    try:
        raw = json.loads(selected.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProfileError(f"cannot read profile {selected}: {exc}") from exc
    return FirmwareProfile.from_dict(raw)


def match_profile_for_firmware(path: Path) -> FirmwareProfile:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    for candidate in bundled_profile_paths():
        profile = load_profile(candidate)
        if profile.firmware_sha256 == digest:
            return profile
    raise ProfileError(
        f"no bundled profile matches firmware SHA-256 {digest}; refusing an unpinned image"
    )
