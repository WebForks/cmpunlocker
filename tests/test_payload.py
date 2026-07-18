# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import hashlib
import struct
from dataclasses import replace

import pytest

from cmpunlock.errors import FirmwareError
from cmpunlock.payload import build_compute_payload, build_payload, build_proof_payload


PROOF_SHA256 = "cb2be639efc80feae569d275e5c96563024aebf1c8997b8ebe84e5746ffc5ba7"
COMPUTE_SHA256 = "48705e7cadc441ad728d73cf77229d78e23d7ef05ac22c6a808355ea17efb6c6"


def _dword(payload: bytes, dmem_address: int, dma_target: int) -> int:
    return struct.unpack_from("<I", payload, dmem_address - dma_target)[0]


def test_proof_payload_is_deterministic_uniform_fill(profile_580_105) -> None:
    first, first_report = build_proof_payload(profile_580_105)
    second, second_report = build_proof_payload(profile_580_105)

    assert first == second
    assert first_report == second_report
    assert len(first) == 0xF800
    assert first == struct.pack("<I", 0x4A7) * (0xF800 // 4)
    assert hashlib.sha256(first).hexdigest() == PROOF_SHA256
    assert first_report.sha256 == PROOF_SHA256
    assert first_report.nonzero_bytes == 0xF800 // 2
    assert first_report.nonzero_dwords == 0xF800 // 4


def test_compute_payload_is_deterministic_and_encodes_frames(profile_580_105) -> None:
    payload, report = build_compute_payload(profile_580_105)
    repeated, repeated_report = build_compute_payload(profile_580_105)

    assert payload == repeated
    assert report == repeated_report
    assert len(payload) == profile_580_105.payload_size
    assert report.sha256 == COMPUTE_SHA256
    assert report.nonzero_bytes == 55
    assert report.nonzero_dwords == 18
    assert _dword(payload, profile_580_105.guard_address, profile_580_105.dma_target) == 0xFACEB13D

    r0, r1, r2, r3, saved, return_pc = profile_580_105.frame_fields
    frame = profile_580_105.frame_start
    for write in profile_580_105.hs_writes:
        assert _dword(payload, frame + r0, profile_580_105.dma_target) == profile_580_105.guard_address
        assert _dword(payload, frame + r1, profile_580_105.dma_target) == 0
        assert _dword(payload, frame + r2, profile_580_105.dma_target) == write.value
        assert _dword(payload, frame + r3, profile_580_105.dma_target) == write.address
        assert _dword(payload, frame + saved, profile_580_105.dma_target) == 0xFACEB13D
        assert _dword(payload, frame + return_pc, profile_580_105.dma_target) == 0x10B9
        frame += profile_580_105.frame_stride

    assert _dword(payload, frame + saved, profile_580_105.dma_target) == 0xFACEB13D
    assert _dword(payload, frame + return_pc, profile_580_105.dma_target) == 0x810D


def test_compute_payload_rejects_frame_beyond_dma_buffer(profile_580_105) -> None:
    invalid = replace(
        profile_580_105,
        frame_start=profile_580_105.dma_target + profile_580_105.payload_size - 4,
    )

    with pytest.raises(FirmwareError, match="outside the payload"):
        build_compute_payload(invalid)


def test_compute_payload_rejects_misaligned_dmem_address(profile_580_105) -> None:
    invalid = replace(profile_580_105, guard_address=profile_580_105.guard_address + 1)

    with pytest.raises(FirmwareError, match="not dword-aligned"):
        build_compute_payload(invalid)


def test_proof_payload_rejects_unaligned_size(profile_580_105) -> None:
    invalid = replace(profile_580_105, payload_size=profile_580_105.payload_size - 1)

    with pytest.raises(FirmwareError, match="dword-aligned"):
        build_proof_payload(invalid)


def test_payload_dispatch_rejects_unknown_mode(profile_580_105) -> None:
    with pytest.raises(FirmwareError, match="unknown payload mode"):
        build_payload(profile_580_105, "memory")
