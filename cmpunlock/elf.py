# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import struct
from dataclasses import dataclass

from .errors import FirmwareError


ELF_MAGIC = b"\x7fELF"
ELF64_HEADER_SIZE = 64
ELF64_SECTION_SIZE = 64
SHT_NOBITS = 8
_SECTION = struct.Struct("<IIQQQQIIQQ")


@dataclass(frozen=True)
class Section:
    index: int
    name_offset: int
    name: str
    section_type: int
    flags: int
    address: int
    offset: int
    size: int
    link: int
    info: int
    alignment: int
    entry_size: int

    @property
    def file_backed(self) -> bool:
        return self.section_type != SHT_NOBITS and self.size > 0

    @property
    def end(self) -> int:
        return self.offset + self.size


@dataclass(frozen=True)
class Elf64:
    data: bytes
    section_header_offset: int
    section_header_size: int
    section_count: int
    section_names_index: int
    sections: tuple[Section, ...]

    @classmethod
    def parse(cls, data: bytes, *, reject_section_overlaps: bool = True) -> "Elf64":
        if len(data) < ELF64_HEADER_SIZE:
            raise FirmwareError("firmware is shorter than an ELF64 header")
        if data[:4] != ELF_MAGIC:
            raise FirmwareError("firmware does not have ELF magic")
        if data[4] != 2 or data[5] != 1 or data[6] != 1:
            raise FirmwareError("firmware must be ELF64, little-endian, ELF version 1")

        header_size = struct.unpack_from("<H", data, 0x34)[0]
        section_offset = struct.unpack_from("<Q", data, 0x28)[0]
        section_size = struct.unpack_from("<H", data, 0x3A)[0]
        section_count = struct.unpack_from("<H", data, 0x3C)[0]
        names_index = struct.unpack_from("<H", data, 0x3E)[0]
        if header_size != ELF64_HEADER_SIZE:
            raise FirmwareError(f"unexpected ELF header size: {header_size}")
        if section_size != ELF64_SECTION_SIZE:
            raise FirmwareError(f"unexpected section-header size: {section_size}")
        if section_count == 0:
            raise FirmwareError("extended or empty section tables are unsupported")
        if names_index >= section_count:
            raise FirmwareError("section-name table index is out of range")
        table_end = section_offset + section_count * section_size
        if section_offset < header_size or table_end > len(data):
            raise FirmwareError("section-header table lies outside the firmware")

        raw_headers = [
            _SECTION.unpack_from(data, section_offset + index * section_size)
            for index in range(section_count)
        ]
        names_header = raw_headers[names_index]
        names_type = names_header[1]
        names_offset = names_header[4]
        names_size = names_header[5]
        if names_type == SHT_NOBITS or names_size == 0:
            raise FirmwareError("section-name table is not file-backed")
        if names_offset + names_size > len(data):
            raise FirmwareError("section-name table lies outside the firmware")
        names = data[names_offset : names_offset + names_size]

        sections: list[Section] = []
        for index, values in enumerate(raw_headers):
            (
                name_offset,
                section_type,
                flags,
                address,
                offset,
                size,
                link,
                info,
                alignment,
                entry_size,
            ) = values
            if name_offset >= len(names) and index != 0:
                raise FirmwareError(f"section {index} has an invalid name offset")
            if index == 0 and name_offset == 0:
                section_name = ""
            else:
                end = names.find(b"\x00", name_offset)
                if end < 0:
                    raise FirmwareError(f"section {index} name is not terminated")
                try:
                    section_name = names[name_offset:end].decode("ascii")
                except UnicodeDecodeError as exc:
                    raise FirmwareError(f"section {index} name is not ASCII") from exc
            if alignment and alignment & (alignment - 1):
                raise FirmwareError(f"section {section_name or index} has invalid alignment")
            if section_type != SHT_NOBITS and size and offset + size > len(data):
                raise FirmwareError(f"section {section_name or index} lies outside the firmware")
            sections.append(
                Section(
                    index=index,
                    name_offset=name_offset,
                    name=section_name,
                    section_type=section_type,
                    flags=flags,
                    address=address,
                    offset=offset,
                    size=size,
                    link=link,
                    info=info,
                    alignment=alignment,
                    entry_size=entry_size,
                )
            )

        if reject_section_overlaps:
            backed = sorted(
                (section for section in sections if section.file_backed),
                key=lambda section: (section.offset, section.end),
            )
            for left, right in zip(backed, backed[1:]):
                if left.end > right.offset:
                    raise FirmwareError(
                        f"ELF sections overlap: {left.name!r} and {right.name!r}"
                    )
            for section in backed:
                if section.offset < table_end and section.end > section_offset:
                    raise FirmwareError(
                        f"section {section.name!r} overlaps the active section-header table"
                    )

        return cls(
            data=data,
            section_header_offset=section_offset,
            section_header_size=section_size,
            section_count=section_count,
            section_names_index=names_index,
            sections=tuple(sections),
        )

    def section(self, name: str) -> Section:
        matches = [section for section in self.sections if section.name == name]
        if len(matches) != 1:
            raise FirmwareError(f"expected exactly one ELF section named {name!r}")
        return matches[0]

    def section_bytes(self, name: str) -> bytes:
        section = self.section(name)
        if not section.file_backed:
            raise FirmwareError(f"ELF section {name!r} is not file-backed")
        return self.data[section.offset : section.end]


