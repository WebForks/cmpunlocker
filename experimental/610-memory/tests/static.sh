#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

assert_before() {
    local first_pattern="$1"
    local second_pattern="$2"
    local file="$3"
    local first_line second_line

    first_line="$(grep -nF -- "${first_pattern}" "${file}" | head -n 1 | cut -d: -f1)"
    second_line="$(grep -nF -- "${second_pattern}" "${file}" | head -n 1 | cut -d: -f1)"
    [[ -n "${first_line}" && -n "${second_line}" && \
       "${first_line}" -lt "${second_line}" ]] || {
        printf 'required ordering missing in %s: %s before %s\n' \
            "${file}" "${first_pattern}" "${second_pattern}" >&2
        exit 1
    }
}

bash -n \
    "${SCRIPT_DIR}/lib.sh" \
    "${SCRIPT_DIR}/build.sh" \
    "${SCRIPT_DIR}/install.sh" \
    "${SCRIPT_DIR}/remove.sh" \
    "${SCRIPT_DIR}/validate.sh" \
    "${SCRIPT_DIR}/build-validator.sh" \
    "${SCRIPT_DIR}/tools/validate-memory.sh"

shell_files=(
    "${SCRIPT_DIR}/lib.sh"
    "${SCRIPT_DIR}/build.sh"
    "${SCRIPT_DIR}/install.sh"
    "${SCRIPT_DIR}/remove.sh"
    "${SCRIPT_DIR}/validate.sh"
    "${SCRIPT_DIR}/build-validator.sh"
    "${SCRIPT_DIR}/tools/validate-memory.sh"
    "${SCRIPT_DIR}/tests/static.sh"
)

for script in "${shell_files[@]}"; do
    [[ "$(sed -n '2p' "${script}")" == '# SPDX-License-Identifier: GPL-2.0-only' ]]
    if LC_ALL=C grep -q $'\r' "${script}"; then
        printf 'CRLF line ending found: %s\n' "${script}" >&2
        exit 1
    fi
done
[[ "$(sed -n '1p' "${SCRIPT_DIR}/manifest.env")" == \
   '# SPDX-License-Identifier: GPL-2.0-only' ]]

forbidden='(rmmod[[:space:]]+-f|modprobe[[:space:]]+-r|kill(all)?[[:space:]]|apt(-get)?[[:space:]]|dnf[[:space:]]|pacman[[:space:]]|zypper[[:space:]]|NVIDIA-Linux.*\.run)'
if grep -ERn "${forbidden}" \
    "${SCRIPT_DIR}/build.sh" \
    "${SCRIPT_DIR}/install.sh" \
    "${SCRIPT_DIR}/remove.sh" \
    "${SCRIPT_DIR}/validate.sh" \
    "${SCRIPT_DIR}/build-validator.sh" \
    "${SCRIPT_DIR}/lib.sh"; then
    printf 'forbidden operational pattern found\n' >&2
    exit 1
fi

if grep -En 'nvcc .* -- ' "${SCRIPT_DIR}/build-validator.sh"; then
    printf 'nvcc option terminator is unsupported by the required toolchain\n' >&2
    exit 1
fi

(
    # shellcheck source=../lib.sh
    source "${SCRIPT_DIR}/lib.sh"
    require_manifest
    verify_patch_hash
    version_sample=$'NVIDIA-SMI version  : 610.43.03\nNVML version        : 610.43.03\nDRIVER version      : 610.43.03\nGSP Firmware Version: 610.43.03'
    [[ "$(read_version_line "${version_sample}" 'NVIDIA-SMI version')" == '610.43.03' ]]
    [[ "$(read_version_line "${version_sample}" 'NVML version')" == '610.43.03' ]]
    [[ "$(read_version_line "${version_sample}" 'GSP Firmware Version')" == '610.43.03' ]]

    mock_image="$(mktemp)"
    trap 'rm -f -- "$mock_image"' EXIT
    : >"${mock_image}"
    list_initramfs() { printf '%s\n' "${MOCK_INITRAMFS_LIST:-}"; }

    MOCK_INITRAMFS_LIST='usr/lib/modules/mock-kernel/kernel/other.ko'
    verify_initramfs_selection mock "${mock_image}" mock-kernel

    MOCK_INITRAMFS_LIST='usr/lib/modules/mock-kernel/updates/cmpunlocker-610-memory/nvidia.ko'
    verify_initramfs_selection mock "${mock_image}" mock-kernel

    MOCK_INITRAMFS_LIST='usr/lib/modules/mock-kernel/kernel/drivers/video/nvidia.ko'
    if verify_initramfs_selection mock "${mock_image}" mock-kernel 2>/dev/null; then
        printf 'competing initramfs module was accepted\n' >&2
        exit 1
    fi

    MOCK_INITRAMFS_LIST=$'usr/lib/modules/mock-kernel/updates/cmpunlocker-610-memory/nvidia.ko\nusr/lib/modules/mock-kernel/updates/cmpunlocker-610-memory/nvidia.ko'
    if verify_initramfs_selection mock "${mock_image}" mock-kernel 2>/dev/null; then
        printf 'duplicate isolated initramfs module was accepted\n' >&2
        exit 1
    fi
)

