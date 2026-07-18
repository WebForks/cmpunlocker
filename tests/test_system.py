# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import contextlib
import hashlib
import inspect
import json
import zlib
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator

import pytest

from cmpunlock import system
from cmpunlock.errors import ApplyError, SystemCheckError
from cmpunlock.system import Bar0, PciDevice


_NVIDIA_SMI = str(Path("/usr/bin/nvidia-smi").resolve())


def _device(
    profile,
    *,
    device_id: str | None = None,
    vendor_id: str = "10de",
    driver: str | None = None,
    resource0_size: int | None = None,
) -> PciDevice:
    required_size = max(
        profile.plm_readback_address,
        *(write.address for write in profile.host_writes),
        *(write.address for write in profile.hs_writes),
    ) + 4
    return PciDevice(
        bdf="0000:01:00.0",
        vendor_id=vendor_id,
        device_id=device_id or profile.accepted_device_ids[0],
        subsystem_vendor_id="10de",
        subsystem_device_id="0000",
        class_code="030200",
        driver=driver,
        resource0_size=required_size if resource0_size is None else resource0_size,
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("01:00.0", "0000:01:00.0"),
        ("ABCD:EF:01.7", "abcd:ef:01.7"),
        (" 0000:0A:0B.2 ", "0000:0a:0b.2"),
    ],
)
def test_normalize_bdf(value: str, expected: str) -> None:
    assert system.normalize_bdf(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "",
        "01:00",
        "01:00.8",
        "000:01:00.0",
        "0000:0g:00.0",
        "0000:01:000.0",
        "0000:01:00.0/../../reset",
    ],
)
def test_normalize_bdf_rejects_malformed_values(value: str) -> None:
    with pytest.raises(SystemCheckError, match="invalid PCI BDF"):
        system.normalize_bdf(value)


def test_validate_target_explicitly_rejects_a100(profile_580_105) -> None:
    with pytest.raises(SystemCheckError, match="20b0 is an A100 SXM4"):
        system.validate_target(_device(profile_580_105, device_id="20b0"), profile_580_105)


def test_validate_target_rejects_bar0_one_byte_too_small(profile_580_105) -> None:
    minimum = max(
        profile_580_105.plm_readback_address,
        *(write.address for write in profile_580_105.host_writes),
        *(write.address for write in profile_580_105.hs_writes),
    ) + 4

    system.validate_target(
        _device(profile_580_105, resource0_size=minimum), profile_580_105
    )
    with pytest.raises(SystemCheckError, match="BAR0 is too small"):
        system.validate_target(
            _device(profile_580_105, resource0_size=minimum - 1), profile_580_105
        )


def test_reported_profile_requires_bar0_for_diagnostic_hs_addresses(
    profile_580_173,
) -> None:
    minimum = max(write.address for write in profile_580_173.hs_writes) + 4

    system.validate_target(
        _device(profile_580_173, resource0_size=minimum), profile_580_173
    )
    with pytest.raises(SystemCheckError, match="BAR0 is too small"):
        system.validate_target(
            _device(profile_580_173, resource0_size=minimum - 1), profile_580_173
        )


def test_reported_profile_is_restricted_to_reported_20c2_device(
    profile_580_173,
) -> None:
    system.validate_target(_device(profile_580_173), profile_580_173)

    with pytest.raises(SystemCheckError, match="expected one of: 20c2"):
        system.validate_target(
            _device(profile_580_173, device_id="2082"), profile_580_173
        )


def test_validate_target_rejects_non_nvidia_vendor(profile_580_105) -> None:
    with pytest.raises(SystemCheckError, match="not an NVIDIA device"):
        system.validate_target(
            _device(profile_580_105, vendor_id="1234"), profile_580_105
        )


def test_validate_target_rejects_unreviewed_nvidia_device(profile_580_105) -> None:
    custom = replace(profile_580_105, accepted_device_ids=("1e04",))

    with pytest.raises(SystemCheckError, match="reviewed CMP IDs"):
        system.validate_target(_device(custom, device_id="1e04"), custom)


def test_validate_live_profile_rejects_external_profile(profile_580_105) -> None:
    custom = replace(profile_580_105, profile_id="external-profile")

    with pytest.raises(SystemCheckError, match="unchanged bundled firmware profile"):
        system.validate_live_profile(custom)


