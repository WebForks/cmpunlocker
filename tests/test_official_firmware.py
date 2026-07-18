# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

from pathlib import Path

import pytest

from cmpunlock.elf import Elf64
from cmpunlock.firmware import patch_firmware, sha256_bytes, validate_stock_firmware
from cmpunlock.payload import build_payload
from cmpunlock.profile import load_profile


FIXTURES = [
    pytest.param(
        "580.105.08",
        [
            Path("C:/tmp/nvidia-driver-58010508/firmware/gsp_tu10x.bin"),
            Path("/tmp/nvidia-driver-58010508/firmware/gsp_tu10x.bin"),
        ],
        "84e0f47adc5b7f40a5789f1e3d528ca1269bd6184029dec0af6c76f9f282d0d7",
        {
            "proof": "afcd18a9118e3f9f766562fb8b9cf6b0b38afdd452ee2c7a66ab6c057847d2cb",
            "compute": "804dfe02ac093d01b32d1189c9fed46364b599b59089b7c34236a9d0570d1213",
        },
        30_387_288,
        id="580.105.08",
    ),
    pytest.param(
        "580.126.09",
        [
            Path("C:/tmp/rpm-fw-580.126.09/lib/firmware/nvidia/580.126.09/gsp_tu10x.bin"),
            Path("/tmp/rpm-fw-580.126.09/lib/firmware/nvidia/580.126.09/gsp_tu10x.bin"),
        ],
        "a3788bfb368bdd2384a8b1aceeb946f2b0e1dff734d9f3fdca65e7f727ed42b7",
        {
            "proof": "9b2414b3580351feb52ab0dcb2a6990a1fa6185ace8e7a47de5e27bcb5080419",
            "compute": "c6be2bfca7b119d7dfd6d2e2c36ca061f3cad373f2adca7843bd4d064a12d5dc",
        },
        30_497_880,
        id="580.126.09",
    ),
    pytest.param(
        "580.173.02",
        [
            Path(
                "C:/tmp/nvidia-driver-58017302/extracted/firmware/gsp_tu10x.bin"
            ),
            Path(
                "/tmp/nvidia-driver-58017302/extracted/firmware/gsp_tu10x.bin"
            ),
        ],
        "6f3ccbd570c7ac2a7ea910d9d87fc3d23db9ae3dfe82020ea07b17a30954495e",
        {
            "proof": "7ed44320995f068f15c082d116f8c15319ea6b11d497914fc21c75b92b81c54e",
            "compute": "6b57e314f980e0d2f343ee9604e59b440a668f6797e9358acf2b8a7333468c85",
        },
        30_530_648,
        id="580.173.02",
    ),
]


def _first_existing(candidates: list[Path]) -> Path:
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    pytest.skip("official NVIDIA firmware fixture is not cached under C:/tmp")


@pytest.mark.parametrize(
    ("version", "candidates", "stock_sha256", "patched_sha256", "patched_size"),
    FIXTURES,
)
def test_exact_official_firmware_and_patched_images(
    version: str,
    candidates: list[Path],
    stock_sha256: str,
    patched_sha256: dict[str, str],
    patched_size: int,
) -> None:
    path = _first_existing(candidates)
    source = path.read_bytes()
    profile = load_profile(version)
    original = validate_stock_firmware(source, profile)

    assert sha256_bytes(source) == stock_sha256
    assert original.section_bytes(".fwversion").rstrip(b"\x00") == version.encode("ascii")
    preserved = {
        section.name: original.section_bytes(section.name)
        for section in original.sections
        if section.file_backed and section.name != profile.signature_section
    }

    for mode in ("proof", "compute"):
        payload, payload_report = build_payload(profile, mode)
        patched, report = patch_firmware(source, payload, payload_report, profile)
        reparsed = Elf64.parse(patched)

        assert len(patched) == patched_size
        assert report.patched_sha256 == patched_sha256[mode]
        assert reparsed.section(profile.signature_section).offset == profile.signature_offset
        assert reparsed.section_bytes(profile.signature_section) == payload
        assert set(report.relocated_sections) == {
            ".fwsignature_tu11x",
            ".fwsignature_tu10x",
            ".symtab",
            ".strtab",
            ".shstrtab",
        }
        for name, contents in preserved.items():
            assert reparsed.section_bytes(name) == contents