grep -Fq 'memory_capacity_verified=false' "${SCRIPT_DIR}/build.sh"
grep -Fq 'memory_capacity_verified=false' "${SCRIPT_DIR}/install.sh"
grep -Fq -- '--dry-run' "${SCRIPT_DIR}/build.sh"
grep -Fq -- '--dry-run' "${SCRIPT_DIR}/install.sh"
grep -Fq -- '--dry-run' "${SCRIPT_DIR}/remove.sh"
grep -Fq 'lock_file="${state_root}/operation.lock"' "${SCRIPT_DIR}/lib.sh"
grep -Fq 'require_secure_operation_lock_file()' "${SCRIPT_DIR}/lib.sh"
grep -Fq 'flock --exclusive --nonblock "${CMPUNLOCKER_OPERATION_LOCK_FD}"' \
    "${SCRIPT_DIR}/lib.sh"
grep -Fq 'operation lock permissions are not 0600' "${SCRIPT_DIR}/lib.sh"
grep -Fq 'acquire_operation_lock "${STATE_ROOT}" 1' "${SCRIPT_DIR}/install.sh"
grep -Fq 'acquire_operation_lock "${STATE_ROOT}" 0' "${SCRIPT_DIR}/remove.sh"
grep -Fq 'acquire_operation_lock "${STATE_ROOT}" 0' "${SCRIPT_DIR}/validate.sh"
assert_before 'acquire_operation_lock "${STATE_ROOT}" 1' \
    'BDF="$(discover_single_cmp_bdf)"' "${SCRIPT_DIR}/install.sh"
assert_before 'acquire_operation_lock "${STATE_ROOT}" 0' \
    'if [[ "${CONFIRM_COLD_CYCLE}" -eq 1 ]]' "${SCRIPT_DIR}/remove.sh"
assert_before 'acquire_operation_lock "${STATE_ROOT}" 0' \
    'BDF="$(discover_single_cmp_bdf)"' "${SCRIPT_DIR}/validate.sh"
assert_before 'acquire_operation_lock "${STATE_ROOT}" 1' \
    'if [[ "${DRY_RUN}" -eq 1 ]]' "${SCRIPT_DIR}/install.sh"
assert_before 'acquire_operation_lock "${STATE_ROOT}" 0' \
    'if [[ "${DRY_RUN}" -eq 1 ]]' "${SCRIPT_DIR}/remove.sh"
grep -Fq '"${VALIDATOR}" "${ARGS[@]}"' "${SCRIPT_DIR}/validate.sh"
if grep -Fq 'exec "${VALIDATOR}"' "${SCRIPT_DIR}/validate.sh"; then
    printf 'validate shell must retain the operation lock during stress\n' >&2
    exit 1
fi
grep -Fq 'restore_initramfs' "${SCRIPT_DIR}/install.sh"
grep -Fq 'restore_initramfs' "${SCRIPT_DIR}/remove.sh"
grep -Fq 'STAGE="${ARCHIVE_ROOT}/install-stage.${STAMP}"' "${SCRIPT_DIR}/install.sh"
grep -Fq 'mv -T -- "${STAGE}" "${TARGET}"' "${SCRIPT_DIR}/install.sh"
grep -Fq 'require_root_owned_state_directory "${ARCHIVE_ROOT}"' "${SCRIPT_DIR}/install.sh"
grep -Fq 'require_same_filesystem "${TARGET_PARENT}" "${ARCHIVE_ROOT}"' "${SCRIPT_DIR}/install.sh"
if grep -Fq 'STAGE="${TARGET_PARENT}/' "${SCRIPT_DIR}/install.sh"; then
    printf 'install stage is under the live module tree\n' >&2
    exit 1
