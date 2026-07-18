# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import struct

import pytest

from cmpunlock.elf import ELF64_HEADER_SIZE, ELF64_SECTION_SIZE, Elf64, expand_section_preserving_elf
from cmpunlock.errors import FirmwareError


def test_parse_valid_elf(test_elf: bytes) -> None:
    elf = Elf64.parse(test_elf)

    assert elf.section_header_size == ELF64_SECTION_SIZE
    assert elf.section(".fwversion").offset >= ELF64_HEADER_SIZE
    assert elf.section_bytes(".fwversion") == b"test.1\x00"
    assert elf.section_bytes(".fwsignature_ga100") == b"SIGNHERE"


def test_expand_section_relocates_overwritten_sections_and_preserves_bytes(
    test_elf: bytes,
) -> None:
    original = Elf64.parse(test_elf)
    replacement = bytes(range(96))

    patched, relocated = expand_section_preserving_elf(
        test_elf, ".fwsignature_ga100", replacement
    )
    reparsed = Elf64.parse(patched)

    assert reparsed.section(".fwsignature_ga100").offset == original.section(
        ".fwsignature_ga100"
    ).offset
    assert reparsed.section_bytes(".fwsignature_ga100") == replacement
    assert set(relocated) == {".after", ".shstrtab"}
    assert reparsed.section_bytes(".fwimage") == original.section_bytes(".fwimage")
    assert reparsed.section_bytes(".fwversion") == original.section_bytes(".fwversion")
    assert reparsed.section_bytes(".after") == original.section_bytes(".after")
    assert reparsed.section_bytes(".shstrtab") == original.section_bytes(".shstrtab")
    assert reparsed.section_header_offset > original.section_header_offset


def test_expand_rejects_shrinking_target(test_elf: bytes) -> None:
    with pytest.raises(FirmwareError, match="must not shrink"):
        expand_section_preserving_elf(test_elf, ".fwsignature_ga100", b"short")


@pytest.mark.parametrize(
    ("offset", "encoded", "message"),
    [
        (0, b"NOPE", "ELF magic"),
        (4, b"\x01", "ELF64"),
        (5, b"\x02", "little-endian"),
        (0x34, struct.pack("<H", 63), "header size"),
        (0x3A, struct.pack("<H", 40), "section-header size"),
        (0x3C, struct.pack("<H", 0), "empty section tables"),
        (0x3E, struct.pack("<H", 99), "name table index"),
    ],
)
def test_parse_rejects_malformed_elf_header(
    test_elf: bytes, offset: int, encoded: bytes, message: str
) -> None:
    malformed = bytearray(test_elf)
    malformed[offset : offset + len(encoded)] = encoded

    with pytest.raises(FirmwareError, match=message):
        Elf64.parse(bytes(malformed))


def test_parse_rejects_section_table_outside_file(test_elf: bytes) -> None:
    malformed = bytearray(test_elf)
    struct.pack_into("<Q", malformed, 0x28, len(malformed) - 1)

    with pytest.raises(FirmwareError, match="section-header table lies outside"):
        Elf64.parse(bytes(malformed))


def test_parse_rejects_invalid_section_name_offset(test_elf: bytes) -> None:
    malformed = bytearray(test_elf)
    section_table = struct.unpack_from("<Q", malformed, 0x28)[0]
    struct.pack_into("<I", malformed, section_table + ELF64_SECTION_SIZE, 0xFFFFFFFF)

    with pytest.raises(FirmwareError, match="invalid name offset"):
        Elf64.parse(bytes(malformed))


def test_parse_rejects_invalid_section_alignment(test_elf: bytes) -> None:
    malformed = bytearray(test_elf)
    section_table = struct.unpack_from("<Q", malformed, 0x28)[0]
    struct.pack_into("<Q", malformed, section_table + ELF64_SECTION_SIZE + 0x30, 3)

    with pytest.raises(FirmwareError, match="invalid alignment"):
        Elf64.parse(bytes(malformed))


def test_parse_rejects_overlapping_sections(test_elf: bytes) -> None:
    malformed = bytearray(test_elf)
    parsed = Elf64.parse(test_elf)
    section_table = parsed.section_header_offset
    after_header = section_table + parsed.section(".after").index * ELF64_SECTION_SIZE
    target = parsed.section(".fwsignature_ga100")
    struct.pack_into("<Q", malformed, after_header + 0x18, target.offset + 1)

    with pytest.raises(FirmwareError, match="sections overlap"):
        Elf64.parse(bytes(malformed))


def test_parse_can_explicitly_allow_section_overlaps(test_elf: bytes) -> None:
    malformed = bytearray(test_elf)
    parsed = Elf64.parse(test_elf)
    after_header = (
        parsed.section_header_offset + parsed.section(".after").index * ELF64_SECTION_SIZE
    )
    target = parsed.section(".fwsignature_ga100")
    struct.pack_into("<Q", malformed, after_header + 0x18, target.offset + 1)

    reparsed = Elf64.parse(bytes(malformed), reject_section_overlaps=False)
    assert reparsed.section(".after").offset == target.offset + 1
