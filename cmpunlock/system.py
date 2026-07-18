# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import contextlib
import gzip
import hashlib
import json
import lzma
import math
import mmap
import os
import platform
import re
import shutil
import signal
import struct
import subprocess
import time
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

try:
    import fcntl
except ImportError:  # Offline commands are supported on non-POSIX hosts.
    fcntl = None  # type: ignore[assignment]

from .errors import ApplyError, SystemCheckError
from .firmware import atomic_replace_bytes, patch_firmware, sha256_bytes, validate_stock_firmware
from .payload import build_compute_payload
from .profile import FirmwareProfile, RegisterWrite, bundled_profile_paths, load_profile


_BDF = re.compile(
    r"^(?:(?P<domain>[0-9a-fA-F]{4}):)?(?P<bus>[0-9a-fA-F]{2}):"
    r"(?P<slot>[0-9a-fA-F]{2})\.(?P<function>[0-7])$"
)
_NVIDIA_MODULES = {
    "nvidia",
    "nvidia_drm",
    "nvidia_modeset",
    "nvidia_uvm",
    "nvidia_peermem",
}
_ACKNOWLEDGEMENT = "UNVERIFIED-CMP170HX-EXPERIMENT"
_REPORTED_PATH_ACKNOWLEDGEMENT = (
    "UNVERIFIED-CMP170HX-REPORTED-PATH-WITH-MEMORY-SIDE-EFFECTS"
)
_COLD_CYCLE_ACKNOWLEDGEMENT = "COLD-POWER-CYCLE-COMPLETED"
_CMP_DEVICE_IDS = frozenset({"2082", "20c2"})
_REPORTED_PROFILE_ID = "ga100-580.173.02-community-reported"
_LIVE_EXPLOIT_CONTRACT = (
    0x0800,
    0xF800,
    0x6340,
    0xFACEB13D,
    0xFF48,
    0x18,
    (0x00, 0x04, 0x08, 0x0C, 0x10, 0x14),
    0x10B9,
    0x810D,
)
_LEGACY_COMPUTE_ONLY_HS_WRITES = (
    (0x00823804, 0xFFFFFFFF),
    (0x00823804, 0xFFFFFFFF),
    (0x00823804, 0xFFFFFFFF),
)
_REPORTED_HS_WRITES = (
    (0x009A0204, 0x02779000),
    (0x00100CE0, 0x0000020B),
    (0x00823804, 0xFFFFFFFF),
)
_LIVE_HOST_WRITES = (
    (0x0082381C, 0x88888888),
    (0x00823820, 0x00000008),
)


def require_linux() -> None:
    if platform.system() != "Linux":
        raise SystemCheckError("live system commands must run on Linux")


def normalize_bdf(value: str) -> str:
    match = _BDF.fullmatch(value.strip())
    if match is None:
        raise SystemCheckError(f"invalid PCI BDF: {value!r}")
    domain = match.group("domain") or "0000"
    return (
        f"{domain}:{match.group('bus')}:{match.group('slot')}."
        f"{match.group('function')}"
    ).lower()


def validate_live_profile(profile: FirmwareProfile) -> None:
    """Restrict live operations to the reviewed, bundled live contracts."""
    if not any(profile == load_profile(path) for path in bundled_profile_paths()):
        raise SystemCheckError(
            "live system commands require an unchanged bundled firmware profile"
        )
    reported = profile.execution_strategy == "reported-two-phase"
    expected_ids = frozenset({"20c2"}) if reported else _CMP_DEVICE_IDS
    if frozenset(profile.accepted_device_ids) != expected_ids:
        raise SystemCheckError("live profile changes the reviewed CMP device-ID contract")
    if reported and (
        profile.profile_id != _REPORTED_PROFILE_ID
        or profile.driver_version != "580.173.02"
        or profile.evidence != "community-reported-hardware"
    ):
        raise SystemCheckError("reported two-phase execution is pinned to 580.173.02")
    exploit_contract = (
        profile.dma_target,
        profile.payload_size,
        profile.guard_address,
        profile.canary_replacement,
        profile.frame_start,
        profile.frame_stride,
        profile.frame_fields,
        profile.bar0_write_gadget,
        profile.tail_return,
    )
    if exploit_contract != _LIVE_EXPLOIT_CONTRACT:
        raise SystemCheckError("live profile changes the reviewed exploit contract")
    hs_writes = tuple((write.address, write.value) for write in profile.hs_writes)
    expected_hs_writes = (
        _REPORTED_HS_WRITES if reported else _LEGACY_COMPUTE_ONLY_HS_WRITES
    )
    if hs_writes != expected_hs_writes:
        raise SystemCheckError("live profile changes the reviewed Heavy Secure writes")
    host_writes = tuple((write.address, write.value) for write in profile.host_writes)
    if host_writes != _LIVE_HOST_WRITES:
        raise SystemCheckError("live profile changes the reviewed host BAR0 writes")
    if (
        profile.plm_readback_address != 0x00823804
        or profile.plm_open_value != 0xFFFFFFFF
    ):
        raise SystemCheckError("live profile changes the reviewed PLM readback gate")


def _read_hex(path: Path) -> int:
    try:
        return int(path.read_text(encoding="ascii").strip(), 0)
    except (OSError, ValueError) as exc:
        raise SystemCheckError(f"cannot read hexadecimal value from {path}: {exc}") from exc


@dataclass(frozen=True)
class PciDevice:
    bdf: str
    vendor_id: str
    device_id: str
    subsystem_vendor_id: str
    subsystem_device_id: str
    class_code: str
    driver: str | None
    resource0_size: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "bdf": self.bdf,
            "vendor_id": self.vendor_id,
            "device_id": self.device_id,
            "subsystem_vendor_id": self.subsystem_vendor_id,
            "subsystem_device_id": self.subsystem_device_id,
            "class_code": self.class_code,
            "driver": self.driver,
            "resource0_size": self.resource0_size,
        }