def align_up(value: int, alignment: int) -> int:
    if alignment <= 1:
        return value
    return (value + alignment - 1) & ~(alignment - 1)


def expand_section_preserving_elf(
    source: bytes, section_name: str, replacement: bytes
) -> tuple[bytes, tuple[str, ...]]:
    elf = Elf64.parse(source)
    target = elf.section(section_name)
    if not target.file_backed:
        raise FirmwareError(f"target section {section_name!r} is not file-backed")
    if len(replacement) < target.size:
        raise FirmwareError("replacement must not shrink the target section")

    overwrite_start = target.offset
    overwrite_end = target.offset + len(replacement)
    output = bytearray(source)
    if overwrite_end > len(output):
        output.extend(b"\x00" * (overwrite_end - len(output)))
    output[overwrite_start:overwrite_end] = replacement

    headers = bytearray(
        source[
            elf.section_header_offset :
            elf.section_header_offset + elf.section_count * elf.section_header_size
        ]
    )
    struct.pack_into(
        "<Q", headers, target.index * elf.section_header_size + 0x20, len(replacement)
    )

    relocated: list[str] = []
    for section in elf.sections:
        if section.index == target.index or not section.file_backed:
            continue
        if section.offset >= overwrite_end or section.end <= overwrite_start:
            continue
        new_offset = align_up(len(output), max(1, section.alignment))
        output.extend(b"\x00" * (new_offset - len(output)))
        output.extend(source[section.offset : section.end])
        struct.pack_into(
            "<Q", headers, section.index * elf.section_header_size + 0x18, new_offset
        )
        relocated.append(section.name)

    new_table_offset = align_up(len(output), 8)
    output.extend(b"\x00" * (new_table_offset - len(output)))
    output.extend(headers)
    struct.pack_into("<Q", output, 0x28, new_table_offset)

    reparsed = Elf64.parse(bytes(output))
    expanded = reparsed.section(section_name)
    if expanded.offset != target.offset or expanded.size != len(replacement):
        raise FirmwareError("patched ELF did not preserve the target section contract")
    if reparsed.section_bytes(section_name) != replacement:
        raise FirmwareError("patched ELF payload differs after reparse")
    for section in elf.sections:
        if section.index == target.index or not section.file_backed:
            continue
        if reparsed.section_bytes(section.name) != elf.section_bytes(section.name):
            raise FirmwareError(f"patch changed preserved section {section.name!r}")
    return bytes(output), tuple(relocated)

