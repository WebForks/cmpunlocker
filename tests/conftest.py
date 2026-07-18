# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import json
import struct
import tempfile
from pathlib import Path

import pytest

from cmpunlock.profile import load_profile


ELF64_HEADER = struct.Struct("<16sHHIQQQIHHHHHH")
ELF64_SECTION = struct.Struct("<IIQQQQIIQQ")


@pytest.fixture
def profile_580_105():
    return load_profile("580.105.08")


@pytest.fixture
def profile_580_105_raw() -> dict[str, object]:
    path = Path(__file__).parents[1] / "cmpunlock" / "profiles" / "580.105.08.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _align(value: int, alignment: int) -> int:
    return (value + alignment - 1) & ~(alignment - 1)


def build_test_elf() -> bytes:
    """Build a small, valid ELF64 image with sections after the patch target."""
    names = (
        b"\x00.fwimage\x00.fwversion\x00.fwsignature_ga100\x00"
        b".after\x00.shstrtab\x00"
    )
    specs = [
        ("", 0, 0, b""),
        (".fwimage", 1, 8, b"IMAGE-CONTENTS!!"),
        (".fwversion", 1, 1, b"test.1\x00"),
        (".fwsignature_ga100", 1, 1, b"SIGNHERE"),
        (".after", 1, 8, b"PRESERVE-AFTER"),
        (".shstrtab", 3, 1, names),
    ]

    cursor = 0x80
    sections: list[tuple[str, int, int, int, bytes]] = []
    for name, section_type, alignment, contents in specs:
        if not name:
            sections.append((name, section_type, alignment, 0, contents))
            continue
        cursor = _align(cursor, alignment)
        sections.append((name, section_type, alignment, cursor, contents))
        cursor += len(contents)

    section_header_offset = _align(cursor, 8)
    output = bytearray(section_header_offset + len(sections) * ELF64_SECTION.size)
    ident = b"\x7fELF" + bytes((2, 1, 1, 0)) + b"\x00" * 8
    ELF64_HEADER.pack_into(
        output,
        0,
        ident,
        1,
        0xF3,
        1,
        0,
        0,
        section_header_offset,
        0,
        ELF64_HEADER.size,
        0,
        0,
        ELF64_SECTION.size,
        len(sections),
        5,
    )

    for index, (name, section_type, alignment, offset, contents) in enumerate(sections):
        if contents:
            output[offset : offset + len(contents)] = contents
        name_offset = names.find(name.encode("ascii")) if name else 0
        ELF64_SECTION.pack_into(
            output,
            section_header_offset + index * ELF64_SECTION.size,
            name_offset,
            section_type,
            0,
            0,
            offset,
            len(contents),
            0,
            0,
            alignment,
            0,
        )
    return bytes(output)


@pytest.fixture
def test_elf() -> bytes:
    return build_test_elf()


@pytest.fixture
def local_tmp_path() -> Path:
    """Avoid relying on the host's global pytest temp directory permissions."""
    base = Path(__file__).parents[1] / "tmp" / "pytest"
    base.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=base) as directory:
        yield Path(directory)
