# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import copy

import pytest

from cmpunlock.errors import ProfileError
from cmpunlock.profile import FirmwareProfile, bundled_profile_paths, load_profile, parse_int


def test_bundled_profiles_load_and_exclude_a100() -> None:
    profiles = [load_profile(path) for path in bundled_profile_paths()]

    assert {profile.driver_version for profile in profiles} == {"580.105.08", "580.126.09"}
    assert all(profile.accepted_device_ids == ("2082", "20c2") for profile in profiles)
    assert all("20b0" not in profile.accepted_device_ids for profile in profiles)


@pytest.mark.parametrize("device_id", ["20b0", "20B0"])
def test_profile_rejects_a100_device_id(
    profile_580_105_raw: dict[str, object], device_id: str
) -> None:
    raw = copy.deepcopy(profile_580_105_raw)
    raw["device"]["accepted_pci_device_ids"].append(device_id)  # type: ignore[index]

    with pytest.raises(ProfileError, match="20b0 is an A100"):
        FirmwareProfile.from_dict(raw)


@pytest.mark.parametrize("schema_version", [None, 0, 2, "1"])
def test_profile_rejects_unknown_schema_version(
    profile_580_105_raw: dict[str, object], schema_version: object
) -> None:
    raw = copy.deepcopy(profile_580_105_raw)
    raw["schema_version"] = schema_version

    with pytest.raises(ProfileError, match="schema_version"):
        FirmwareProfile.from_dict(raw)


@pytest.mark.parametrize("value", [True, False, None, [], {}])
def test_parse_int_rejects_non_integer_types(value: object) -> None:
    with pytest.raises(ProfileError, match="must be an integer"):
        parse_int(value, "field")


def test_profile_rejects_frame_outside_payload(profile_580_105_raw: dict[str, object]) -> None:
    raw = copy.deepcopy(profile_580_105_raw)
    raw["exploit"]["frame_start"] = "0xfff0"  # type: ignore[index]

    with pytest.raises(ProfileError, match="ROP frames lie outside"):
        FirmwareProfile.from_dict(raw)


def test_profile_rejects_misaligned_bar0_write(profile_580_105_raw: dict[str, object]) -> None:
    raw = copy.deepcopy(profile_580_105_raw)
    raw["compute"]["host_writes"][0]["address"] = "0x82381d"  # type: ignore[index]

    with pytest.raises(ProfileError, match="aligned 32-bit BAR0 offset"):
        FirmwareProfile.from_dict(raw)


@pytest.mark.parametrize("proof_fill", [-1, "0x100000000"])
def test_profile_rejects_proof_fill_outside_dword(
    profile_580_105_raw: dict[str, object], proof_fill: object
) -> None:
    raw = copy.deepcopy(profile_580_105_raw)
    raw["exploit"]["proof_fill"] = proof_fill  # type: ignore[index]

    with pytest.raises(ProfileError, match="proof_fill"):
        FirmwareProfile.from_dict(raw)


@pytest.mark.parametrize(
    ("field", "value"),
    [("bar0_write_gadget", "0x100000000"), ("tail_return", -1)],
)
def test_profile_rejects_control_flow_value_outside_dword(
    profile_580_105_raw: dict[str, object], field: str, value: object
) -> None:
    raw = copy.deepcopy(profile_580_105_raw)
    raw["exploit"][field] = value  # type: ignore[index]

    with pytest.raises(ProfileError, match=field):
        FirmwareProfile.from_dict(raw)


def test_profile_lookup_requires_explicit_choice() -> None:
    with pytest.raises(ProfileError, match="select a profile explicitly"):
        load_profile()


def test_profile_lookup_rejects_unknown_profile() -> None:
    with pytest.raises(ProfileError, match="profile not found"):
        load_profile("does-not-exist")
