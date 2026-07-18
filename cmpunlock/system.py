# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import contextlib
import gzip
import hashlib
import json
import lzma
import mmap
import os
import platform
import re
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
from .profile import FirmwareProfile


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


def _run(command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            check=check,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise SystemCheckError(f"required command is missing: {command[0]}") from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() or exc.stdout.strip() or f"exit {exc.returncode}"
        raise ApplyError(f"command failed ({' '.join(command)}): {detail}") from exc


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
        "profile": profile.to_summary(),
        "experimental_apply_available": True,
        "hardware_verified": False,
        "warning": (
            "community continuation addresses have no published decrypted disassembly "
            "or hardware trace"
        ),
    }


def build_apply_plan(
    bdf: str, firmware_path: Path, profile: FirmwareProfile
) -> dict[str, Any]:
    payload, payload_report = build_compute_payload(profile)
    source = firmware_path.read_bytes()
    _patched, patch_report = patch_firmware(source, payload, payload_report, profile)
    return {
        "status": "experimental-unverified",
        "device": normalize_bdf(bdf),
        "firmware": str(firmware_path.resolve()),
        "profile": profile.to_summary(),
        "patch": patch_report.as_dict(),
        "planned_stages": [
            "verify exact CMP PCI ID, firmware, driver version, and embedded booter hash",
            "require a single idle NVIDIA GPU and all NVIDIA modules unloaded",
            "write a durable stock-firmware backup and transaction journal",
            "atomically install the patched GSP image",
            "load nvidia to enter the vulnerable GA100 booter",
            "require FEAT_OVR_PLM readback to prove the HS continuation ran",
            "write and verify only the two compute-rate overrides",
            "unload nvidia cleanly, restore stock firmware, issue FLR, and load stock nvidia",
            "verify override readback after the stock reload",
        ],
        "automatic_execute": False,
        "unresolved_evidence": (
            "main.pdf omits the productive continuation and the public 0x10b9/0x810d "
            "addresses are inside encrypted Falcon IMEM with no published derivation"
        ),
    }


def _journal_path(firmware_path: Path) -> Path:
    return firmware_path.with_name(f".{firmware_path.name}.cmpunlock-transaction.json")


def _state_path(firmware_path: Path) -> Path:
    return firmware_path.with_name(f".{firmware_path.name}.cmpunlock-state.json")


def recover_firmware(firmware_path: Path) -> dict[str, Any]:
    require_linux()
    with _exclusive_lock():
        return _recover_firmware_unlocked(firmware_path)