def _resource0_size(device_path: Path) -> int:
    resource = device_path / "resource"
    try:
        first_line = resource.read_text(encoding="ascii").splitlines()[0]
        start_text, end_text, _flags = first_line.split()
        start = int(start_text, 16)
        end = int(end_text, 16)
    except (OSError, IndexError, ValueError) as exc:
        raise SystemCheckError(f"cannot parse BAR0 range from {resource}: {exc}") from exc
    return 0 if start == 0 and end == 0 else end - start + 1


def inspect_pci_device(bdf: str, sysfs_root: Path = Path("/sys")) -> PciDevice:
    normalized = normalize_bdf(bdf)
    device_path = sysfs_root / "bus" / "pci" / "devices" / normalized
    if not device_path.is_dir():
        raise SystemCheckError(f"PCI device does not exist: {normalized}")
    driver_link = device_path / "driver"
    driver = driver_link.resolve().name if driver_link.exists() else None
    return PciDevice(
        bdf=normalized,
        vendor_id=f"{_read_hex(device_path / 'vendor'):04x}",
        device_id=f"{_read_hex(device_path / 'device'):04x}",
        subsystem_vendor_id=f"{_read_hex(device_path / 'subsystem_vendor'):04x}",
        subsystem_device_id=f"{_read_hex(device_path / 'subsystem_device'):04x}",
        class_code=f"{_read_hex(device_path / 'class'):06x}",
        driver=driver,
        resource0_size=_resource0_size(device_path),
    )


def validate_target(device: PciDevice, profile: FirmwareProfile) -> None:
    if device.vendor_id != "10de":
        raise SystemCheckError(f"{device.bdf} is not an NVIDIA device")
    if device.device_id == "20b0":
        raise SystemCheckError("PCI ID 20b0 is an A100 SXM4; it is explicitly rejected")
    if device.device_id not in _CMP_DEVICE_IDS:
        accepted = ", ".join(sorted(_CMP_DEVICE_IDS))
        raise SystemCheckError(
            f"unsupported PCI device ID {device.device_id}; reviewed CMP IDs: {accepted}"
        )
    if device.device_id not in profile.accepted_device_ids:
        accepted = ", ".join(profile.accepted_device_ids)
        raise SystemCheckError(
            f"unsupported PCI device ID {device.device_id}; expected one of: {accepted}"
        )
    if device.driver not in {None, "nvidia"}:
        raise SystemCheckError(
            f"{device.bdf} is bound to unexpected driver {device.driver!r}"
        )
    if device.resource0_size < max(
        profile.plm_readback_address,
        *(write.address for write in profile.host_writes),
        *(write.address for write in profile.hs_writes),
    ) + 4:
        raise SystemCheckError("BAR0 is too small for the profiled register offsets")


def enumerate_nvidia_devices(sysfs_root: Path = Path("/sys")) -> tuple[str, ...]:
    devices_root = sysfs_root / "bus" / "pci" / "devices"
    result: list[str] = []
    if not devices_root.is_dir():
        return ()
    for path in devices_root.iterdir():
        try:
            if _read_hex(path / "vendor") == 0x10DE:
                result.append(path.name.lower())
        except SystemCheckError:
            continue
    return tuple(sorted(result))


def loaded_nvidia_modules(proc_modules: Path = Path("/proc/modules")) -> tuple[str, ...]:
    try:
        lines = proc_modules.read_text(encoding="ascii").splitlines()
    except OSError as exc:
        raise SystemCheckError(f"cannot read {proc_modules}: {exc}") from exc
    loaded = {line.split()[0] for line in lines if line.split()}
    return tuple(sorted(loaded & _NVIDIA_MODULES))


