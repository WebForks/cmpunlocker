# SPDX-License-Identifier: GPL-2.0-only
from __future__ import annotations

import os
import random
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "experimental" / "610-memory" / "tools"
CUDA_SOURCE = TOOLS / "memory-pattern-test.cu"
VALIDATOR = TOOLS / "validate-memory.sh"
VALIDATE_WRAPPER = ROOT / "experimental" / "610-memory" / "validate.sh"
MASK64 = (1 << 64) - 1
MIX1 = 0xBF58476D1CE4E5B9
MIX2 = 0x94D049BB133111EB


def _xor_right(value: int, shift: int) -> int:
    return (value ^ (value >> shift)) & MASK64


def _undo_xor_right(value: int, shift: int) -> int:
    result = value
    distance = shift
    while distance < 64:
        result ^= value >> distance
        distance += shift
    return result & MASK64


def _pattern(word_index: int, seed: int, inverted: bool = False) -> int:
    value = (word_index ^ seed) & MASK64
    value = _xor_right(value, 30)
    value = (value * MIX1) & MASK64
    value = _xor_right(value, 27)
    value = (value * MIX2) & MASK64
    value = _xor_right(value, 31)
    return (~value & MASK64) if inverted else value


def _unpattern(value: int, seed: int, inverted: bool = False) -> int:
    if inverted:
        value = ~value & MASK64
    value = _undo_xor_right(value, 31)
    value = (value * pow(MIX2, -1, 1 << 64)) & MASK64
    value = _undo_xor_right(value, 27)
    value = (value * pow(MIX1, -1, 1 << 64)) & MASK64
    value = _undo_xor_right(value, 30)
    return (value ^ seed) & MASK64


def test_cuda_memory_tester_has_full_range_address_pattern_and_failure_contract() -> None:
    source = CUDA_SOURCE.read_text(encoding="utf-8")

    assert "static_cast<std::uint64_t>(index), seed, invert_output" in source
    assert "for (std::size_t index = start; index < word_count; index += stride)" in source
    assert "cudaMalloc(&memory, requested_bytes)" in source
    assert "atomicAdd(mismatch_count, block_mismatches[0])" in source
    assert "unsigned long long local_mismatches = 0" in source
    assert "sample_word=" in source
    assert "--stage-gib" in source
    assert "--passes" in source
    assert "--pci-bdf" in source
    assert "--device" not in source
    assert "verify_sysfs_target" in source
    assert "I-ACCEPT-UNVERIFIED-610-MEMORY-STRESS-AND-CONFIRM-FORCED-AIRFLOW" in source
    assert "unsigned int passes = 5" in source
    assert "~base_seed" in source
    assert "const bool inverted[] = {false, true, false}" in source
    assert 'return 2;' in source
    assert 'VRAM_VERIFY_RESULT status=PASS mismatch_count=0' in source


