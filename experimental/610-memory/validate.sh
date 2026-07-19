#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "${SCRIPT_DIR}/lib.sh"

ACK=""
COLD_CYCLE_ACK=""
PASSES=""
LOG_ROOT=""
PREFLIGHT_ONLY=0

usage() {
    cat <<EOF
Usage: sudo ./validate.sh --acknowledge ${MEMORY_TEST_ACKNOWLEDGEMENT}
       --cold-cycle-acknowledge ${INSTALL_COLD_CYCLE_CONFIRMATION}
       [--passes N] [--log-root DIR] [--preflight-only]

Delegates only to the fixed repository validator and supplies the exact installed
nvidia.ko SHA-256. No arbitrary test command is accepted.
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --acknowledge) [[ $# -ge 2 ]] || die "--acknowledge needs a value"; ACK="$2"; shift 2 ;;
        --cold-cycle-acknowledge)
            [[ $# -ge 2 ]] || die "--cold-cycle-acknowledge needs a value"
            COLD_CYCLE_ACK="$2"
            shift 2
            ;;
        --passes) [[ $# -ge 2 ]] || die "--passes needs a value"; PASSES="$2"; shift 2 ;;
        --log-root) [[ $# -ge 2 ]] || die "--log-root needs a value"; LOG_ROOT="$2"; shift 2 ;;
        --preflight-only) PREFLIGHT_ONLY=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown argument: $1" ;;
    esac
done

require_linux_x86_64
require_manifest
require_root
require_acknowledgement "${COLD_CYCLE_ACK}" "${INSTALL_COLD_CYCLE_CONFIRMATION}"
if [[ "${PREFLIGHT_ONLY}" -eq 0 ]]; then
    require_acknowledgement "${ACK}" "${MEMORY_TEST_ACKNOWLEDGEMENT}"
fi
require_command sha256sum
require_command readlink
require_command modprobe

KVER="$(uname -r)"
MODULE_ROOT="$(readlink -f -- "/lib/modules/${KVER}")" || die "kernel module root is missing"
TARGET="${MODULE_ROOT}/updates/cmpunlocker-610-memory"
STATE_ROOT="/var/lib/${PROJECT_ID}"
STATE_DIR="${STATE_ROOT}/${KVER}"
STATE_FILE="${STATE_DIR}/install.env"
VALIDATOR="${SCRIPT_DIR}/tools/validate-memory.sh"

# The shell retains this descriptor while the fixed validator and its complete
# stress sequence run, so install/remove/confirmation cannot overlap it.
acquire_operation_lock "${STATE_ROOT}" 0

BDF="$(discover_single_cmp_bdf)"
require_secure_boot_disabled
require_installed_stack "${BDF}"
[[ -f "${VALIDATOR}" && ! -L "${VALIDATOR}" && -x "${VALIDATOR}" ]] || \
    die "fixed validator is missing, symlinked, or not executable: ${VALIDATOR}"
[[ -f "${TARGET}/.cmpunlocker-610-memory" && -f "${TARGET}/nvidia.ko" ]] || \
    die "hash-pinned experiment modules are not installed"
verify_artifact "${TARGET}"
verify_installed_tree_permissions "${TARGET}"
require_root_owned_state_directory "${STATE_ROOT}"
require_root_owned_state_directory "${STATE_DIR}"
verify_install_state_identity "${STATE_FILE}" "${KVER}" "${TARGET}"
[[ -r /proc/sys/kernel/random/boot_id ]] || die "current boot id is unavailable"
INSTALL_BOOT_ID="$(read_kv "${STATE_FILE}" install_boot_id)"
CURRENT_BOOT_ID="$(< /proc/sys/kernel/random/boot_id)"
[[ "${CURRENT_BOOT_ID}" != "${INSTALL_BOOT_ID}" ]] || \
    die "the host has not rebooted since installation; a full AC power cycle is required"
RECORDED_MODULE_SHA256="$(read_kv "${STATE_FILE}" nvidia_module_sha256)"
require_hex_sha256 "${RECORDED_MODULE_SHA256}" "recorded installed module"
CURRENT_MODULE_SHA256="$(sha256_file "${TARGET}/nvidia.ko")"
[[ "${CURRENT_MODULE_SHA256}" == "${RECORDED_MODULE_SHA256}" ]] || \
    die "installed nvidia.ko differs from the trusted installation record"
RECORDED_CHECKSUMS_SHA256="$(read_kv "${STATE_FILE}" module_checksums_sha256)"
require_hex_sha256 "${RECORDED_CHECKSUMS_SHA256}" "recorded module checksum manifest"
[[ "$(sha256_file "${TARGET}/checksums.sha256")" == "${RECORDED_CHECKSUMS_SHA256}" ]] || \
    die "installed module checksum manifest differs from trusted state"
verify_resolved_experiment_module_set "${TARGET}"
require_loaded_core_match "${TARGET}"
if [[ ! -d /sys/module/nvidia_uvm ]]; then
    info "Loading the already-verified experimental nvidia_uvm module for CUDA validation"
    modprobe nvidia_uvm || die "could not load the verified experimental nvidia_uvm module"
fi
require_loaded_core_and_uvm_match "${TARGET}"

shopt -s nullglob
PENDING_RECORDS=("${STATE_DIR}"/remove-pending-cold-cycle.*.env)
shopt -u nullglob
[[ "${#PENDING_RECORDS[@]}" -eq 0 ]] || die "removal is pending; validation is forbidden"

ARGS=(
    --bdf "${BDF}"
    --module-sha256 "${RECORDED_MODULE_SHA256}"
    --operation-lock-fd "${CMPUNLOCKER_OPERATION_LOCK_FD}"
    --cold-cycle-acknowledge "${INSTALL_COLD_CYCLE_CONFIRMATION}"
)
if [[ "${PREFLIGHT_ONLY}" -eq 0 ]]; then
    ARGS+=(--acknowledge "${MEMORY_TEST_ACKNOWLEDGEMENT}")
fi
if [[ -n "${PASSES}" ]]; then
    [[ "${PASSES}" =~ ^[1-9][0-9]*$ ]] || die "--passes must be a positive integer"
    ARGS+=(--passes "${PASSES}")
fi
if [[ -n "${LOG_ROOT}" ]]; then ARGS+=(--log-root "${LOG_ROOT}"); fi
if [[ "${PREFLIGHT_ONLY}" -eq 1 ]]; then ARGS+=(--preflight-only); fi

"${VALIDATOR}" "${ARGS[@]}"