def _run(
    command: list[str],
    *,
    check: bool = True,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            check=check,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise SystemCheckError(f"required command is missing: {command[0]}") from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() or exc.stdout.strip() or f"exit {exc.returncode}"
        raise ApplyError(f"command failed ({' '.join(command)}): {detail}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ApplyError(
            f"command timed out after {timeout:g}s ({' '.join(command)})"
        ) from exc


def _require_nvidia_smi() -> str:
    path = shutil.which("nvidia-smi")
    if path is None:
        raise SystemCheckError(
            "nvidia-smi is required to trigger and verify GPU initialization"
        )
    return str(Path(path).resolve())


def _nvidia_smi_command(bdf: str, executable: str) -> list[str]:
    return [
        executable,
        "--id=" + bdf,
        "--query-gpu=pci.bus_id",
        "--format=csv,noheader",
    ]


def _bounded_output(value: str, limit: int = 512) -> str:
    value = value.strip()
    return value if len(value) <= limit else value[-limit:]


def _probe_patched_gpu(
    bdf: str,
    executable: str,
    *,
    timeout: float = 15.0,
) -> dict[str, Any]:
    command = _nvidia_smi_command(bdf, executable)
    try:
        result = _run(command, check=False, timeout=timeout)
    except ApplyError as exc:
        # Patched GSP initialization may intentionally fail. A timeout or
        # nonzero result is diagnostic; PLM readback is the mandatory gate.
        return {
            "command": command,
            "returncode": None,
            "timed_out": True,
            "detail": str(exc),
        }
    return {
        "command": command,
        "returncode": result.returncode,
        "timed_out": False,
        "stdout": _bounded_output(result.stdout),
        "stderr": _bounded_output(result.stderr),
    }


def _probe_stock_gpu(
    bdf: str,
    executable: str,
    *,
    timeout: float = 15.0,
) -> dict[str, Any]:
    command = _nvidia_smi_command(bdf, executable)
    result = _run(command, timeout=timeout)
    return {
        "command": command,
        "returncode": result.returncode,
        "timed_out": False,
        "stdout": _bounded_output(result.stdout),
        "stderr": _bounded_output(result.stderr),
    }


def _diagnostic_hs_writes(profile: FirmwareProfile) -> tuple[RegisterWrite, ...]:
    return tuple(
        write
        for write in profile.hs_writes
        if write.address != profile.plm_readback_address
    )


def module_info() -> tuple[str, Path]:
    version = _run(["modinfo", "-F", "version", "nvidia"]).stdout.strip()
    path_text = _run(["modinfo", "-n", "nvidia"]).stdout.strip()
    if not version or not path_text:
        raise SystemCheckError("modinfo returned incomplete NVIDIA module metadata")
    return version, Path(path_text)


def _module_bytes(path: Path) -> bytes:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise SystemCheckError(f"cannot read NVIDIA module {path}: {exc}") from exc
    if path.suffix == ".xz":
        try:
            return lzma.decompress(raw)
        except lzma.LZMAError as exc:
            raise SystemCheckError(f"cannot decompress NVIDIA module {path}: {exc}") from exc
    if path.suffix == ".gz":
        try:
            return gzip.decompress(raw)
        except (OSError, EOFError) as exc:
            raise SystemCheckError(f"cannot decompress NVIDIA module {path}: {exc}") from exc
    if path.suffix == ".zst":
        try:
            result = subprocess.run(
                ["zstd", "-q", "-d", "-c", str(path)],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise SystemCheckError("zstd is required to inspect a .ko.zst module") from exc
        except subprocess.CalledProcessError as exc:
            detail = exc.stderr.decode("utf-8", "replace").strip() or f"exit {exc.returncode}"
            raise SystemCheckError(f"cannot decompress NVIDIA module {path}: {detail}") from exc
        return result.stdout
    return raw


def extract_profiled_booter(module: bytes, profile: FirmwareProfile) -> bytes:
    positions: list[int] = []
    start = 0
    while True:
        position = module.find(profile.booter_compressed_prefix, start)
        if position < 0:
            break
        positions.append(position)
        start = position + 1
    if len(positions) != 1:
        raise SystemCheckError(
            "expected one profiled GA100 production-booter stream in nvidia.ko, "
            f"found {len(positions)}"
        )
    position = positions[0]
    end = position + profile.booter_compressed_size
    if end > len(module):
        raise SystemCheckError("profiled booter stream is truncated in nvidia.ko")
    try:
        booter = zlib.decompress(module[position:end], -zlib.MAX_WBITS)
    except zlib.error as exc:
        raise SystemCheckError(f"cannot decompress profiled GA100 booter: {exc}") from exc
    digest = sha256_bytes(booter)
    if len(booter) != profile.booter_size or digest != profile.booter_sha256:
        raise SystemCheckError(
            "embedded GA100 booter mismatch: "
            f"size={len(booter)}, sha256={digest}"
        )
    return booter


def validate_module(profile: FirmwareProfile) -> dict[str, Any]:
    version, path = module_info()
    if version != profile.driver_version:
        raise SystemCheckError(
            f"NVIDIA module version mismatch: expected {profile.driver_version}, got {version}"
        )
    module = _module_bytes(path)
    booter = extract_profiled_booter(module, profile)
    return {
        "version": version,
        "path": str(path),
        "module_sha256": sha256_bytes(module),
        "booter_size": len(booter),
        "booter_sha256": sha256_bytes(booter),
    }


class Bar0:
    def __init__(self, device: PciDevice, sysfs_root: Path = Path("/sys")) -> None:
        self.device = device
        self.path = sysfs_root / "bus" / "pci" / "devices" / device.bdf / "resource0"
        self._fd: int | None = None
        self._mapping: mmap.mmap | None = None

    def __enter__(self) -> "Bar0":
        try:
            self._fd = os.open(self.path, os.O_RDWR | os.O_SYNC)
            self._mapping = mmap.mmap(
                self._fd, self.device.resource0_size, access=mmap.ACCESS_WRITE
            )
        except (OSError, ValueError) as exc:
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None
            raise ApplyError(f"cannot open BAR0 at {self.path}: {exc}") from exc
        return self

    def __exit__(self, *_args: object) -> None:
        if self._mapping is not None:
            self._mapping.close()
            self._mapping = None
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def _check(self, offset: int) -> None:
        if offset < 0 or offset % 4 or offset + 4 > self.device.resource0_size:
            raise ApplyError(f"invalid BAR0 dword offset: 0x{offset:x}")
        if self._mapping is None:
            raise ApplyError("BAR0 is not open")

    def read32(self, offset: int) -> int:
        self._check(offset)
        assert self._mapping is not None
        try:
            return struct.unpack_from("<I", self._mapping, offset)[0]
        except (OSError, ValueError, struct.error) as exc:
            raise ApplyError(f"BAR0 read failed at 0x{offset:x}: {exc}") from exc

    def write32(self, offset: int, value: int) -> None:
        self._check(offset)
        if not 0 <= value <= 0xFFFFFFFF:
            raise ApplyError("BAR0 write value does not fit in 32 bits")
        assert self._mapping is not None
        try:
            struct.pack_into("<I", self._mapping, offset, value)
        except (OSError, ValueError, struct.error) as exc:
            raise ApplyError(f"BAR0 write failed at 0x{offset:x}: {exc}") from exc


def inspect_system(
    bdf: str,
    firmware_path: Path,
    profile: FirmwareProfile,
    *,
    sysfs_root: Path = Path("/sys"),
) -> dict[str, Any]:
    require_linux()
    validate_live_profile(profile)
    device = inspect_pci_device(bdf, sysfs_root)
    validate_target(device, profile)
    if device.driver != "nvidia":
        raise SystemCheckError(
            "the stock NVIDIA driver must already be loaded and bound during system inspect"
        )
    try:
        firmware = firmware_path.read_bytes()
    except OSError as exc:
        raise SystemCheckError(f"cannot read firmware {firmware_path}: {exc}") from exc
    validate_stock_firmware(firmware, profile)
    module = validate_module(profile)
    nvidia_smi = _require_nvidia_smi()
    stock_probe = _probe_stock_gpu(normalize_bdf(bdf), nvidia_smi)
    return {
        "device": device.as_dict(),
        "nvidia_devices": list(enumerate_nvidia_devices(sysfs_root)),
        "loaded_nvidia_modules": list(loaded_nvidia_modules()),
        "firmware": {
            "path": str(firmware_path.resolve()),
            "size": len(firmware),
            "sha256": sha256_bytes(firmware),
        },
        "module": module,
        "nvidia_smi": {
            "path": nvidia_smi,
            "stock_probe": stock_probe,
        },
        "profile": profile.to_summary(),
        "experimental_apply_available": True,
        "hardware_verified": False,
        "warning": (
            "580.173.02 has one community hardware report but no independently "
            "reproduced trace or benchmark artifact"
            if profile.execution_strategy == "reported-two-phase"
            else "community continuation addresses have no published decrypted "
            "disassembly or hardware trace"
        ),
    }


def build_apply_plan(
    bdf: str, firmware_path: Path, profile: FirmwareProfile
) -> dict[str, Any]:
    validate_live_profile(profile)
    payload, payload_report = build_compute_payload(profile)
    source = firmware_path.read_bytes()
    _patched, patch_report = patch_firmware(source, payload, payload_report, profile)
    if profile.execution_strategy == "reported-two-phase":
        planned_stages = [
            "verify exact 20c2 device, 580.173.02 firmware/module, and embedded booter",
            "require one idle NVIDIA GPU, all NVIDIA modules unloaded, and nvidia-smi",
            "record PLM, compute, FBPA, and LMR baselines",
            "write a durable stock-firmware backup and transaction journal",
            "atomically install the patched GSP image",
            "load nvidia and run a bounded best-effort nvidia-smi initialization probe",
            "issue FLR #1, unload nvidia cleanly, restore stock, then issue FLR #2",
            "require FEAT_OVR_PLM all-open readback after both resets",
            "record FBPA/LMR diagnostically and write only the two compute overrides",
            "load stock nvidia without force removal",
            "require stock nvidia-smi success and verify compute override readback",
        ]
    else:
        planned_stages = [
            "verify exact CMP PCI ID, firmware, driver version, and embedded booter hash",
            "require a single idle NVIDIA GPU and all NVIDIA modules unloaded",
            "write a durable stock-firmware backup and transaction journal",
            "atomically install the patched GSP image",
            "load nvidia and run a bounded best-effort nvidia-smi initialization probe",
            "require a changed FEAT_OVR_PLM readback consistent with the HS continuation",
            "write and verify only the two compute-rate overrides",
            "unload nvidia cleanly, restore stock firmware, issue FLR, and load stock nvidia",
            "require stock nvidia-smi success and verify override readback",
        ]
    return {
        "status": "experimental-unverified",
        "device": normalize_bdf(bdf),
        "firmware": str(firmware_path.resolve()),
        "profile": profile.to_summary(),
        "patch": patch_report.as_dict(),
        "planned_stages": planned_stages,
        "required_acknowledgement": (
            _REPORTED_PATH_ACKNOWLEDGEMENT
            if profile.execution_strategy == "reported-two-phase"
            else _ACKNOWLEDGEMENT
        ),
        "memory_capacity_verified": False,
        "automatic_execute": False,
        "unresolved_evidence": (
            "one 580.173.02/20c2 community report reached compute, but it publishes "
            "no decrypted derivation or independently reproducible trace and reports "
            "that memory remained at 8 GiB"
            if profile.execution_strategy == "reported-two-phase"
            else "main.pdf omits the productive continuation and the public "
            "0x10b9/0x810d addresses are inside encrypted Falcon IMEM with no "
            "published derivation"
        ),
    }


def _journal_path(firmware_path: Path) -> Path:
    return firmware_path.with_name(f".{firmware_path.name}.cmpunlock-transaction.json")


def _state_path(firmware_path: Path) -> Path:
    return firmware_path.with_name(f".{firmware_path.name}.cmpunlock-state.json")


def _bundled_profile_for_digest(digest: str) -> FirmwareProfile:
    profiles = [load_profile(path) for path in bundled_profile_paths()]
    matches = [profile for profile in profiles if profile.firmware_sha256 == digest]
    if len(matches) != 1:
        raise ApplyError(
            f"stock SHA-256 does not identify one bundled profile: {digest}"
        )
    return matches[0]


def recover_firmware(firmware_path: Path) -> dict[str, Any]:
    require_linux()
    with _exclusive_lock():
        return _recover_firmware_unlocked(firmware_path)


def _recover_firmware_unlocked(firmware_path: Path) -> dict[str, Any]:
    firmware_path = firmware_path.resolve()
    journal_path = _journal_path(firmware_path)
    try:
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ApplyError(f"no recovery journal exists at {journal_path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ApplyError(f"cannot read recovery journal {journal_path}: {exc}") from exc
    if not isinstance(journal, dict):
        raise ApplyError("recovery journal must contain a JSON object")
    schema = journal.get("schema_version")
    if schema not in {None, 2}:
        raise ApplyError(f"unsupported recovery journal schema: {schema!r}")
    recorded_firmware = journal.get("firmware")
    if not isinstance(recorded_firmware, str) or not recorded_firmware:
        raise ApplyError("recovery journal has no firmware path")
    if Path(recorded_firmware).resolve() != firmware_path:
        raise ApplyError(
            "recovery journal firmware does not match the requested path: "
            f"{recorded_firmware}"
        )
    backup_text = journal.get("backup")
    expected = journal.get("stock_sha256")
    if not isinstance(backup_text, str) or not isinstance(expected, str):
        raise ApplyError("recovery journal has invalid backup metadata")
    backup = Path(backup_text)
    if not backup.is_absolute():
        raise ApplyError("recovery journal backup path must be absolute")
    profile = _bundled_profile_for_digest(expected)
    expected_backup = firmware_path.with_name(
        f"{firmware_path.name}.cmpunlock.stock-{expected[:16]}"
    )
    if backup.resolve() != expected_backup:
        raise ApplyError(
            f"recovery journal backup path is unexpected: {backup}; "
            f"expected {expected_backup}"
        )
    if not backup.is_file():
        raise ApplyError(f"recovery backup does not exist: {backup}")
    data = backup.read_bytes()
    actual = sha256_bytes(data)
    if actual != expected:
        raise ApplyError(f"backup SHA-256 mismatch: expected {expected}, got {actual}")
    validate_stock_firmware(data, profile)
    state_path = _state_path(firmware_path)
    prior_state: dict[str, Any] | None = None
    state_requires_cold_cycle = False
    state_warning: str | None = None
    if state_path.is_symlink():
        state_requires_cold_cycle = True
        state_warning = "state record is a symlink; treating it conservatively"
    elif state_path.exists() and not state_path.is_file():
        state_requires_cold_cycle = True
        state_warning = "state record is not a regular file; treating it conservatively"
    elif state_path.is_file():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            state_requires_cold_cycle = True
            state_warning = f"state record is unreadable; treating it conservatively: {exc}"
        else:
            invalid_reasons: list[str] = []
            if not isinstance(state, dict):
                invalid_reasons.append("it is not a JSON object")
            else:
                prior_state = state
                if state.get("schema_version") not in {None, 1}:
                    invalid_reasons.append("its schema is unsupported")
                if not isinstance(state.get("cold_power_cycle_required"), bool):
                    invalid_reasons.append("its cold-cycle flag is missing or invalid")
                recorded_state_firmware = state.get("firmware")
                if not isinstance(recorded_state_firmware, str):
                    invalid_reasons.append("its firmware path is missing or invalid")
                else:
                    try:
                        if Path(recorded_state_firmware).resolve() != firmware_path:
                            invalid_reasons.append("its firmware path does not match")
                    except OSError:
                        invalid_reasons.append("its firmware path cannot be resolved")
                if state.get("stock_sha256") != expected:
                    invalid_reasons.append("its stock digest does not match")
            if invalid_reasons:
                state_requires_cold_cycle = True
                state_warning = (
                    "state record is structurally invalid; treating it conservatively: "
                    + "; ".join(invalid_reasons)
                )
            else:
                state_requires_cold_cycle = state["cold_power_cycle_required"] is True
    journal_boot_may_have_run = journal.get("patched_boot_may_have_run")
    journal_requires_cold_cycle = (
        True if not isinstance(journal_boot_may_have_run, bool) else journal_boot_may_have_run
    )
    cold_power_cycle_required = journal_requires_cold_cycle or state_requires_cold_cycle
    atomic_replace_bytes(firmware_path, data, metadata_from=firmware_path)
    if cold_power_cycle_required:
        recovered_state = dict(prior_state or {})
        recovered_state.update(
            {
                "schema_version": 1,
                "cold_power_cycle_required": True,
                "firmware": str(firmware_path),
                "firmware_restored": True,
                "override_state_may_be_active": True,
                "patched_firmware_boot_attempted": (
                    journal_requires_cold_cycle
                    or recovered_state.get("patched_firmware_boot_attempted") is True
                ),
                "stage": "recovered-stock-cold-cycle-required",
                "stock_sha256": expected,
            }
        )
        if state_warning is not None:
            recovered_state["recovery_warning"] = state_warning
        # The state interlock must be durable before the journal is removed. If
        # this write or the validation fails, the exception leaves the journal
        # in place so a later apply remains blocked.
        _write_journal(state_path, recovered_state)
        try:
            persisted_state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ApplyError(f"cannot validate recovered state record {state_path}: {exc}") from exc
        if not (
            isinstance(persisted_state, dict)
            and persisted_state.get("cold_power_cycle_required") is True
            and persisted_state.get("firmware") == str(firmware_path)
            and persisted_state.get("firmware_restored") is True
            and persisted_state.get("stock_sha256") == expected
        ):
            raise ApplyError(f"recovered state record failed validation: {state_path}")
    journal_path.unlink()
    return {
        "restored": str(firmware_path),
        "sha256": actual,
        "backup": str(backup),
        "cold_power_cycle_required": cold_power_cycle_required,
        "state": str(state_path) if state_path.exists() else None,
        "state_warning": state_warning,
    }


def clear_state(firmware_path: Path, *, acknowledgement: str) -> dict[str, Any]:
    require_linux()
    if acknowledgement != _COLD_CYCLE_ACKNOWLEDGEMENT:
        raise ApplyError(
            "state clearing requires --acknowledge " + _COLD_CYCLE_ACKNOWLEDGEMENT
        )
    if os.geteuid() != 0:
        raise ApplyError("state clearing must run as root")
    firmware_path = firmware_path.resolve()
    journal_path = _journal_path(firmware_path)
    state_path = _state_path(firmware_path)
    with _exclusive_lock():
        if journal_path.exists() or journal_path.is_symlink():
            raise ApplyError(
                f"an unresolved recovery journal exists at {journal_path}; recover first"
            )
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ApplyError(f"no state record exists at {state_path}") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise ApplyError(f"cannot read state record {state_path}: {exc}") from exc
        if not isinstance(state, dict):
            raise ApplyError("state record must contain a JSON object")
        recorded_firmware = state.get("firmware")
        digest = state.get("stock_sha256")
        if (
            not isinstance(recorded_firmware, str)
            or Path(recorded_firmware).resolve() != firmware_path
            or not isinstance(digest, str)
        ):
            raise ApplyError("state record does not match the requested firmware")
        profile = _bundled_profile_for_digest(digest)
        validate_stock_firmware(firmware_path.read_bytes(), profile)
        prior_stage = state.get("stage")
        state_path.unlink()
    return {
        "cleared": str(state_path),
        "firmware": str(firmware_path),
        "prior_stage": prior_stage,
        "stock_sha256": digest,
    }


@contextlib.contextmanager
def _exclusive_lock() -> Iterator[None]:
    if fcntl is None:
        raise ApplyError("POSIX file locking is unavailable on this host")
    lock_path = Path("/run/lock/cmpunlock.lock")
    with lock_path.open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ApplyError(f"another CMP unlock transaction holds {lock_path}") from exc
        yield


def _write_backup(path: Path, data: bytes) -> None:
    if path.is_symlink():
        raise ApplyError(f"firmware backup path is a symlink: {path}")
    if path.exists():
        if sha256_bytes(path.read_bytes()) != sha256_bytes(data):
            raise ApplyError(f"existing backup differs from the profiled stock firmware: {path}")
        return
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name != "nt":
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except OSError as exc:
        raise ApplyError(f"cannot create firmware backup {path}: {exc}") from exc


def _write_journal(path: Path, document: dict[str, Any]) -> None:
    if path.is_symlink():
        raise ApplyError(f"transaction record path is a symlink: {path}")
    encoded = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("ascii")
    atomic_replace_bytes(path, encoded, mode=0o600)


def _restore_registers(bar0: Bar0, old_values: dict[int, int]) -> bool:
    for address, value in old_values.items():
        bar0.write32(address, value)
    return all(bar0.read32(address) == value for address, value in old_values.items())


def _reset_path(bdf: str, sysfs_root: Path) -> Path:
    return sysfs_root / "bus" / "pci" / "devices" / bdf / "reset"


def _require_reset_available(bdf: str, sysfs_root: Path) -> None:
    reset = _reset_path(bdf, sysfs_root)
    if not reset.exists():
        raise ApplyError(f"PCI function reset is unavailable at {reset}")


def _reset_device(bdf: str, sysfs_root: Path) -> None:
    reset = _reset_path(bdf, sysfs_root)
    try:
        reset.write_text("1\n", encoding="ascii")
    except OSError as exc:
        raise ApplyError(f"PCI function reset failed: {exc}") from exc


def experimental_apply(
    bdf: str,
    firmware_path: Path,
    profile: FirmwareProfile,
    *,
    acknowledgement: str,
    settle_seconds: float = 5.0,
    sysfs_root: Path = Path("/sys"),
) -> dict[str, Any]:
    require_linux()
    validate_live_profile(profile)
    reported_two_phase = profile.execution_strategy == "reported-two-phase"
    required_acknowledgement = (
        _REPORTED_PATH_ACKNOWLEDGEMENT if reported_two_phase else _ACKNOWLEDGEMENT
    )
    if acknowledgement != required_acknowledgement:
        raise ApplyError(
            f"execution requires --acknowledge {required_acknowledgement}"
        )
    if os.geteuid() != 0:
        raise ApplyError("experimental apply must run as root")
    if not math.isfinite(settle_seconds) or settle_seconds < 0:
        raise ApplyError("settle seconds must be a finite, nonnegative value")

    normalized = normalize_bdf(bdf)
    device = inspect_pci_device(normalized, sysfs_root)
    validate_target(device, profile)
    devices = enumerate_nvidia_devices(sysfs_root)
    if devices != (normalized,):
        raise ApplyError(
            "experimental apply requires the target to be the only NVIDIA PCI function; "
            f"found: {', '.join(devices) or 'none'}"
        )
    _require_reset_available(normalized, sysfs_root)
    loaded = loaded_nvidia_modules()
    if loaded:
        raise ApplyError(
            "NVIDIA modules must be unloaded before execution; still loaded: " + ", ".join(loaded)
        )
    validate_module(profile)
    nvidia_smi = _require_nvidia_smi()

    firmware_path = firmware_path.resolve()
    stock = firmware_path.read_bytes()
    validate_stock_firmware(stock, profile)
    payload, payload_report = build_compute_payload(profile)
    patched, patch_report = patch_firmware(stock, payload, payload_report, profile)
    backup = firmware_path.with_name(
        f"{firmware_path.name}.cmpunlock.stock-{profile.firmware_sha256[:16]}"
    )
    journal_path = _journal_path(firmware_path)
    state_path = _state_path(firmware_path)
    try:
        free_bytes = shutil.disk_usage(firmware_path.parent).free
    except OSError as exc:
        raise ApplyError(f"cannot determine free firmware-filesystem space: {exc}") from exc
    required_free_bytes = len(patched) + (0 if backup.exists() else len(stock)) + 1024 * 1024
    if free_bytes < required_free_bytes:
        raise ApplyError(
            "insufficient free space for patched image, backup, and atomic recovery: "
            f"need {required_free_bytes} bytes, have {free_bytes}"
        )

    old_sigterm = signal.getsignal(signal.SIGTERM)

    def interrupt(_signum: int, _frame: object) -> None:
        raise ApplyError("interrupted; restoring the stock firmware")

    with _exclusive_lock():
        if journal_path.exists() or journal_path.is_symlink():
            raise ApplyError(
                f"unresolved recovery journal exists at {journal_path}; run system recover"
            )
        if state_path.exists() or state_path.is_symlink():
            raise ApplyError(
                f"unresolved hardware state exists at {state_path}; complete a cold power "
                "cycle, then run system state-clear"
            )
        with Bar0(device, sysfs_root) as bar0:
            baseline_registers = {
                "FEAT_OVR_PLM": f"0x{bar0.read32(profile.plm_readback_address):08x}",
                **{
                    write.name: f"0x{bar0.read32(write.address):08x}"
                    for write in profile.host_writes
                },
                **{
                    write.name: f"0x{bar0.read32(write.address):08x}"
                    for write in _diagnostic_hs_writes(profile)
                },
            }
        if baseline_registers["FEAT_OVR_PLM"] == f"0x{profile.plm_open_value:08x}":
            raise ApplyError(
                "FEAT_OVR_PLM was already open before the patched boot; refusing to "
                "attribute stale hardware state to this transaction; complete a cold "
                "power cycle"
            )
        _write_backup(backup, stock)
        transaction = {
            "schema_version": 2,
            "bdf": normalized,
            "backup": str(backup),
            "firmware": str(firmware_path),
            "patched_boot_may_have_run": False,
            "stock_sha256": profile.firmware_sha256,
            "patched_sha256": patch_report.patched_sha256,
            "baseline_registers": baseline_registers,
            "stage": "prepared",
        }
        _write_journal(
            journal_path,
            transaction,
        )
        signal.signal(signal.SIGTERM, interrupt)
        module_loaded = False
        patched_boot_attempted = False
        firmware_restored = False
        overrides_active = False
        overrides_ever_attempted = False
        old_values: dict[int, int] = {}
        register_results: dict[str, str] = {}
        patched_probe_result: dict[str, Any] | None = None
        stock_probe_result: dict[str, Any] | None = None
        stock_module_loaded = False
        stage = "prepared"
        primary_error: BaseException | None = None

        def write_state(error: BaseException | None = None) -> None:
            reported_hs_side_effects_may_be_active = (
                reported_two_phase and patched_boot_attempted
            )
            _write_journal(
                state_path,
                {
                    "schema_version": 1,
                    "bdf": normalized,
                    "baseline_registers": baseline_registers,
                    "cold_power_cycle_required": (
                        patched_boot_attempted or overrides_active or module_loaded
                    ),
                    "error": (
                        f"{type(error).__name__}: {error}" if error is not None else None
                    ),
                    "execution_strategy": profile.execution_strategy,
                    "firmware": str(firmware_path),
                    "firmware_restored": firmware_restored,
                    "memory_capacity_verified": False,
                    "host_compute_overrides_may_be_active": overrides_active,
                    "override_state_may_be_active": (
                        overrides_active or reported_hs_side_effects_may_be_active
                    ),
                    "patched_firmware_boot_attempted": patched_boot_attempted,
                    "patched_gpu_initialization_probe": patched_probe_result,
                    "patched_module_may_be_loaded": module_loaded,
                    "profile": profile.profile_id,
                    "reported_hs_side_effects_may_be_active": (
                        reported_hs_side_effects_may_be_active
                    ),
                    "patched_sha256": patch_report.patched_sha256,
                    "registers": register_results,
                    "stage": stage,
                    "stock_gpu_verification": stock_probe_result,
                    "stock_module_may_be_loaded": stock_module_loaded,
                    "stock_sha256": profile.firmware_sha256,
                },
            )

        def restore_stock_on_disk() -> None:
            nonlocal firmware_restored
            atomic_replace_bytes(firmware_path, stock, metadata_from=firmware_path)
            firmware_restored = True

        def apply_compute_overrides(active: PciDevice, phase: str) -> None:
            nonlocal overrides_active, overrides_ever_attempted, stage
            with Bar0(active, sysfs_root) as bar0:
                plm = bar0.read32(profile.plm_readback_address)
                register_results[f"{phase}_FEAT_OVR_PLM"] = f"0x{plm:08x}"
                if plm != profile.plm_open_value:
                    raise ApplyError(
                        "HS continuation readback gate failed: FEAT_OVR_PLM read "
                        f"0x{plm:08x}, expected 0x{profile.plm_open_value:08x}"
                    )
                for diagnostic in _diagnostic_hs_writes(profile):
                    value = bar0.read32(diagnostic.address)
                    register_results[f"{phase}_{diagnostic.name}"] = f"0x{value:08x}"
                stage = "override-write-started"
                overrides_active = True
                overrides_ever_attempted = True
                write_state()
                try:
                    for write in profile.host_writes:
                        old_values[write.address] = bar0.read32(write.address)
                        bar0.write32(write.address, write.value)
                        readback = bar0.read32(write.address)
                        register_results[write.name] = f"0x{readback:08x}"
                        if readback != write.value:
                            raise ApplyError(
                                f"{write.name} did not stick: read 0x{readback:08x}, "
                                f"expected 0x{write.value:08x}"
                            )
                    stage = "overrides-written"
                    write_state()
                except BaseException:
                    if _restore_registers(bar0, old_values):
                        overrides_active = False
                    raise

        try:
            try:
                atomic_replace_bytes(firmware_path, patched, metadata_from=firmware_path)
                stage = "patched-boot-attempting"
                transaction["stage"] = stage
                transaction["patched_boot_may_have_run"] = True
                _write_journal(journal_path, transaction)
                patched_boot_attempted = True
                write_state()
                module_loaded = True
                _run(["modprobe", "nvidia"])
                stage = "patched-module-load-returned"
                write_state()
                patched_probe_result = _probe_patched_gpu(normalized, nvidia_smi)
                stage = "patched-gpu-probe-complete"
                write_state()
                time.sleep(settle_seconds)

                if reported_two_phase:
                    _reset_device(normalized, sysfs_root)
                    stage = "reported-flr-1-complete"
                    write_state()
                    time.sleep(settle_seconds)
                    _run(["modprobe", "-r", "nvidia"])
                    module_loaded = False
                    stage = "patched-module-unloaded"
                    write_state()
                    restore_stock_on_disk()
                    stage = "stock-firmware-restored-after-patched-unload"
                    write_state()
                    _reset_device(normalized, sysfs_root)
                    stage = "reported-flr-2-complete"
                    write_state()
                    time.sleep(settle_seconds)
                    active = inspect_pci_device(normalized, sysfs_root)
                    if active.driver is not None:
                        raise ApplyError(
                            "target rebound unexpectedly after the clean patched-module "
                            f"unload: {active.driver}"
                        )
                    apply_compute_overrides(active, "post_reported_flr")
                else:
                    active = inspect_pci_device(normalized, sysfs_root)
                    if active.driver != "nvidia":
                        raise ApplyError(
                            f"target did not bind to nvidia after module load: {active.driver}"
                        )
                    apply_compute_overrides(active, "patched_boot")
                    _run(["modprobe", "-r", "nvidia"])
                    module_loaded = False
                    stage = "patched-module-unloaded"
                    write_state()
            except BaseException as exc:
                primary_error = exc
                if overrides_active and old_values:
                    try:
                        active = inspect_pci_device(normalized, sysfs_root)
                        with Bar0(active, sysfs_root) as bar0:
                            if _restore_registers(bar0, old_values):
                                overrides_active = False
                                stage = "overrides-rolled-back"
                    except BaseException:
                        pass
        finally:
            if module_loaded:
                try:
                    _run(["modprobe", "-r", "nvidia"])
                    module_loaded = False
                except BaseException as exc:
                    if primary_error is None:
                        primary_error = exc
            if not firmware_restored:
                try:
                    restore_stock_on_disk()
                except BaseException as exc:
                    if primary_error is None:
                        primary_error = exc
            if (
                patched_boot_attempted
                or overrides_ever_attempted
                or module_loaded
                or not firmware_restored
            ):
                try:
                    write_state(primary_error)
                except BaseException as exc:
                    if primary_error is None:
                        primary_error = exc
                else:
                    if firmware_restored:
                        journal_path.unlink(missing_ok=True)
            elif firmware_restored:
                journal_path.unlink(missing_ok=True)

        if primary_error is not None:
            signal.signal(signal.SIGTERM, old_sigterm)
            if (
                patched_boot_attempted
                or overrides_active
                or module_loaded
                or not firmware_restored
            ):
                raise ApplyError(
                    f"transaction failed with partial state: {primary_error}; "
                    "cold power cycle required; inspect the state record or retained "
                    f"recovery journal beside {firmware_path}"
                ) from primary_error
            raise primary_error
        if not overrides_active:
            signal.signal(signal.SIGTERM, old_sigterm)
            raise ApplyError("compute overrides were not left active")

        try:
            if reported_two_phase:
                stage = "stock-firmware-ready-after-reported-flrs"
            else:
                _reset_device(normalized, sysfs_root)
                stage = "flr-complete"
            write_state()
            stock_module_loaded = True
            stage = "stock-module-load-attempting"
            write_state()
            _run(["modprobe", "nvidia"])
            stage = "stock-module-load-returned"
            write_state()
            time.sleep(settle_seconds)
            stock_probe_result = _probe_stock_gpu(normalized, nvidia_smi)
            final_device = inspect_pci_device(normalized, sysfs_root)
            if final_device.driver != "nvidia":
                raise ApplyError(
                    "target did not bind to the stock nvidia driver after restoration: "
                    f"{final_device.driver}"
                )
            stage = "stock-driver-loaded"
            write_state()
            with Bar0(final_device, sysfs_root) as bar0:
                for write in profile.host_writes:
                    value = bar0.read32(write.address)
                    register_results[f"post_reload_{write.name}"] = f"0x{value:08x}"
                    if value != write.value:
                        raise ApplyError(
                            f"{write.name} did not persist across FLR/stock reload: 0x{value:08x}"
                        )
                for diagnostic in _diagnostic_hs_writes(profile):
                    value = bar0.read32(diagnostic.address)
                    register_results[
                        f"post_reload_{diagnostic.name}"
                    ] = f"0x{value:08x}"
            stage = "complete"
            write_state()
        except BaseException as exc:
            with contextlib.suppress(Exception):
                write_state(exc)
            signal.signal(signal.SIGTERM, old_sigterm)
            raise ApplyError(
                f"override state may remain after transaction failure: {exc}; "
                f"cold power cycle required; inspect {state_path}"
            ) from exc

        signal.signal(signal.SIGTERM, old_sigterm)
        return {
            "status": "register-readback-passed",
            "hardware_performance_verified": False,
            "memory_capacity_verified": False,
            "bdf": normalized,
            "profile": profile.profile_id,
            "execution_strategy": profile.execution_strategy,
            "firmware_restored_sha256": sha256_bytes(firmware_path.read_bytes()),
            "backup": str(backup),
            "baseline_registers": baseline_registers,
            "state": str(state_path),
            "patched_gpu_initialization_probe": patched_probe_result,
            "stock_gpu_verification": stock_probe_result,
            "registers": register_results,
            "next_required_validation": (
                "run GEMM throughput and thermal tests; readback alone is not proof"
            ),
        }


ACKNOWLEDGEMENT = _ACKNOWLEDGEMENT
REPORTED_PATH_ACKNOWLEDGEMENT = _REPORTED_PATH_ACKNOWLEDGEMENT
COLD_CYCLE_ACKNOWLEDGEMENT = _COLD_CYCLE_ACKNOWLEDGEMENT