fi
grep -Fq "trap '' HUP INT TERM" "${SCRIPT_DIR}/install.sh"
grep -Fq "trap '' HUP INT TERM" "${SCRIPT_DIR}/remove.sh"
grep -Fq 'verify_initramfs_selection' "${SCRIPT_DIR}/install.sh"
grep -Fq 'verify_initramfs_excludes' "${SCRIPT_DIR}/remove.sh"
grep -Fq 'RECORDED_MODULE_SHA256' "${SCRIPT_DIR}/validate.sh"
grep -Fq 'require_native_memory_total' "${SCRIPT_DIR}/remove.sh"
grep -Fq 'verify_required_module_markers' "${SCRIPT_DIR}/build.sh"
grep -Fq 'verify_resolved_experiment_module_set' "${SCRIPT_DIR}/install.sh"
grep -Fq 'verify_resolved_experiment_module_set' "${SCRIPT_DIR}/validate.sh"
grep -Fq 'verify_resolved_stock_module_set' "${SCRIPT_DIR}/remove.sh"
grep -Fq 'module_checksums_sha256=' "${SCRIPT_DIR}/install.sh"
grep -Fq 'install_boot_id=' "${SCRIPT_DIR}/install.sh"
grep -Fq -- '--cold-cycle-acknowledge' "${SCRIPT_DIR}/validate.sh"
grep -Fq 'require_loaded_core_and_uvm_match' "${SCRIPT_DIR}/validate.sh"
grep -Fq 'require_loaded_core_match' "${SCRIPT_DIR}/validate.sh"
grep -Fq 'require_loaded_recorded_core_match' "${SCRIPT_DIR}/remove.sh"
grep -Fq 'GSP_FIRMWARE_VERSION' "${SCRIPT_DIR}/lib.sh"
grep -Fq 'GSP_FIRMWARE_VERSION' "${SCRIPT_DIR}/tools/validate-memory.sh"
grep -Fq 'modprobe nvidia_uvm' "${SCRIPT_DIR}/validate.sh"
grep -Fq 'modprobe nvidia_uvm' "${SCRIPT_DIR}/remove.sh"
grep -Fq 'monitor_stage_temperature' "${SCRIPT_DIR}/tools/validate-memory.sh"
grep -Fq 'TEMPERATURE_LIMIT_EXCEEDED' "${SCRIPT_DIR}/tools/validate-memory.sh"
grep -Fq 'terminate_process_bounded' "${SCRIPT_DIR}/tools/validate-memory.sh"
grep -Fq 'process_is_live' "${SCRIPT_DIR}/tools/validate-memory.sh"
grep -Fq 'process_starttime' "${SCRIPT_DIR}/tools/validate-memory.sh"
grep -Fq 'ACTIVE_TESTER_STARTTIME' "${SCRIPT_DIR}/tools/validate-memory.sh"
grep -Fq '"$ACTIVE_TESTER_PID" "$ACTIVE_TESTER_STARTTIME"' \
    "${SCRIPT_DIR}/tools/validate-memory.sh"
grep -Fq 'kill -KILL "$pid"' "${SCRIPT_DIR}/tools/validate-memory.sh"
grep -Fq 'PROCESS_STILL_LIVE' "${SCRIPT_DIR}/tools/validate-memory.sh"
grep -Fq "trap '' HUP INT TERM" "${SCRIPT_DIR}/tools/validate-memory.sh"
grep -Fq 'log root must be owned by root' "${SCRIPT_DIR}/tools/validate-memory.sh"
grep -Fq 'install-state.env' "${SCRIPT_DIR}/tools/validate-memory.sh"
grep -Fq 'installed-artifact.env' "${SCRIPT_DIR}/tools/validate-memory.sh"
grep -Fq 'module-provenance.tsv' "${SCRIPT_DIR}/tools/validate-memory.sh"
grep -Fq 'module-checksums.sha256' "${SCRIPT_DIR}/tools/validate-memory.sh"
grep -Fq 'EXPECTED_PATCH_SHA256' "${SCRIPT_DIR}/tools/validate-memory.sh"
grep -Fq 'EXPECTED_GSP_SHA256' "${SCRIPT_DIR}/tools/validate-memory.sh"
grep -Fq 'recorded_gsp_path=' "${SCRIPT_DIR}/tools/validate-memory.sh"
grep -Fq -- '--operation-lock-fd "${CMPUNLOCKER_OPERATION_LOCK_FD}"' \
    "${SCRIPT_DIR}/validate.sh"
grep -Fq 'flock --exclusive --nonblock "$OPERATION_LOCK_FD"' \
    "${SCRIPT_DIR}/tools/validate-memory.sh"
grep -Fq -- '--cold-cycle-acknowledge "${INSTALL_COLD_CYCLE_CONFIRMATION}"' \
    "${SCRIPT_DIR}/validate.sh"
grep -Fq 'sync -f -- "${STATE_FILE}"' "${SCRIPT_DIR}/remove.sh"
grep -Fq 'sync -f -- "${FAILED_PENDING}"' "${SCRIPT_DIR}/remove.sh"
grep -Fq 'sync -f -- "${STATE_DIR}"' "${SCRIPT_DIR}/remove.sh"
grep -Fq 'sync -f -- "${ARCHIVE_ROOT}"' "${SCRIPT_DIR}/remove.sh"

printf 'experimental 610-memory script static checks passed\n'