def test_validate_live_profile_enforces_hard_coded_write_contract(
    profile_580_105, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = replace(profile_580_105.host_writes[0], address=0x100)
    custom = replace(
        profile_580_105,
        host_writes=(first, profile_580_105.host_writes[1]),
    )
    monkeypatch.setattr(system, "bundled_profile_paths", lambda: [Path("synthetic.json")])
    monkeypatch.setattr(system, "load_profile", lambda _path: custom)

    with pytest.raises(SystemCheckError, match="host BAR0 writes"):
        system.validate_live_profile(custom)


def test_validate_live_profile_accepts_exact_reported_contract(
    profile_580_173,
) -> None:
    system.validate_live_profile(profile_580_173)


def test_validate_live_profile_rejects_reported_contract_on_other_version(
    profile_580_173, monkeypatch: pytest.MonkeyPatch
) -> None:
    custom = replace(profile_580_173, driver_version="580.999.99")
    monkeypatch.setattr(system, "bundled_profile_paths", lambda: [Path("synthetic.json")])
    monkeypatch.setattr(system, "load_profile", lambda _path: custom)

    with pytest.raises(SystemCheckError, match="pinned to 580.173.02"):
        system.validate_live_profile(custom)


def test_reported_apply_plan_discloses_sequence_and_stronger_acknowledgement(
    profile_580_173, local_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    firmware = local_tmp_path / "gsp_tu10x.bin"
    firmware.write_bytes(b"stock")
    monkeypatch.setattr(
        system,
        "build_compute_payload",
        lambda _profile: (b"payload", SimpleNamespace()),
    )
    monkeypatch.setattr(
        system,
        "patch_firmware",
        lambda *_args: (
            b"patched",
            SimpleNamespace(as_dict=lambda: {"patched_sha256": "f" * 64}),
        ),
    )

    plan = system.build_apply_plan("0000:01:00.0", firmware, profile_580_173)

    assert plan["required_acknowledgement"] == system.REPORTED_PATH_ACKNOWLEDGEMENT
    assert plan["memory_capacity_verified"] is False
    assert any("FLR #1" in stage and "FLR #2" in stage for stage in plan["planned_stages"])
    assert any("write only the two compute overrides" in stage for stage in plan["planned_stages"])


def test_system_inspect_requires_target_scoped_stock_nvidia_smi_probe(
    profile_580_173, local_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    firmware = local_tmp_path / "gsp_tu10x.bin"
    firmware.write_bytes(b"stock")
    device = replace(_device(profile_580_173), driver="nvidia")
    commands: list[tuple[str, ...]] = []
    monkeypatch.setattr(system, "require_linux", lambda: None)
    monkeypatch.setattr(system, "inspect_pci_device", lambda *_args, **_kwargs: device)
    monkeypatch.setattr(system, "validate_stock_firmware", lambda *_args: None)
    monkeypatch.setattr(system, "validate_module", lambda _profile: {"version": "580.173.02"})
    monkeypatch.setattr(system, "enumerate_nvidia_devices", lambda *_args: (device.bdf,))
    monkeypatch.setattr(system, "loaded_nvidia_modules", lambda: ("nvidia",))
    monkeypatch.setattr(system.shutil, "which", lambda _command: _NVIDIA_SMI)

    def run(command: list[str], **_kwargs: object):
        commands.append(tuple(command))
        return SimpleNamespace(stdout="00000000:01:00.0\n", stderr="", returncode=0)

    monkeypatch.setattr(system, "_run", run)

    result = system.inspect_system(
        device.bdf,
        firmware,
        profile_580_173,
        sysfs_root=local_tmp_path,
    )

    expected = tuple(system._nvidia_smi_command(device.bdf, _NVIDIA_SMI))
    assert commands == [expected]
    assert result["nvidia_smi"]["path"] == _NVIDIA_SMI
    assert result["nvidia_smi"]["stock_probe"]["returncode"] == 0
    assert result["nvidia_smi"]["stock_probe"]["stdout"] == "00000000:01:00.0"


def _synthetic_booter(profile):
    booter = (b"synthetic GA100 production booter\x00" * 128) + bytes(range(256)) * 8
    compressor = zlib.compressobj(level=9, wbits=-zlib.MAX_WBITS)
    compressed = compressor.compress(booter) + compressor.flush()
    prefix = compressed[:24]
    assert compressed.count(prefix) == 1
    return booter, compressed, replace(
        profile,
        booter_size=len(booter),
        booter_compressed_size=len(compressed),
        booter_compressed_prefix=prefix,
        booter_sha256=hashlib.sha256(booter).hexdigest(),
    )


def test_extract_profiled_booter_from_raw_deflate_stream(profile_580_105) -> None:
    booter, compressed, profile = _synthetic_booter(profile_580_105)
    module = b"module prefix" + compressed + b"module suffix"

    assert system.extract_profiled_booter(module, profile) == booter


@pytest.mark.parametrize("copies", [0, 2])
def test_extract_profiled_booter_rejects_missing_or_duplicate_stream(
    profile_580_105, copies: int
) -> None:
    _booter, compressed, profile = _synthetic_booter(profile_580_105)
    module = b"separator".join(compressed for _ in range(copies))

    with pytest.raises(SystemCheckError, match=rf"found {copies}"):
        system.extract_profiled_booter(module, profile)


def test_extract_profiled_booter_rejects_truncated_stream(profile_580_105) -> None:
    _booter, compressed, profile = _synthetic_booter(profile_580_105)

    with pytest.raises(SystemCheckError, match="stream is truncated"):
        system.extract_profiled_booter(b"prefix" + compressed[:-1], profile)


def _mock_mapped_bar0(monkeypatch: pytest.MonkeyPatch, *, size: int = 8):
    mapping = system.mmap.mmap(-1, size)
    mapping[0:4] = b"\x78\x56\x34\x12"
    closed: list[int] = []
    map_calls: list[tuple[int, int, int]] = []
    monkeypatch.setattr(system.os, "O_SYNC", 0, raising=False)
    monkeypatch.setattr(system.os, "open", lambda *_args: 91)
    monkeypatch.setattr(system.os, "close", closed.append)

    def open_mapping(fd: int, length: int, *, access: int):
        map_calls.append((fd, length, access))
        return mapping

    monkeypatch.setattr(system.mmap, "mmap", open_mapping)
    return mapping, closed, map_calls


def test_bar0_read_write_and_bounds(
    profile_580_105, local_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mapping, closed, map_calls = _mock_mapped_bar0(monkeypatch)
    device = replace(_device(profile_580_105), resource0_size=8)

    with Bar0(device, local_tmp_path) as bar0:
        assert bar0.read32(0) == 0x12345678
        bar0.write32(4, 0xAABBCCDD)
        assert bar0.read32(4) == 0xAABBCCDD
        for offset in (-4, 2, 8):
            with pytest.raises(ApplyError, match="invalid BAR0 dword offset"):
                bar0.read32(offset)
        with pytest.raises(ApplyError, match="does not fit in 32 bits"):
            bar0.write32(0, 0x1_0000_0000)

    assert map_calls == [(91, 8, system.mmap.ACCESS_WRITE)]
    assert closed == [91]
    assert mapping.closed
    with pytest.raises(ApplyError, match="BAR0 is not open"):
        bar0.read32(0)


def test_bar0_translates_open_failure(
    profile_580_105, local_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(system.os, "O_SYNC", 0, raising=False)

    def fail_open(*_args: object) -> int:
        raise OSError("permission denied")

    monkeypatch.setattr(system.os, "open", fail_open)
    bar0 = Bar0(replace(_device(profile_580_105), resource0_size=4), local_tmp_path)

    with pytest.raises(ApplyError, match="cannot open BAR0.*permission denied"):
        bar0.__enter__()

    assert bar0._fd is None
    assert bar0._mapping is None


@pytest.mark.parametrize("error", [OSError("mapping refused"), ValueError("invalid length")])
def test_bar0_closes_fd_when_mmap_fails(
    error: Exception,
    profile_580_105, local_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed: list[int] = []
    monkeypatch.setattr(system.os, "O_SYNC", 0, raising=False)
    monkeypatch.setattr(system.os, "open", lambda *_args: 91)
    monkeypatch.setattr(system.os, "close", closed.append)

    def fail_mapping(*_args: object, **_kwargs: object):
        raise error

    monkeypatch.setattr(system.mmap, "mmap", fail_mapping)
    bar0 = Bar0(replace(_device(profile_580_105), resource0_size=4), local_tmp_path)

    with pytest.raises(ApplyError, match="cannot open BAR0"):
        bar0.__enter__()

    assert closed == [91]
    assert bar0._fd is None
    assert bar0._mapping is None


def _write_recovery_journal(
    firmware: Path, backup: Path, digest: str, *, boot_may_have_run: bool = False
) -> Path:
    journal = system._journal_path(firmware.resolve())
    journal.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "backup": str(backup.resolve()),
                "firmware": str(firmware.resolve()),
                "patched_boot_may_have_run": boot_may_have_run,
                "stock_sha256": digest,
            }
        ),
        encoding="utf-8",
    )
    return journal


@contextlib.contextmanager
def _unlocked_recovery() -> Iterator[None]:
    yield


def test_recover_firmware_rejects_backup_hash_mismatch(
    local_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    firmware = local_tmp_path / "gsp_tu10x.bin"
    digest = "0" * 64
    backup = firmware.with_name(f"{firmware.name}.cmpunlock.stock-{digest[:16]}")
    firmware.write_bytes(b"patched")
    backup.write_bytes(b"authentic stock")
    journal = _write_recovery_journal(firmware, backup, digest)
    monkeypatch.setattr(system, "require_linux", lambda: None)
    monkeypatch.setattr(system, "_exclusive_lock", _unlocked_recovery)
    monkeypatch.setattr(system, "_bundled_profile_for_digest", lambda _digest: object())

    with pytest.raises(ApplyError, match="backup SHA-256 mismatch"):
        system.recover_firmware(firmware)

    assert firmware.read_bytes() == b"patched"
    assert journal.exists()


def test_recover_firmware_restores_verified_backup(
    local_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    firmware = local_tmp_path / "gsp_tu10x.bin"
    stock = b"authentic stock"
    digest = system.sha256_bytes(stock)
    backup = firmware.with_name(f"{firmware.name}.cmpunlock.stock-{digest[:16]}")
    firmware.write_bytes(b"patched")
    backup.write_bytes(stock)
    journal = _write_recovery_journal(firmware, backup, digest)
    monkeypatch.setattr(system, "require_linux", lambda: None)
    monkeypatch.setattr(system, "_exclusive_lock", _unlocked_recovery)
    monkeypatch.setattr(system, "_bundled_profile_for_digest", lambda _digest: object())
    monkeypatch.setattr(system, "validate_stock_firmware", lambda *_args: None)
    monkeypatch.setattr(
        system,
        "atomic_replace_bytes",
        lambda path, data, **_kwargs: Path(path).write_bytes(data),
    )

    result = system.recover_firmware(firmware)

    assert firmware.read_bytes() == stock
    assert result["sha256"] == system.sha256_bytes(stock)
    assert result["backup"] == str(backup)
    assert not journal.exists()


def test_recover_firmware_materializes_malformed_state_as_cold(
    local_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    firmware = local_tmp_path / "gsp_tu10x.bin"
    stock = b"authentic stock"
    digest = system.sha256_bytes(stock)
    backup = firmware.with_name(f"{firmware.name}.cmpunlock.stock-{digest[:16]}")
    firmware.write_bytes(b"patched")
    backup.write_bytes(stock)
    journal = _write_recovery_journal(firmware, backup, digest)
    state_path = system._state_path(firmware.resolve())
    state_path.write_text("[]\n", encoding="utf-8")
    monkeypatch.setattr(system, "require_linux", lambda: None)
    monkeypatch.setattr(system, "_exclusive_lock", _unlocked_recovery)
    monkeypatch.setattr(system, "_bundled_profile_for_digest", lambda _digest: object())
    monkeypatch.setattr(system, "validate_stock_firmware", lambda *_args: None)

    result = system.recover_firmware(firmware)

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert result["cold_power_cycle_required"] is True
    assert "not a JSON object" in result["state_warning"]
    assert state["cold_power_cycle_required"] is True
    assert state["firmware_restored"] is True
    assert "not a JSON object" in state["recovery_warning"]
    assert not journal.exists()


def test_recover_firmware_retains_journal_if_cold_state_write_fails(
    local_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    firmware = local_tmp_path / "gsp_tu10x.bin"
    stock = b"authentic stock"
    digest = system.sha256_bytes(stock)
    backup = firmware.with_name(f"{firmware.name}.cmpunlock.stock-{digest[:16]}")
    firmware.write_bytes(b"patched")
    backup.write_bytes(stock)
    journal = _write_recovery_journal(
        firmware,
        backup,
        digest,
        boot_may_have_run=True,
    )
    state_path = system._state_path(firmware.resolve())

    def fail_state_write(path: Path, data: bytes, **_kwargs: object) -> None:
        path = Path(path)
        if path == state_path:
            raise ApplyError("simulated state write failure")
        path.write_bytes(data)

    monkeypatch.setattr(system, "require_linux", lambda: None)
    monkeypatch.setattr(system, "_exclusive_lock", _unlocked_recovery)
    monkeypatch.setattr(system, "_bundled_profile_for_digest", lambda _digest: object())
    monkeypatch.setattr(system, "validate_stock_firmware", lambda *_args: None)
    monkeypatch.setattr(system, "atomic_replace_bytes", fail_state_write)

    with pytest.raises(ApplyError, match="simulated state write failure"):
        system.recover_firmware(firmware)

    assert firmware.read_bytes() == stock
    assert journal.exists()
    assert not state_path.exists()


def test_recover_firmware_lock_contention_preserves_firmware_and_journal(
    local_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    firmware = local_tmp_path / "gsp_tu10x.bin"
    patched = b"patched"
    stock = b"authentic stock"
    digest = system.sha256_bytes(stock)
    backup = firmware.with_name(f"{firmware.name}.cmpunlock.stock-{digest[:16]}")
    firmware.write_bytes(patched)
    backup.write_bytes(stock)
    journal = _write_recovery_journal(firmware, backup, digest)
    writes: list[bytes] = []

    @contextlib.contextmanager
    def contended() -> Iterator[None]:
        raise ApplyError("another CMP unlock transaction holds the global lock")
        yield

    monkeypatch.setattr(system, "require_linux", lambda: None)
    monkeypatch.setattr(system, "_exclusive_lock", contended)
    monkeypatch.setattr(
        system,
        "atomic_replace_bytes",
        lambda _path, data, **_kwargs: writes.append(data),
    )

    with pytest.raises(ApplyError, match="another CMP unlock transaction"):
        system.recover_firmware(firmware)

    assert writes == []
    assert firmware.read_bytes() == patched
    assert journal.exists()


def test_recover_firmware_rejects_unknown_stock_digest(
    local_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    firmware = local_tmp_path / "gsp_tu10x.bin"
    digest = "a" * 64
    backup = firmware.with_name(f"{firmware.name}.cmpunlock.stock-{digest[:16]}")
    firmware.write_bytes(b"patched")
    backup.write_bytes(b"attacker-selected bytes")
    journal = _write_recovery_journal(firmware, backup, digest)
    writes: list[bytes] = []
    monkeypatch.setattr(system, "require_linux", lambda: None)
    monkeypatch.setattr(system, "_exclusive_lock", _unlocked_recovery)
    monkeypatch.setattr(
        system,
        "atomic_replace_bytes",
        lambda _path, data, **_kwargs: writes.append(data),
    )

    with pytest.raises(ApplyError, match="does not identify one bundled profile"):
        system.recover_firmware(firmware)

    assert writes == []
    assert firmware.read_bytes() == b"patched"
    assert journal.exists()


def test_recover_firmware_rejects_journal_for_different_firmware(
    local_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    firmware = local_tmp_path / "gsp_tu10x.bin"
    other = local_tmp_path / "other.bin"
    digest = "b" * 64
    backup = firmware.with_name(f"{firmware.name}.cmpunlock.stock-{digest[:16]}")
    firmware.write_bytes(b"patched")
    backup.write_bytes(b"stock")
    journal = _write_recovery_journal(firmware, backup, digest)
    document = json.loads(journal.read_text(encoding="utf-8"))
    document["firmware"] = str(other.resolve())
    journal.write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setattr(system, "require_linux", lambda: None)
    monkeypatch.setattr(system, "_exclusive_lock", _unlocked_recovery)

    with pytest.raises(ApplyError, match="does not match the requested path"):
        system.recover_firmware(firmware)

    assert firmware.read_bytes() == b"patched"
    assert journal.exists()


def test_clear_state_requires_cold_cycle_ack_and_verified_stock(
    local_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    firmware = local_tmp_path / "gsp_tu10x.bin"
    firmware.write_bytes(b"stock")
    digest = "c" * 64
    state_path = system._state_path(firmware.resolve())
    state_path.write_text(
        json.dumps(
            {
                "firmware": str(firmware.resolve()),
                "stage": "complete",
                "stock_sha256": digest,
            }
        ),
        encoding="utf-8",
    )
    validated: list[bytes] = []
    monkeypatch.setattr(system, "require_linux", lambda: None)
    monkeypatch.setattr(system.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(system, "_exclusive_lock", _unlocked_recovery)
    monkeypatch.setattr(system, "_bundled_profile_for_digest", lambda _digest: object())
    monkeypatch.setattr(
        system,
        "validate_stock_firmware",
        lambda data, _profile: validated.append(data),
    )

    with pytest.raises(ApplyError, match="COLD-POWER-CYCLE-COMPLETED"):
        system.clear_state(firmware, acknowledgement="")
    assert state_path.exists()

    result = system.clear_state(
        firmware,
        acknowledgement=system.COLD_CYCLE_ACKNOWLEDGEMENT,
    )

    assert result["prior_stage"] == "complete"
    assert validated == [b"stock"]
    assert not state_path.exists()


class _FakeBar0:
    def __init__(
        self,
        registers: dict[int, int],
        writes: list[tuple[int, int]],
        events: list[tuple[object, ...]] | None = None,
    ) -> None:
        self.registers = registers
        self.writes = writes
        self.events = events

    def __enter__(self) -> "_FakeBar0":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read32(self, address: int) -> int:
        if self.events is not None:
            self.events.append(("bar0-read", address))
        return self.registers[address]

    def write32(self, address: int, value: int) -> None:
        if self.events is not None:
            self.events.append(("bar0-write", address, value))
        self.writes.append((address, value))
        self.registers[address] = value


def _mock_apply_environment(
    profile,
    local_tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    inspect_drivers: tuple[str | None, ...],
):
    firmware = local_tmp_path / "gsp_tu10x.bin"
    stock = b"stock firmware bytes"
    patched = b"patched firmware bytes"
    firmware.write_bytes(stock)
    device = _device(profile)
    reset = local_tmp_path / "reset-sentinel"
    reset.write_text("", encoding="ascii")
    drivers = iter(inspect_drivers)
    last_driver = inspect_drivers[-1]
    registers = {profile.plm_readback_address: 0}
    old_values: dict[int, int] = {}
    for index, write in enumerate(profile.host_writes, start=1):
        old_values[write.address] = index * 0x11111111
        registers[write.address] = old_values[write.address]
    for index, write in enumerate(system._diagnostic_hs_writes(profile), start=1):
        registers[write.address] = 0x20000000 + index
    register_writes: list[tuple[int, int]] = []
    commands: list[tuple[str, ...]] = []
    firmware_writes: list[bytes] = []
    reset_calls: list[tuple[str, Path]] = []
    events: list[tuple[object, ...]] = []

    @contextlib.contextmanager
    def unlocked() -> Iterator[None]:
        yield

    def inspect_device(*_args: object, **_kwargs: object) -> PciDevice:
        nonlocal last_driver
        try:
            last_driver = next(drivers)
        except StopIteration:
            pass
        return replace(device, driver=last_driver)

    def fake_atomic(path: Path, data: bytes, **_kwargs: object) -> None:
        path = Path(path)
        path.write_bytes(data)
        if path == firmware.resolve():
            firmware_writes.append(data)
            events.append(("firmware", "patched" if data == patched else "stock"))

    def run(command: list[str], **_kwargs: object):
        commands.append(tuple(command))
        events.append(("command", tuple(command)))
        if command == ["modprobe", "nvidia"]:
            registers[profile.plm_readback_address] = profile.plm_open_value
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    def reset_device(bdf: str, root: Path) -> None:
        reset_calls.append((bdf, root))
        events.append(("flr", len(reset_calls)))
        reset.write_text("1\n", encoding="ascii")

    monkeypatch.setattr(system, "require_linux", lambda: None)
    monkeypatch.setattr(system.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(system, "inspect_pci_device", inspect_device)
    monkeypatch.setattr(system, "enumerate_nvidia_devices", lambda *_args: (device.bdf,))
    monkeypatch.setattr(system, "loaded_nvidia_modules", lambda: ())
    monkeypatch.setattr(system, "_require_reset_available", lambda *_args: None)
    monkeypatch.setattr(
        system,
        "_reset_device",
        reset_device,
    )
    monkeypatch.setattr(system, "validate_module", lambda _profile: {})
    monkeypatch.setattr(system, "validate_stock_firmware", lambda *_args: None)
    monkeypatch.setattr(
        system,
        "build_compute_payload",
        lambda _profile: (b"payload", SimpleNamespace()),
    )
    monkeypatch.setattr(
        system,
        "patch_firmware",
        lambda *_args: (patched, SimpleNamespace(patched_sha256="f" * 64)),
    )
    monkeypatch.setattr(system, "_exclusive_lock", unlocked)
    monkeypatch.setattr(system, "atomic_replace_bytes", fake_atomic)
    monkeypatch.setattr(
        system,
        "Bar0",
        lambda *_args, **_kwargs: _FakeBar0(registers, register_writes, events),
    )
    monkeypatch.setattr(system, "_run", run)
    monkeypatch.setattr(system.shutil, "which", lambda command: f"/usr/bin/{command}")
    monkeypatch.setattr(system.signal, "getsignal", lambda _signal: "old-handler")
    monkeypatch.setattr(system.signal, "signal", lambda *_args: None)
    return SimpleNamespace(
        commands=commands,
        device=device,
        events=events,
        firmware=firmware,
        firmware_writes=firmware_writes,
        old_values=old_values,
        patched=patched,
        register_writes=register_writes,
        registers=registers,
        reset=reset,
        reset_calls=reset_calls,
        stock=stock,
    )


def test_recovery_persists_cold_cycle_interlock_before_removing_journal(
    profile_580_105, local_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _mock_apply_environment(
        profile_580_105,
        local_tmp_path,
        monkeypatch,
        inspect_drivers=(None,),
    )
    digest = system.sha256_bytes(harness.stock)
    backup = harness.firmware.with_name(
        f"{harness.firmware.name}.cmpunlock.stock-{digest[:16]}"
    )
    backup.write_bytes(harness.stock)
    harness.firmware.write_bytes(harness.patched)
    journal = _write_recovery_journal(
        harness.firmware,
        backup,
        digest,
        boot_may_have_run=True,
    )
    monkeypatch.setattr(system, "_bundled_profile_for_digest", lambda _digest: object())

    result = system.recover_firmware(harness.firmware)

    state_path = system._state_path(harness.firmware.resolve())
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert result["cold_power_cycle_required"] is True
    assert state["cold_power_cycle_required"] is True
    assert state["firmware_restored"] is True
    assert state["stage"] == "recovered-stock-cold-cycle-required"
    assert not journal.exists()

    with pytest.raises(ApplyError, match="unresolved hardware state"):
        system.experimental_apply(
            harness.device.bdf,
            harness.firmware,
            profile_580_105,
            acknowledgement=system.ACKNOWLEDGEMENT,
            settle_seconds=0,
            sysfs_root=local_tmp_path,
        )

    assert harness.commands == []


def test_experimental_apply_missing_flr_fails_before_firmware_write(
    profile_580_105, local_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    firmware = local_tmp_path / "gsp_tu10x.bin"
    stock = b"stock"
    firmware.write_bytes(stock)
    device = _device(profile_580_105)
    writes: list[tuple[Path, bytes]] = []
    monkeypatch.setattr(system, "require_linux", lambda: None)
    monkeypatch.setattr(system.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(system, "inspect_pci_device", lambda *_args, **_kwargs: device)
    monkeypatch.setattr(system, "enumerate_nvidia_devices", lambda *_args: (device.bdf,))
    monkeypatch.setattr(
        system,
        "atomic_replace_bytes",
        lambda path, data, **_kwargs: writes.append((Path(path), data)),
    )

    with pytest.raises(ApplyError, match="PCI function reset is unavailable"):
        system.experimental_apply(
            device.bdf,
            firmware,
            profile_580_105,
            acknowledgement=system.ACKNOWLEDGEMENT,
            settle_seconds=0,
            sysfs_root=local_tmp_path,
        )

    assert writes == []
    assert firmware.read_bytes() == stock
    assert not system._journal_path(firmware.resolve()).exists()


def test_experimental_apply_always_rejects_multi_gpu(
    profile_580_105, local_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    firmware = local_tmp_path / "gsp_tu10x.bin"
    firmware.write_bytes(b"stock")
    device = _device(profile_580_105)
    writes: list[bytes] = []
    monkeypatch.setattr(system, "require_linux", lambda: None)
    monkeypatch.setattr(system.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(system, "inspect_pci_device", lambda *_args, **_kwargs: device)
    monkeypatch.setattr(
        system,
        "enumerate_nvidia_devices",
        lambda *_args: (device.bdf, "0000:02:00.0"),
    )
    monkeypatch.setattr(
        system,
        "atomic_replace_bytes",
        lambda _path, data, **_kwargs: writes.append(data),
    )

    with pytest.raises(ApplyError, match="only NVIDIA PCI function"):
        system.experimental_apply(
            device.bdf,
            firmware,
            profile_580_105,
            acknowledgement=system.ACKNOWLEDGEMENT,
            settle_seconds=0,
            sysfs_root=local_tmp_path,
        )

    assert "allow_multi_gpu" not in inspect.signature(system.experimental_apply).parameters
    assert writes == []


def test_experimental_apply_success_restores_firmware_and_completes_state(
    profile_580_105, local_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _mock_apply_environment(
        profile_580_105,
        local_tmp_path,
        monkeypatch,
        inspect_drivers=(None, "nvidia", "nvidia"),
    )

    result = system.experimental_apply(
        harness.device.bdf,
        harness.firmware,
        profile_580_105,
        acknowledgement=system.ACKNOWLEDGEMENT,
        settle_seconds=0,
        sysfs_root=local_tmp_path,
    )

    state_path = system._state_path(harness.firmware.resolve())
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert result["status"] == "register-readback-passed"
    assert result["state"] == str(state_path)
    assert harness.firmware_writes == [harness.patched, harness.stock]
    assert harness.firmware.read_bytes() == harness.stock
    assert harness.reset.read_text(encoding="ascii") == "1\n"
    assert harness.commands == [
        ("modprobe", "nvidia"),
        tuple(system._nvidia_smi_command(harness.device.bdf, _NVIDIA_SMI)),
        ("modprobe", "-r", "nvidia"),
        ("modprobe", "nvidia"),
        tuple(system._nvidia_smi_command(harness.device.bdf, _NVIDIA_SMI)),
    ]
    assert harness.reset_calls == [(harness.device.bdf, local_tmp_path)]
    assert state["stage"] == "complete"
    assert state["firmware_restored"] is True
    assert state["override_state_may_be_active"] is True
    assert state["patched_firmware_boot_attempted"] is True
    assert state["patched_gpu_initialization_probe"]["returncode"] == 0
    assert state["stock_gpu_verification"]["returncode"] == 0
    assert state["stock_module_may_be_loaded"] is True
    assert state["error"] is None
    assert not system._journal_path(harness.firmware.resolve()).exists()


def test_reported_apply_uses_two_flrs_and_never_host_writes_memory_registers(
    profile_580_173, local_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _mock_apply_environment(
        profile_580_173,
        local_tmp_path,
        monkeypatch,
        inspect_drivers=(None, None, "nvidia"),
    )

    result = system.experimental_apply(
        harness.device.bdf,
        harness.firmware,
        profile_580_173,
        acknowledgement=system.REPORTED_PATH_ACKNOWLEDGEMENT,
        settle_seconds=0,
        sysfs_root=local_tmp_path,
    )

    state = json.loads(
        system._state_path(harness.firmware.resolve()).read_text(encoding="utf-8")
    )
    probe = tuple(system._nvidia_smi_command(harness.device.bdf, _NVIDIA_SMI))
    assert harness.commands == [
        ("modprobe", "nvidia"),
        probe,
        ("modprobe", "-r", "nvidia"),
        ("modprobe", "nvidia"),
        probe,
    ]
    assert harness.reset_calls == [
        (harness.device.bdf, local_tmp_path),
        (harness.device.bdf, local_tmp_path),
    ]
    assert harness.register_writes == [
        (write.address, write.value) for write in profile_580_173.host_writes
    ]
    diagnostic_addresses = {
        write.address for write in system._diagnostic_hs_writes(profile_580_173)
    }
    assert not diagnostic_addresses & {
        address for address, _value in harness.register_writes
    }
    assert result["execution_strategy"] == "reported-two-phase"
    assert result["memory_capacity_verified"] is False
    assert result["patched_gpu_initialization_probe"]["returncode"] == 0
    assert result["stock_gpu_verification"]["returncode"] == 0
    assert state["execution_strategy"] == "reported-two-phase"
    assert state["memory_capacity_verified"] is False
    assert state["stage"] == "complete"
    assert "FBPA_CFG1_UNVERIFIED" in result["baseline_registers"]
    assert "LMR_UNVERIFIED" in result["baseline_registers"]

    loads = [
        index
        for index, event in enumerate(harness.events)
        if event == ("command", ("modprobe", "nvidia"))
    ]
    probes = [
        index
        for index, event in enumerate(harness.events)
        if event == ("command", probe)
    ]
    flrs = [
        index
        for index, event in enumerate(harness.events)
        if event[0] == "flr"
    ]
    writes = [
        index
        for index, event in enumerate(harness.events)
        if event[0] == "bar0-write"
    ]
    patched_install = harness.events.index(("firmware", "patched"))
    clean_unload = harness.events.index(
        ("command", ("modprobe", "-r", "nvidia"))
    )
    stock_restore = harness.events.index(("firmware", "stock"))
    post_flr_plm_gate = next(
        index
        for index, event in enumerate(harness.events)
        if index > flrs[1]
        and event == ("bar0-read", profile_580_173.plm_readback_address)
    )
    assert len(loads) == len(probes) == len(flrs) == len(writes) == 2
    assert (
        patched_install
        < loads[0]
        < probes[0]
        < flrs[0]
        < clean_unload
        < stock_restore
        < flrs[1]
        < post_flr_plm_gate
        < writes[0]
        < writes[1]
        < loads[1]
        < probes[1]
    )


def test_reported_apply_requires_memory_side_effect_acknowledgement(
    profile_580_173, local_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _mock_apply_environment(
        profile_580_173,
        local_tmp_path,
        monkeypatch,
        inspect_drivers=(None,),
    )

    with pytest.raises(ApplyError, match=system.REPORTED_PATH_ACKNOWLEDGEMENT):
        system.experimental_apply(
            harness.device.bdf,
            harness.firmware,
            profile_580_173,
            acknowledgement=system.ACKNOWLEDGEMENT,
            settle_seconds=0,
            sysfs_root=local_tmp_path,
        )

    assert harness.commands == []
    assert harness.firmware_writes == []
    assert harness.reset_calls == []


def test_reported_apply_requires_nvidia_smi_before_firmware_mutation(
    profile_580_173, local_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _mock_apply_environment(
        profile_580_173,
        local_tmp_path,
        monkeypatch,
        inspect_drivers=(None,),
    )
    monkeypatch.setattr(system.shutil, "which", lambda _command: None)

    with pytest.raises(SystemCheckError, match="nvidia-smi is required"):
        system.experimental_apply(
            harness.device.bdf,
            harness.firmware,
            profile_580_173,
            acknowledgement=system.REPORTED_PATH_ACKNOWLEDGEMENT,
            settle_seconds=0,
            sysfs_root=local_tmp_path,
        )

    assert harness.commands == []
    assert harness.firmware_writes == []
    assert harness.register_writes == []
    assert harness.reset_calls == []


def test_reported_apply_records_nonzero_patched_probe_and_continues_to_plm_gate(
    profile_580_173, local_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _mock_apply_environment(
        profile_580_173,
        local_tmp_path,
        monkeypatch,
        inspect_drivers=(None, None, "nvidia"),
    )
    probe_calls = 0

    def run(command: list[str], **_kwargs: object):
        nonlocal probe_calls
        harness.commands.append(tuple(command))
        if command == ["modprobe", "nvidia"]:
            harness.registers[
                profile_580_173.plm_readback_address
            ] = profile_580_173.plm_open_value
        if command and command[0] == _NVIDIA_SMI:
            probe_calls += 1
            return SimpleNamespace(
                stdout="",
                stderr="expected patched initialization failure" if probe_calls == 1 else "",
                returncode=9 if probe_calls == 1 else 0,
            )
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(system, "_run", run)

    result = system.experimental_apply(
        harness.device.bdf,
        harness.firmware,
        profile_580_173,
        acknowledgement=system.REPORTED_PATH_ACKNOWLEDGEMENT,
        settle_seconds=0,
        sysfs_root=local_tmp_path,
    )

    assert probe_calls == 2
    assert result["patched_gpu_initialization_probe"]["returncode"] == 9
    assert result["patched_gpu_initialization_probe"]["timed_out"] is False
    assert result["stock_gpu_verification"]["returncode"] == 0


def test_reported_apply_records_patched_probe_timeout_and_continues_to_plm_gate(
    profile_580_173, local_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _mock_apply_environment(
        profile_580_173,
        local_tmp_path,
        monkeypatch,
        inspect_drivers=(None, None, "nvidia"),
    )
    probe_calls = 0

    def run(command: list[str], **_kwargs: object):
        nonlocal probe_calls
        harness.commands.append(tuple(command))
        if command == ["modprobe", "nvidia"]:
            harness.registers[
                profile_580_173.plm_readback_address
            ] = profile_580_173.plm_open_value
        if command and command[0] == _NVIDIA_SMI:
            probe_calls += 1
            if probe_calls == 1:
                raise ApplyError("simulated patched probe timeout")
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(system, "_run", run)

    result = system.experimental_apply(
        harness.device.bdf,
        harness.firmware,
        profile_580_173,
        acknowledgement=system.REPORTED_PATH_ACKNOWLEDGEMENT,
        settle_seconds=0,
        sysfs_root=local_tmp_path,
    )

    assert probe_calls == 2
    assert result["patched_gpu_initialization_probe"]["returncode"] is None
    assert result["patched_gpu_initialization_probe"]["timed_out"] is True
    assert "simulated patched probe timeout" in result[
        "patched_gpu_initialization_probe"
    ]["detail"]


def test_reported_apply_closed_plm_after_flrs_prevents_all_host_writes(
    profile_580_173, local_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _mock_apply_environment(
        profile_580_173,
        local_tmp_path,
        monkeypatch,
        inspect_drivers=(None, None),
    )

    def reset_device(bdf: str, root: Path) -> None:
        harness.reset_calls.append((bdf, root))
        harness.reset.write_text("1\n", encoding="ascii")
        if len(harness.reset_calls) == 2:
            harness.registers[profile_580_173.plm_readback_address] = 0

    monkeypatch.setattr(system, "_reset_device", reset_device)

    with pytest.raises(ApplyError, match="FEAT_OVR_PLM read 0x00000000"):
        system.experimental_apply(
            harness.device.bdf,
            harness.firmware,
            profile_580_173,
            acknowledgement=system.REPORTED_PATH_ACKNOWLEDGEMENT,
            settle_seconds=0,
            sysfs_root=local_tmp_path,
        )

    assert len(harness.reset_calls) == 2
    assert harness.register_writes == []
    assert harness.firmware_writes == [harness.patched, harness.stock]
    assert harness.firmware.read_bytes() == harness.stock
    state = json.loads(
        system._state_path(harness.firmware.resolve()).read_text(encoding="utf-8")
    )
    assert state["host_compute_overrides_may_be_active"] is False
    assert state["reported_hs_side_effects_may_be_active"] is True
    assert state["override_state_may_be_active"] is True


def test_reported_apply_flr1_failure_restores_stock_without_host_writes(
    profile_580_173, local_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _mock_apply_environment(
        profile_580_173,
        local_tmp_path,
        monkeypatch,
        inspect_drivers=(None,),
    )

    def fail_flr1(bdf: str, root: Path) -> None:
        harness.reset_calls.append((bdf, root))
        raise ApplyError("simulated FLR1 failure")

    monkeypatch.setattr(system, "_reset_device", fail_flr1)

    with pytest.raises(ApplyError, match="simulated FLR1 failure"):
        system.experimental_apply(
            harness.device.bdf,
            harness.firmware,
            profile_580_173,
            acknowledgement=system.REPORTED_PATH_ACKNOWLEDGEMENT,
            settle_seconds=0,
            sysfs_root=local_tmp_path,
        )

    assert len(harness.reset_calls) == 1
    assert harness.register_writes == []
    assert harness.firmware_writes == [harness.patched, harness.stock]
    assert harness.firmware.read_bytes() == harness.stock
    assert ("modprobe", "-r", "nvidia") in harness.commands
    assert all(command[0] not in {"rmmod", "kill", "pkill"} for command in harness.commands)


def test_reported_apply_unload_failure_retries_cleanly_and_restores_stock(
    profile_580_173, local_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _mock_apply_environment(
        profile_580_173,
        local_tmp_path,
        monkeypatch,
        inspect_drivers=(None,),
    )
    unload_attempts = 0

    def run(command: list[str], **_kwargs: object):
        nonlocal unload_attempts
        harness.commands.append(tuple(command))
        if command == ["modprobe", "nvidia"]:
            harness.registers[
                profile_580_173.plm_readback_address
            ] = profile_580_173.plm_open_value
        if command == ["modprobe", "-r", "nvidia"]:
            unload_attempts += 1
            if unload_attempts == 1:
                raise ApplyError("simulated reported unload failure")
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(system, "_run", run)

    with pytest.raises(ApplyError, match="simulated reported unload failure"):
        system.experimental_apply(
            harness.device.bdf,
            harness.firmware,
            profile_580_173,
            acknowledgement=system.REPORTED_PATH_ACKNOWLEDGEMENT,
            settle_seconds=0,
            sysfs_root=local_tmp_path,
        )

    assert unload_attempts == 2
    assert len(harness.reset_calls) == 1
    assert harness.register_writes == []
    assert harness.firmware_writes == [harness.patched, harness.stock]
    assert harness.firmware.read_bytes() == harness.stock
    assert all(command[0] not in {"rmmod", "kill", "pkill"} for command in harness.commands)


def test_reported_apply_restores_stock_before_flr2_and_handles_flr2_failure(
    profile_580_173, local_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _mock_apply_environment(
        profile_580_173,
        local_tmp_path,
        monkeypatch,
        inspect_drivers=(None,),
    )

    def fail_flr2(bdf: str, root: Path) -> None:
        harness.reset_calls.append((bdf, root))
        if len(harness.reset_calls) == 2:
            assert harness.firmware.read_bytes() == harness.stock
            raise ApplyError("simulated FLR2 failure")

    monkeypatch.setattr(system, "_reset_device", fail_flr2)

    with pytest.raises(ApplyError, match="simulated FLR2 failure"):
        system.experimental_apply(
            harness.device.bdf,
            harness.firmware,
            profile_580_173,
            acknowledgement=system.REPORTED_PATH_ACKNOWLEDGEMENT,
            settle_seconds=0,
            sysfs_root=local_tmp_path,
        )

    assert len(harness.reset_calls) == 2
    assert harness.register_writes == []
    assert harness.firmware_writes == [harness.patched, harness.stock]
    assert harness.firmware.read_bytes() == harness.stock


def test_reported_apply_requires_successful_final_stock_probe(
    profile_580_173, local_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _mock_apply_environment(
        profile_580_173,
        local_tmp_path,
        monkeypatch,
        inspect_drivers=(None, None),
    )
    probe_calls = 0

    def run(command: list[str], **_kwargs: object):
        nonlocal probe_calls
        harness.commands.append(tuple(command))
        if command == ["modprobe", "nvidia"]:
            harness.registers[
                profile_580_173.plm_readback_address
            ] = profile_580_173.plm_open_value
        if command and command[0] == _NVIDIA_SMI:
            probe_calls += 1
            if probe_calls == 2:
                raise ApplyError("simulated stock nvidia-smi failure")
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(system, "_run", run)

    with pytest.raises(ApplyError, match="cold power cycle required") as raised:
        system.experimental_apply(
            harness.device.bdf,
            harness.firmware,
            profile_580_173,
            acknowledgement=system.REPORTED_PATH_ACKNOWLEDGEMENT,
            settle_seconds=0,
            sysfs_root=local_tmp_path,
        )

    state = json.loads(
        system._state_path(harness.firmware.resolve()).read_text(encoding="utf-8")
    )
    assert "simulated stock nvidia-smi failure" in str(raised.value)
    assert state["stage"] == "stock-module-load-returned"
    assert "simulated stock nvidia-smi failure" in state["error"]
    assert state["stock_gpu_verification"] is None
    assert state["stock_module_may_be_loaded"] is True
    assert harness.firmware.read_bytes() == harness.stock
    assert all(command[0] not in {"rmmod", "kill", "pkill"} for command in harness.commands)


@pytest.mark.parametrize(
    ("record_kind", "message"),
    [
        ("journal", "unresolved recovery journal"),
        ("state", "unresolved hardware state"),
    ],
)
def test_experimental_apply_refuses_to_overwrite_unresolved_records(
    profile_580_105,
    local_tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    record_kind: str,
    message: str,
) -> None:
    harness = _mock_apply_environment(
        profile_580_105,
        local_tmp_path,
        monkeypatch,
        inspect_drivers=(None,),
    )
    record_path = (
        system._journal_path(harness.firmware.resolve())
        if record_kind == "journal"
        else system._state_path(harness.firmware.resolve())
    )
    original = '{"preserve": true}\n'
    record_path.write_text(original, encoding="utf-8")

    with pytest.raises(ApplyError, match=message):
        system.experimental_apply(
            harness.device.bdf,
            harness.firmware,
            profile_580_105,
            acknowledgement=system.ACKNOWLEDGEMENT,
            settle_seconds=0,
            sysfs_root=local_tmp_path,
        )

    assert record_path.read_text(encoding="utf-8") == original
    assert harness.commands == []
    assert harness.firmware_writes == []
    assert harness.register_writes == []


def test_experimental_apply_persists_boot_audit_before_modprobe(
    profile_580_105, local_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _mock_apply_environment(
        profile_580_105,
        local_tmp_path,
        monkeypatch,
        inspect_drivers=(None, "nvidia", "nvidia"),
    )
    observed_preboot_audit = False

    def run(command: list[str], **_kwargs: object):
        nonlocal observed_preboot_audit
        harness.commands.append(tuple(command))
        if command == ["modprobe", "nvidia"] and not observed_preboot_audit:
            journal = json.loads(
                system._journal_path(harness.firmware.resolve()).read_text(encoding="utf-8")
            )
            state = json.loads(
                system._state_path(harness.firmware.resolve()).read_text(encoding="utf-8")
            )
            assert journal["stage"] == "patched-boot-attempting"
            assert journal["patched_boot_may_have_run"] is True
            assert state["stage"] == "patched-boot-attempting"
            assert state["patched_firmware_boot_attempted"] is True
            observed_preboot_audit = True
        if command == ["modprobe", "nvidia"]:
            harness.registers[
                profile_580_105.plm_readback_address
            ] = profile_580_105.plm_open_value
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(system, "_run", run)

    system.experimental_apply(
        harness.device.bdf,
        harness.firmware,
        profile_580_105,
        acknowledgement=system.ACKNOWLEDGEMENT,
        settle_seconds=0,
        sysfs_root=local_tmp_path,
    )

    assert observed_preboot_audit is True


def test_experimental_apply_refuses_preopened_plm_before_firmware_write(
    profile_580_105, local_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _mock_apply_environment(
        profile_580_105,
        local_tmp_path,
        monkeypatch,
        inspect_drivers=(None,),
    )
    harness.registers[
        profile_580_105.plm_readback_address
    ] = profile_580_105.plm_open_value

    with pytest.raises(ApplyError, match="already open before the patched boot"):
        system.experimental_apply(
            harness.device.bdf,
            harness.firmware,
            profile_580_105,
            acknowledgement=system.ACKNOWLEDGEMENT,
            settle_seconds=0,
            sysfs_root=local_tmp_path,
        )

    assert harness.commands == []
    assert harness.firmware_writes == []
    assert harness.register_writes == []


def test_experimental_apply_never_modprobes_when_preboot_audit_write_fails(
    profile_580_105, local_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _mock_apply_environment(
        profile_580_105,
        local_tmp_path,
        monkeypatch,
        inspect_drivers=(None,),
    )
    real_write_journal = system._write_journal

    def fail_preboot_audit(path: Path, document: dict[str, object]) -> None:
        if document.get("patched_boot_may_have_run") is True:
            raise ApplyError("simulated audit write failure")
        real_write_journal(path, document)

    monkeypatch.setattr(system, "_write_journal", fail_preboot_audit)

    with pytest.raises(ApplyError, match="simulated audit write failure"):
        system.experimental_apply(
            harness.device.bdf,
            harness.firmware,
            profile_580_105,
            acknowledgement=system.ACKNOWLEDGEMENT,
            settle_seconds=0,
            sysfs_root=local_tmp_path,
        )

    assert harness.commands == []
    assert harness.firmware_writes == [harness.patched, harness.stock]
    assert harness.firmware.read_bytes() == harness.stock


def test_experimental_apply_retains_journal_when_state_write_fails_after_boot_audit(
    profile_580_105, local_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _mock_apply_environment(
        profile_580_105,
        local_tmp_path,
        monkeypatch,
        inspect_drivers=(None,),
    )
    state_path = system._state_path(harness.firmware.resolve())
    real_write_journal = system._write_journal

    def fail_state_record(path: Path, document: dict[str, object]) -> None:
        if path == state_path:
            raise ApplyError("simulated state write failure")
        real_write_journal(path, document)

    monkeypatch.setattr(system, "_write_journal", fail_state_record)

    with pytest.raises(ApplyError, match="cold power cycle required"):
        system.experimental_apply(
            harness.device.bdf,
            harness.firmware,
            profile_580_105,
            acknowledgement=system.ACKNOWLEDGEMENT,
            settle_seconds=0,
            sysfs_root=local_tmp_path,
        )

    journal_path = system._journal_path(harness.firmware.resolve())
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    assert journal["patched_boot_may_have_run"] is True
    assert harness.commands == []
    assert harness.firmware.read_bytes() == harness.stock
    assert not state_path.exists()


def test_experimental_apply_unload_failure_attempts_register_rollback(
    profile_580_105, local_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _mock_apply_environment(
        profile_580_105,
        local_tmp_path,
        monkeypatch,
        inspect_drivers=(None, "nvidia", "nvidia"),
    )
    unload_attempts = 0

    def run(command: list[str], **_kwargs: object):
        nonlocal unload_attempts
        harness.commands.append(tuple(command))
        if command == ["modprobe", "nvidia"]:
            harness.registers[
                profile_580_105.plm_readback_address
            ] = profile_580_105.plm_open_value
        if command == ["modprobe", "-r", "nvidia"]:
            unload_attempts += 1
            if unload_attempts == 1:
                raise ApplyError("simulated unload failure")
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(system, "_run", run)

    with pytest.raises(ApplyError, match="simulated unload failure"):
        system.experimental_apply(
            harness.device.bdf,
            harness.firmware,
            profile_580_105,
            acknowledgement=system.ACKNOWLEDGEMENT,
            settle_seconds=0,
            sysfs_root=local_tmp_path,
        )

    state = json.loads(
        system._state_path(harness.firmware.resolve()).read_text(encoding="utf-8")
    )
    assert unload_attempts == 2
    assert harness.registers | {} == {
        profile_580_105.plm_readback_address: profile_580_105.plm_open_value,
        **harness.old_values,
    }
    for address, old_value in harness.old_values.items():
        assert (address, old_value) in harness.register_writes
    assert harness.firmware_writes == [harness.patched, harness.stock]
    assert harness.firmware.read_bytes() == harness.stock
    assert state["stage"] == "overrides-rolled-back"
    assert state["override_state_may_be_active"] is False
    assert state["firmware_restored"] is True


def test_experimental_apply_final_bind_failure_records_partial_state(
    profile_580_105, local_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _mock_apply_environment(
        profile_580_105,
        local_tmp_path,
        monkeypatch,
        inspect_drivers=(None, "nvidia", None),
    )

    with pytest.raises(ApplyError, match="cold power cycle required") as raised:
        system.experimental_apply(
            harness.device.bdf,
            harness.firmware,
            profile_580_105,
            acknowledgement=system.ACKNOWLEDGEMENT,
            settle_seconds=0,
            sysfs_root=local_tmp_path,
        )

    state_path = system._state_path(harness.firmware.resolve())
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert "did not bind to the stock nvidia driver" in str(raised.value)
    assert harness.firmware.read_bytes() == harness.stock
    assert state["stage"] == "stock-module-load-returned"
    assert state["cold_power_cycle_required"] is True
    assert state["override_state_may_be_active"] is True
    assert state["firmware_restored"] is True
    assert "did not bind to the stock nvidia driver" in state["error"]


def test_exclusive_lock_is_global_and_rejects_concurrent_acquisition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert not inspect.signature(system._exclusive_lock).parameters
    opened: list[tuple[str, str]] = []
    locked = False

    class FakeHandle:
        acquired = False

        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            nonlocal locked
            if self.acquired:
                locked = False

    class FakeFcntl:
        LOCK_EX = 1
        LOCK_NB = 2

        @staticmethod
        def flock(handle: FakeHandle, flags: int) -> None:
            nonlocal locked
            assert flags == FakeFcntl.LOCK_EX | FakeFcntl.LOCK_NB
            if locked:
                raise BlockingIOError
            locked = True
            handle.acquired = True

    def open_lock(path: Path, mode: str):
        opened.append((str(path), mode))
        return FakeHandle()

    monkeypatch.setattr(system, "fcntl", FakeFcntl)
    monkeypatch.setattr(system.Path, "open", open_lock)

    with system._exclusive_lock():
        with pytest.raises(ApplyError, match="another CMP unlock transaction"):
            with system._exclusive_lock():
                pytest.fail("concurrent lock unexpectedly acquired")
    with system._exclusive_lock():
        pass

    assert opened == [
        (str(Path("/run/lock/cmpunlock.lock")), "a+"),
        (str(Path("/run/lock/cmpunlock.lock")), "a+"),
        (str(Path("/run/lock/cmpunlock.lock")), "a+"),
    ]


def test_experimental_apply_restores_stock_when_driver_load_fails(
    profile_580_105, local_tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    firmware = local_tmp_path / "gsp_tu10x.bin"
    stock = b"stock firmware bytes"
    patched = b"patched firmware bytes"
    firmware.write_bytes(stock)
    device = _device(profile_580_105)
    firmware_writes: list[bytes] = []
    reset_checks: list[tuple[str, Path]] = []

    @contextlib.contextmanager
    def unlocked() -> Iterator[None]:
        yield

    def fake_atomic(path: Path, data: bytes, **_kwargs: object) -> None:
        path = Path(path)
        path.write_bytes(data)
        if path == firmware.resolve():
            firmware_writes.append(data)

    monkeypatch.setattr(system, "require_linux", lambda: None)
    monkeypatch.setattr(system.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(system, "inspect_pci_device", lambda *_args, **_kwargs: device)
    monkeypatch.setattr(system, "enumerate_nvidia_devices", lambda *_args: (device.bdf,))
    monkeypatch.setattr(
        system,
        "_require_reset_available",
        lambda bdf, root: reset_checks.append((bdf, root)),
    )
    monkeypatch.setattr(system, "loaded_nvidia_modules", lambda: ())
    monkeypatch.setattr(system, "validate_module", lambda _profile: {})
    monkeypatch.setattr(system.shutil, "which", lambda command: f"/usr/bin/{command}")
    monkeypatch.setattr(system, "validate_stock_firmware", lambda *_args: None)
    monkeypatch.setattr(
        system,
        "build_compute_payload",
        lambda _profile: (b"payload", SimpleNamespace()),
    )
    monkeypatch.setattr(
        system,
        "patch_firmware",
        lambda *_args: (patched, SimpleNamespace(patched_sha256="f" * 64)),
    )
    monkeypatch.setattr(system, "_exclusive_lock", unlocked)
    monkeypatch.setattr(system, "atomic_replace_bytes", fake_atomic)
    baseline_registers = {profile_580_105.plm_readback_address: 0}
    baseline_registers.update(
        {write.address: 0 for write in profile_580_105.host_writes}
    )
    monkeypatch.setattr(
        system,
        "Bar0",
        lambda *_args, **_kwargs: _FakeBar0(baseline_registers, []),
    )
    monkeypatch.setattr(system.signal, "getsignal", lambda _signal: "old-handler")
    monkeypatch.setattr(system.signal, "signal", lambda *_args: None)

    def fail_module_load(command: list[str], **_kwargs: object):
        if command == ["modprobe", "nvidia"]:
            raise ApplyError("simulated module load failure")
        assert command == ["modprobe", "-r", "nvidia"]
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(system, "_run", fail_module_load)

    with pytest.raises(ApplyError, match="cold power cycle required"):
        system.experimental_apply(
            device.bdf,
            firmware,
            profile_580_105,
            acknowledgement=system.ACKNOWLEDGEMENT,
            settle_seconds=0,
            sysfs_root=local_tmp_path,
        )

    backup = firmware.with_name(
        f"{firmware.name}.cmpunlock.stock-{profile_580_105.firmware_sha256[:16]}"
    )
    assert firmware_writes == [patched, stock]
    assert firmware.read_bytes() == stock
    assert backup.read_bytes() == stock
    assert not system._journal_path(firmware.resolve()).exists()
    assert reset_checks == [(device.bdf, local_tmp_path)]
    state = json.loads(
        system._state_path(firmware.resolve()).read_text(encoding="utf-8")
    )
    assert state["cold_power_cycle_required"] is True
    assert state["firmware_restored"] is True
    assert state["patched_firmware_boot_attempted"] is True
    assert "simulated module load failure" in state["error"]


@pytest.mark.parametrize("settle_seconds", [-1.0, float("inf"), float("nan")])
def test_experimental_apply_rejects_invalid_settle_time_before_device_access(
    profile_580_105,
    local_tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    settle_seconds: float,
) -> None:
    monkeypatch.setattr(system, "require_linux", lambda: None)
    monkeypatch.setattr(system.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(
        system,
        "inspect_pci_device",
        lambda *_args, **_kwargs: pytest.fail("device inspection must not run"),
    )

    with pytest.raises(ApplyError, match="finite, nonnegative"):
        system.experimental_apply(
            "0000:01:00.0",
            local_tmp_path / "gsp_tu10x.bin",
            profile_580_105,
            acknowledgement=system.ACKNOWLEDGEMENT,
            settle_seconds=settle_seconds,
            sysfs_root=local_tmp_path,
        )