def _recover_firmware_unlocked(firmware_path: Path) -> dict[str, Any]:
    journal_path = _journal_path(firmware_path.resolve())
    try:
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ApplyError(f"no recovery journal exists at {journal_path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ApplyError(f"cannot read recovery journal {journal_path}: {exc}") from exc
    backup = Path(journal.get("backup", ""))
    expected = journal.get("stock_sha256")
    if not backup.is_file() or not isinstance(expected, str):
        raise ApplyError("recovery journal has invalid backup metadata")
    data = backup.read_bytes()
    actual = sha256_bytes(data)
    if actual != expected:
        raise ApplyError(f"backup SHA-256 mismatch: expected {expected}, got {actual}")
    atomic_replace_bytes(firmware_path, data, metadata_from=firmware_path)
    journal_path.unlink()
    return {"restored": str(firmware_path.resolve()), "sha256": actual, "backup": str(backup)}


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
    if path.exists():
        if sha256_bytes(path.read_bytes()) != sha256_bytes(data):
            raise ApplyError(f"existing backup differs from the profiled stock firmware: {path}")
        return
    try:
        with path.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(path, 0o600)
    except OSError as exc:
        raise ApplyError(f"cannot create firmware backup {path}: {exc}") from exc


def _write_journal(path: Path, document: dict[str, Any]) -> None:
    encoded = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("ascii")
    atomic_replace_bytes(path, encoded)
    os.chmod(path, 0o600)


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
    if acknowledgement != _ACKNOWLEDGEMENT:
        raise ApplyError(f"execution requires --acknowledge {_ACKNOWLEDGEMENT}")
    if os.geteuid() != 0:
        raise ApplyError("experimental apply must run as root")

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

    old_sigterm = signal.getsignal(signal.SIGTERM)

    def interrupt(_signum: int, _frame: object) -> None:
        raise ApplyError("interrupted; restoring the stock firmware")

    with _exclusive_lock():
        _write_backup(backup, stock)
        _write_journal(
            journal_path,
            {
                "bdf": normalized,
                "backup": str(backup),
                "firmware": str(firmware_path),
                "stock_sha256": profile.firmware_sha256,
                "patched_sha256": patch_report.patched_sha256,
                "stage": "prepared",
            },
        )
        signal.signal(signal.SIGTERM, interrupt)
        module_loaded = False
        firmware_restored = False
        overrides_active = False
        overrides_ever_attempted = False
        old_values: dict[int, int] = {}
        register_results: dict[str, str] = {}
        stage = "prepared"
        primary_error: BaseException | None = None

        def write_state(error: BaseException | None = None) -> None:
            _write_journal(
                state_path,
                {
                    "bdf": normalized,
                    "cold_power_cycle_required": overrides_active or module_loaded,
                    "error": (
                        f"{type(error).__name__}: {error}" if error is not None else None
                    ),
                    "firmware": str(firmware_path),
                    "firmware_restored": firmware_restored,
                    "override_state_may_be_active": overrides_active,
                    "patched_module_may_be_loaded": module_loaded,
                    "profile": profile.profile_id,
                    "registers": register_results,
                    "stage": stage,
                    "stock_sha256": profile.firmware_sha256,
                },
            )

        try:
            try:
                atomic_replace_bytes(firmware_path, patched, metadata_from=firmware_path)
                stage = "patched-firmware-installed"
                module_loaded = True
                _run(["modprobe", "nvidia"])
                stage = "patched-module-loaded"
                time.sleep(settle_seconds)

                active = inspect_pci_device(normalized, sysfs_root)
                if active.driver != "nvidia":
                    raise ApplyError(
                        f"target did not bind to nvidia after module load: {active.driver}"
                    )
                with Bar0(active, sysfs_root) as bar0:
                    plm = bar0.read32(profile.plm_readback_address)
                    register_results["FEAT_OVR_PLM"] = f"0x{plm:08x}"
                    if plm != profile.plm_open_value:
                        raise ApplyError(
                            "HS continuation was not proven: FEAT_OVR_PLM read "
                            f"0x{plm:08x}, expected 0x{profile.plm_open_value:08x}"
                        )
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

                _run(["modprobe", "-r", "nvidia"])
                module_loaded = False
                stage = "patched-module-unloaded"
                write_state()
            except BaseException as exc:
                primary_error = exc
                if overrides_active and module_loaded and old_values:
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
            try:
                atomic_replace_bytes(firmware_path, stock, metadata_from=firmware_path)
                firmware_restored = True
                journal_path.unlink(missing_ok=True)
            except BaseException as exc:
                if primary_error is None:
                    primary_error = exc
            if overrides_ever_attempted or module_loaded or not firmware_restored:
                with contextlib.suppress(Exception):
                    write_state(primary_error)

        if primary_error is not None:
            signal.signal(signal.SIGTERM, old_sigterm)
            if overrides_active or module_loaded or not firmware_restored:
                raise ApplyError(
                    f"transaction failed with partial state: {primary_error}; "
                    f"cold power cycle required; inspect {state_path}"
                ) from primary_error
            raise primary_error
        if not overrides_active:
            signal.signal(signal.SIGTERM, old_sigterm)
            raise ApplyError("compute overrides were not left active")

        try:
            _reset_device(normalized, sysfs_root)
            stage = "flr-complete"
            write_state()
            _run(["modprobe", "nvidia"])
            time.sleep(settle_seconds)
            final_device = inspect_pci_device(normalized, sysfs_root)
            if final_device.driver != "nvidia":
                raise ApplyError(
                    "target did not bind to the stock nvidia driver after FLR: "
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
            "bdf": normalized,
            "profile": profile.profile_id,
            "firmware_restored_sha256": sha256_bytes(firmware_path.read_bytes()),
            "backup": str(backup),
            "state": str(state_path),
            "registers": register_results,
            "next_required_validation": (
                "run GEMM throughput and thermal tests; readback alone is not proof"
            ),
        }


ACKNOWLEDGEMENT = _ACKNOWLEDGEMENT