def test_pattern_mirror_is_bijective_at_stage_boundaries_and_random_indices() -> None:
    random_generator = random.Random(0x20C2)
    indices = {
        0,
        1,
        (8 << 30) // 8 - 1,
        (60 << 30) // 8 - 1,
        (1024 << 30) // 8 - 1,
    }
    indices.update(random_generator.randrange(0, (60 << 30) // 8) for _ in range(500))

    for seed in (0, MASK64, 0xD1B54A32D192ED03, 0x6A09E667F3BCC909):
        for inverted in (False, True):
            outputs = {_pattern(index, seed, inverted) for index in indices}
            assert len(outputs) == len(indices)
            for index in indices:
                assert _unpattern(_pattern(index, seed, inverted), seed, inverted) == index


@pytest.mark.parametrize("stage_gib", [1, 60, 1024])
def test_grid_stride_partition_arithmetic_covers_exact_word_count(stage_gib: int) -> None:
    word_count = (stage_gib << 30) // 8
    thread_count = min(65535 * 256, word_count)
    quotient, remainder = divmod(word_count, thread_count)
    visits = remainder * (quotient + 1) + (thread_count - remainder) * quotient

    assert visits == word_count
    last_index = word_count - 1
    start = last_index % thread_count
    step = last_index // thread_count
    assert start + step * thread_count == last_index


def test_validator_is_fixed_target_fail_closed_and_emits_ready_only_at_end() -> None:
    source = VALIDATOR.read_text(encoding="utf-8")

    assert "readonly -a STAGES_GIB=(8 16 32 48 60)" in source
    assert "I-ACCEPT-UNVERIFIED-610-MEMORY-STRESS-AND-CONFIRM-FORCED-AIRFLOW" in source
    assert "EXPECTED_DRIVER_VERSION='610.43.03'" in source
    assert "${device,,} == 0x20c2" in source
    assert "--module-sha256" in source
    assert "Open Kernel Module" in source
    assert "GSP_FIRMWARE_VERSION" in source
    assert "loaded_gsp_firmware_version=" in source
    assert "memory-pattern-test" in source
    assert "--tester" not in source
    assert "cmpunlock610: gate-active device=10de:20c2" in source
    assert "cmpunlock610: plm-ok name=" in source
    assert "cmpunlock610: host-ok name=" in source
    assert "cmpunlock610: metadata-ok bytes=0x0000001000000000" in source
    assert "cmpunlock610: pma-ok" in source
    assert "cmpunlock610: fail " in source
    assert "COLD_POWER_CYCLE_REQUIRED" in source
    assert "EXPECTED_REPORTED_MIB=65536" in source
    assert "smi_memory_mib == EXPECTED_REPORTED_MIB" in source
    assert "PASSES=5" in source
    assert "MAX_TEMPERATURE_C=75" in source
    assert "default stop threshold is 75 C" in source
    assert "monitor_stage_temperature" in source
    assert "TEMPERATURE_LIMIT_EXCEEDED" in source
    assert "terminate_process_bounded" in source
    assert '"$tester_pid" "$tester_starttime"' in source
    assert "kill -TERM \"$pid\"" in source
    assert "kill -KILL \"$pid\"" in source
    assert "PROCESS_STILL_LIVE" in source
    assert "--kill-after=2s" in source
    assert "process_is_live" in source
    assert "process_starttime" in source
    assert "ACTIVE_TESTER_STARTTIME" in source
    assert "ACTIVE_MONITOR_STARTTIME" in source
    assert "ACTIVE_TEE_STARTTIME" in source
    assert "^State:[[:space:]]*Z" in source
    assert "if ! printf 'TEMPERATURE_SAMPLE" in source
    assert "log root must be owned by root" in source
    assert "log root must be canonical and contain no symlink component" in source
    assert "full validation requires at least 3 passes" in source
    assert "not a lifetime-reliability guarantee" in source
    assert "EXPECTED_PROJECT_ID='cmpunlocker-610-memory'" in source
    assert "EXPECTED_SOURCE_COMMIT='452cec62d827034798072827d3866d1881662b77'" in source
    assert "EXPECTED_PATCH_SHA256='f377efcb000035449a4520c3f306d0983c4de9b3dbe8a71f2ee616a5c0571c6b'" in source
    assert "EXPECTED_GSP_SHA256='73065619db9ec921d19fc4e519dd04d91a9199b525eaca9b257b89fb8c5ec52c'" in source
    assert "recorded_gsp_path=" in source
    assert "artifact-recorded GSP path is not canonical" in source
    assert "install-state.env" in source
    assert "installed-artifact.env" in source
    assert "installed-marker.env" in source
    assert "module-checksums.sha256" in source
    assert "module-provenance.tsv" in source
    assert "nvidia_smi_version=" in source
    assert "nvml_version=" in source
    assert "cold_cycle_acknowledgement=" in source
    assert "--cold-cycle-acknowledge" in source
    assert "--operation-lock-fd" in source
    assert "operation_lock_identity=" in source
    assert 'flock --exclusive --nonblock "$OPERATION_LOCK_FD"' in source
    assert "mismatch_count=0" in source
    assert "free_bytes=[0-9]+ total_bytes=[0-9]+" in source
    assert "pattern_ok_count == PASSES * 3" in source
    assert source.count("printf 'LLM_READY\\n'") == 1
    assert source.rfind("printf 'LLM_READY\\n'") > source.rfind("for stage_gib")
    stage_loop = source.index('for stage_gib in "${STAGES_GIB[@]}"')
    monitor_wait = source.index('wait "$ACTIVE_MONITOR_PID"', stage_loop)
    tester_wait = source.index('wait "$ACTIVE_TESTER_PID"', stage_loop)
    assert monitor_wait < tester_wait
    assert "PCIe" not in source


def test_wrapper_verifies_loaded_core_before_controlled_uvm_load() -> None:
    source = VALIDATE_WRAPPER.read_text(encoding="utf-8")

    lock = source.index('acquire_operation_lock "${STATE_ROOT}" 0')
    core_check = source.index('require_loaded_core_match "${TARGET}"')
    uvm_load = source.index("modprobe nvidia_uvm")
    complete_check = source.index('require_loaded_core_and_uvm_match "${TARGET}"')
    cold_ack_argument = source.index(
        '--cold-cycle-acknowledge "${INSTALL_COLD_CYCLE_CONFIRMATION}"'
    )
    lock_fd_argument = source.index(
        '--operation-lock-fd "${CMPUNLOCKER_OPERATION_LOCK_FD}"'
    )
    delegate = source.index('"${VALIDATOR}" "${ARGS[@]}"')

    assert lock < core_check < uvm_load < complete_check < delegate
    assert complete_check < cold_ack_argument < delegate
    assert complete_check < lock_fd_argument < delegate
    assert 'exec "${VALIDATOR}"' not in source


@pytest.mark.skipif(sys.platform != "linux", reason="Linux flock/FD inheritance contract")
def test_bash_dynamic_lock_fd_is_inherited_and_stays_exclusive(tmp_path: Path) -> None:
    bash = shutil.which("bash")
    flock = shutil.which("flock")
    if bash is None or flock is None:
        pytest.skip("bash or flock is unavailable")

    lock_file = tmp_path / "operation.lock"
    script = r'''
set -euo pipefail
lock_file=$1
exec {lock_fd}>>"$lock_file"
flock --exclusive --nonblock "$lock_fd"
bash -c '
    set -euo pipefail
    lock_fd=$1
    lock_file=$2
    test -e "/proc/self/fd/$lock_fd"
    test "$(stat -Lc "%d:%i" "/proc/self/fd/$lock_fd")" = \
         "$(stat -c "%d:%i" "$lock_file")"
    flock --exclusive --nonblock "$lock_fd"
' child "$lock_fd" "$lock_file"
if flock --exclusive --nonblock "$lock_file" -c true; then
    echo "a second open file description acquired the held lock" >&2
    exit 1
fi
'''
    result = subprocess.run(
        [bash, "-c", script, "lock-test", str(lock_file)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell behavior is tested on POSIX")
def test_validator_help_is_safe_and_shell_syntax_is_valid() -> None:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is unavailable")

    syntax = subprocess.run(
        [bash, "-n", str(VALIDATOR)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert syntax.returncode == 0, syntax.stderr

    help_result = subprocess.run(
        [bash, str(VALIDATOR), "--help"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert help_result.returncode == 0
    assert "8/16/32/48/60-GiB" in help_result.stdout
    assert "LLM_READY" not in help_result.stdout


@pytest.mark.skipif(os.name == "nt", reason="CUDA Linux build is tested on Linux")
def test_cuda_source_links_and_argument_contract_runs_when_nvcc_is_available(tmp_path: Path) -> None:
    nvcc = shutil.which("nvcc")
    if nvcc is None:
        pytest.skip("nvcc is unavailable")

    executable = tmp_path / "memory-pattern-test"
    result = subprocess.run(
        [
            nvcc,
            "-O3",
            "-std=c++17",
            "-arch=sm_80",
            str(CUDA_SOURCE),
            "-o",
            str(executable),
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert result.returncode == 0, result.stderr

    help_result = subprocess.run(
        [str(executable), "--help"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert help_result.returncode == 0
    assert "--pci-bdf" in help_result.stderr

    invalid_result = subprocess.run(
        [str(executable)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert invalid_result.returncode == 64
    assert "a valid --pci-bdf is required" in invalid_result.stderr
