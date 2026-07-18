# SPDX-License-Identifier: GPL-2.0-only

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .errors import CmpUnlockError, FirmwareError, ProfileError
from .firmware import (
    atomic_replace_bytes,
    inspect_firmware,
    patch_firmware,
    validate_stock_firmware,
)
from .payload import build_payload
from .profile import (
    FirmwareProfile,
    bundled_profile_paths,
    load_profile,
    match_profile_for_firmware,
)
from .system import (
    build_apply_plan,
    clear_state,
    experimental_apply,
    inspect_system,
    recover_firmware,
)


def _json(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def _profile(value: str | None, firmware: Path | None = None) -> FirmwareProfile:
    if value is not None:
        return load_profile(value)
    if firmware is not None:
        return match_profile_for_firmware(firmware)
    raise ProfileError(
        "select --profile or provide a stock firmware that matches a bundled profile"
    )


def _write_new(path: Path, data: bytes, force: bool) -> None:
    if path.is_symlink():
        raise FirmwareError(f"output path is a symlink: {path}; refusing to follow it")
    if path.exists() and not force:
        raise FirmwareError(f"output exists: {path}; pass --force to replace it")
    path.parent.mkdir(parents=True, exist_ok=True)
    if force:
        atomic_replace_bytes(path, data, metadata_from=path if path.exists() else None)
        return
    with path.open("xb") as handle:
        handle.write(data)


def _same_file(left: Path, right: Path) -> bool:
    try:
        return left.samefile(right)
    except FileNotFoundError:
        return left.resolve() == right.resolve()
    except OSError as exc:
        raise FirmwareError(f"cannot compare input and output paths: {exc}") from exc


def command_profile_list(_args: argparse.Namespace) -> None:
    _json([load_profile(path).to_summary() for path in bundled_profile_paths()])


def command_profile_show(args: argparse.Namespace) -> None:
    profile = load_profile(args.profile)
    result = profile.to_summary()
    result.update(
        {
            "signature_section": profile.signature_section,
            "signature_offset": f"0x{profile.signature_offset:x}",
            "signature_size": profile.signature_size,
            "payload_size": profile.payload_size,
            "guard_address": f"0x{profile.guard_address:x}",
            "proof_fill": f"0x{profile.proof_fill:x}",
            "bar0_write_gadget": f"0x{profile.bar0_write_gadget:x}",
            "tail_return": f"0x{profile.tail_return:x}",
            "hs_writes": [write.__dict__ for write in profile.hs_writes],
            "host_writes": [write.__dict__ for write in profile.host_writes],
        }
    )
    _json(result)


def command_firmware_inspect(args: argparse.Namespace) -> None:
    path = Path(args.input)
    data = path.read_bytes()
    result = inspect_firmware(data)
    try:
        profile = _profile(args.profile, path)
        validate_stock_firmware(data, profile)
    except CmpUnlockError as exc:
        result["stock_profile"] = None
        result["stock_validation"] = str(exc)
    else:
        result["stock_profile"] = profile.to_summary()
        result["stock_validation"] = "passed"
    _json(result)


def command_payload_build(args: argparse.Namespace) -> None:
    profile = _profile(args.profile)
    payload, report = build_payload(profile, args.mode)
    _write_new(Path(args.output), payload, args.force)
    result = report.as_dict()
    result["output"] = str(Path(args.output).resolve())
    result["profile"] = profile.profile_id
    if args.mode == "proof":
        result["evidence_warning"] = (
            "paper proof-of-control only; this payload intentionally does not resume the driver"
        )
    elif profile.evidence == "community-reported-hardware":
        result["evidence_warning"] = (
            "single community-reported 20c2 compute result; not independently reproduced "
            "and not evidence of a memory-capacity unlock"
        )
    else:
        result["evidence_warning"] = (
            "productive continuation is community-derived and has not been reproduced on hardware"
        )
    _json(result)


def command_firmware_patch(args: argparse.Namespace) -> None:
    input_path = Path(args.input)
    output_path = Path(args.output)
    if _same_file(input_path, output_path):
        raise FirmwareError("input and output refer to the same file; refusing to overwrite stock")
    profile = _profile(args.profile, input_path)
    source = input_path.read_bytes()
    payload, payload_report = build_payload(profile, args.mode)
    patched, report = patch_firmware(source, payload, payload_report, profile)
    _write_new(output_path, patched, args.force)
    result = report.as_dict()
    result.update(
        {
            "input": str(input_path.resolve()),
            "output": str(output_path.resolve()),
            "profile": profile.profile_id,
            "hardware_verified": False,
        }
    )
    _json(result)


def command_system_inspect(args: argparse.Namespace) -> None:
    firmware = Path(args.firmware)
    profile = _profile(args.profile, firmware)
    _json(inspect_system(args.bdf, firmware, profile))


def command_system_plan(args: argparse.Namespace) -> None:
    firmware = Path(args.firmware)
    profile = _profile(args.profile, firmware)
    _json(build_apply_plan(args.bdf, firmware, profile))


def command_system_apply(args: argparse.Namespace) -> None:
    if not args.execute:
        raise CmpUnlockError("system apply is inert unless --execute is present")
    firmware = Path(args.firmware)
    profile = _profile(args.profile, firmware)
    _json(
        experimental_apply(
            args.bdf,
            firmware,
            profile,
            acknowledgement=args.acknowledge,
            settle_seconds=args.settle_seconds,
        )
    )


def command_system_recover(args: argparse.Namespace) -> None:
    _json(recover_firmware(Path(args.firmware)))


def command_system_state_clear(args: argparse.Namespace) -> None:
    _json(
        clear_state(
            Path(args.firmware),
            acknowledgement=args.acknowledge,
        )
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cmpunlock",
        description="Offline-first CMP 170HX GA100 firmware research tooling",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    profiles = subcommands.add_parser("profile", help="inspect immutable compatibility profiles")
    profile_commands = profiles.add_subparsers(dest="profile_command", required=True)
    profile_list = profile_commands.add_parser("list", help="list bundled profiles")
    profile_list.set_defaults(func=command_profile_list)
    profile_show = profile_commands.add_parser("show", help="show a bundled or JSON profile")
    profile_show.add_argument("profile")
    profile_show.set_defaults(func=command_profile_show)

    firmware = subcommands.add_parser("firmware", help="inspect or patch GSP firmware offline")
    firmware_commands = firmware.add_subparsers(dest="firmware_command", required=True)
    inspect = firmware_commands.add_parser("inspect", help="validate an ELF firmware image")
    inspect.add_argument("input")
    inspect.add_argument("--profile")
    inspect.set_defaults(func=command_firmware_inspect)
    patch = firmware_commands.add_parser("patch", help="write a patched image to a new file")
    patch.add_argument("input")
    patch.add_argument("output")
    patch.add_argument("--profile")
    patch.add_argument("--mode", choices=("proof", "compute"), default="compute")
    patch.add_argument("--force", action="store_true")
    patch.set_defaults(func=command_firmware_patch)

    payload = subcommands.add_parser("payload", help="build a raw DMEM payload")
    payload_commands = payload.add_subparsers(dest="payload_command", required=True)
    payload_build = payload_commands.add_parser("build")
    payload_build.add_argument("output")
    payload_build.add_argument("--profile", required=True)
    payload_build.add_argument("--mode", choices=("proof", "compute"), default="compute")
    payload_build.add_argument("--force", action="store_true")
    payload_build.set_defaults(func=command_payload_build)

    system = subcommands.add_parser(
        "system", help="live-host checks, gated execution, and recovery"
    )
    system_commands = system.add_subparsers(dest="system_command", required=True)
    for name, help_text, func in (
        ("inspect", "verify target, firmware, module, and embedded booter", command_system_inspect),
        ("plan", "render the blocked experimental transaction", command_system_plan),
    ):
        command = system_commands.add_parser(name, help=help_text)
        command.add_argument("bdf")
        command.add_argument("--firmware", required=True)
        command.add_argument("--profile")
        command.set_defaults(func=func)

    apply = system_commands.add_parser(
        "apply",
        help="gated experimental execution; unavailable without explicit risk acknowledgement",
    )
    apply.add_argument("bdf")
    apply.add_argument("--firmware", required=True)
    apply.add_argument("--profile")
    apply.add_argument("--execute", action="store_true")
    apply.add_argument("--acknowledge", default="")
    apply.add_argument("--settle-seconds", type=float, default=5.0)
    apply.set_defaults(func=command_system_apply)

    recover = system_commands.add_parser(
        "recover", help="restore verified stock firmware from a journal"
    )
    recover.add_argument("--firmware", required=True)
    recover.set_defaults(func=command_system_recover)

    state_clear = system_commands.add_parser(
        "state-clear",
        help="clear an audit record after a confirmed complete cold power cycle",
    )
    state_clear.add_argument("--firmware", required=True)
    state_clear.add_argument("--acknowledge", default="")
    state_clear.set_defaults(func=command_system_state_clear)
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except KeyboardInterrupt as exc:
        print("error: interrupted", file=sys.stderr)
        raise SystemExit(130) from exc
    except (CmpUnlockError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
