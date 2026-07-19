#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "${SCRIPT_DIR}/lib.sh"

DRY_RUN=0
ACK=""
CONFIRM_COLD_CYCLE=0

usage() {
    cat <<EOF
Usage: sudo ./remove.sh [--dry-run]
       --acknowledge ${REMOVE_ACKNOWLEDGEMENT}

After shutdown, removal of AC power, and a subsequent cold start:
  sudo ./remove.sh --confirm-cold-cycle \
       --acknowledge ${COLD_CYCLE_CONFIRMATION}

The first command atomically archives the isolated module directory and
rebuilds module metadata/initramfs. It deliberately does not hot-unload NVIDIA
modules. Removal remains pending until the second command verifies a new boot,
the recorded stock predecessor, and the native 8192 MiB capacity.
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run) DRY_RUN=1; shift ;;
        --confirm-cold-cycle) CONFIRM_COLD_CYCLE=1; shift ;;
        --acknowledge) [[ $# -ge 2 ]] || die "--acknowledge needs a value"; ACK="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown argument: $1" ;;
    esac
done

require_linux_x86_64
require_manifest
require_command readlink
require_command sha256sum
require_command stat
require_root

KVER="$(uname -r)"
MODULE_ROOT="$(readlink -f -- "/lib/modules/${KVER}")" || die "kernel module root is missing"
TARGET="${MODULE_ROOT}/updates/cmpunlocker-610-memory"
STATE_ROOT="/var/lib/${PROJECT_ID}"
STATE_DIR="${STATE_ROOT}/${KVER}"
STATE_FILE="${STATE_DIR}/install.env"
BACKUP_ROOT="${STATE_ROOT}/backups/${KVER}"
ARCHIVE_ROOT="${STATE_ROOT}/archives/${KVER}"

acquire_operation_lock "${STATE_ROOT}" 0

case "${TARGET}" in
    "${MODULE_ROOT}"/updates/cmpunlocker-610-memory) ;;
    *) die "refusing unexpected module target: ${TARGET}" ;;
esac

if [[ "${CONFIRM_COLD_CYCLE}" -eq 1 ]]; then
    require_command modprobe
    require_command modinfo
    require_command sync
    require_root_owned_state_directory "${STATE_ROOT}"
    require_root_owned_state_directory "${STATE_DIR}"
    require_root_owned_state_directory "${ARCHIVE_ROOT}"
    require_same_filesystem "${STATE_DIR}" "${ARCHIVE_ROOT}"
    shopt -s nullglob
    PENDING_FILES=("${STATE_DIR}"/remove-pending-cold-cycle.*.env)
    shopt -u nullglob
    [[ "${#PENDING_FILES[@]}" -eq 1 ]] || \
        die "expected exactly one pending cold-cycle record; found ${#PENDING_FILES[@]}"
    PENDING_FILE="${PENDING_FILES[0]}"
    verify_install_state_identity "${PENDING_FILE}" "${KVER}" "${TARGET}"
    [[ "$(read_kv "${PENDING_FILE}" removal_status)" == "pending_cold_cycle" ]] || \
        die "pending record has an unexpected removal status"

    REMOVAL_BOOT_ID="$(read_kv "${PENDING_FILE}" removal_boot_id)"
    [[ -r /proc/sys/kernel/random/boot_id ]] || die "current boot id is unavailable"
    CURRENT_BOOT_ID="$(< /proc/sys/kernel/random/boot_id)"
    [[ "${CURRENT_BOOT_ID}" != "${REMOVAL_BOOT_ID}" ]] || \
        die "the host has not booted since removal; perform a full AC power cycle"

    PREVIOUS_MODULE="$(read_kv "${PENDING_FILE}" previous_module)"
    PREVIOUS_MODULE_SHA256="$(read_kv "${PENDING_FILE}" previous_module_sha256)"
    require_hex_sha256 "${PREVIOUS_MODULE_SHA256}" "recorded predecessor module"
    require_stock_module_path "${PREVIOUS_MODULE}" "${MODULE_ROOT}" "${TARGET}"
    RESOLVED="$(resolve_nvidia_module)" || die "cannot resolve the restored stock nvidia module"
    [[ "${RESOLVED}" == "${PREVIOUS_MODULE}" ]] || \
        die "nvidia resolves to ${RESOLVED}, not recorded predecessor ${PREVIOUS_MODULE}"
    [[ "$(sha256_file "${RESOLVED}")" == "${PREVIOUS_MODULE_SHA256}" ]] || \
        die "restored predecessor module hash no longer matches the install record"
    verify_resolved_stock_module_set "${PENDING_FILE}" "${MODULE_ROOT}" "${TARGET}"
    require_loaded_recorded_core_match "${PENDING_FILE}"
    if [[ ! -d /sys/module/nvidia_uvm ]]; then
        info "Loading the already-verified stock nvidia_uvm module for restoration checks"
        modprobe nvidia_uvm || die "could not load the verified stock nvidia_uvm module"
    fi
    require_loaded_recorded_core_and_uvm_match "${PENDING_FILE}"

    REMOVED_ARCHIVE="$(read_kv "${PENDING_FILE}" removed_archive)"
    case "${REMOVED_ARCHIVE}" in
        "${ARCHIVE_ROOT}"/removed.*) ;;
        *) die "pending state contains an unsafe removed archive path" ;;
    esac
    [[ "$(readlink -f -- "${REMOVED_ARCHIVE}")" == "${REMOVED_ARCHIVE}" ]] || \
        die "pending removed archive path is not canonical"
    [[ -d "${REMOVED_ARCHIVE}" && ! -L "${REMOVED_ARCHIVE}" ]] || \
        die "the archived experimental module directory is missing"
    [[ -f "${REMOVED_ARCHIVE}/.cmpunlocker-610-memory" ]] || \
        die "the removed archive lacks its ownership marker"
    [[ ! -e "${TARGET}" && ! -L "${TARGET}" ]] || \
        die "the isolated experiment target exists again"
    ARCHIVED_MODULE_SHA256="$(read_kv "${PENDING_FILE}" nvidia_module_sha256)"
    require_hex_sha256 "${ARCHIVED_MODULE_SHA256}" "recorded archived module"
    [[ -f "${REMOVED_ARCHIVE}/nvidia.ko" && \
       "$(sha256_file "${REMOVED_ARCHIVE}/nvidia.ko")" == "${ARCHIVED_MODULE_SHA256}" ]] || \
        die "the archived experimental nvidia.ko changed after removal"
    ARCHIVED_CHECKSUMS_SHA256="$(read_kv "${PENDING_FILE}" module_checksums_sha256)"
    require_hex_sha256 "${ARCHIVED_CHECKSUMS_SHA256}" "recorded archived checksums"
    [[ -f "${REMOVED_ARCHIVE}/checksums.sha256" && \
       "$(sha256_file "${REMOVED_ARCHIVE}/checksums.sha256")" == "${ARCHIVED_CHECKSUMS_SHA256}" ]] || \
        die "the archived experimental checksum manifest changed after removal"
    verify_artifact "${REMOVED_ARCHIVE}"
    verify_installed_tree_permissions "${REMOVED_ARCHIVE}"

    BDF="$(discover_single_cmp_bdf)"
    require_secure_boot_disabled
    require_installed_stack "${BDF}"
    require_native_memory_total "${BDF}"

    info "New boot id and the exact recorded stock predecessor are active"
    info "nvidia-smi reports the native 8192 MiB capacity"
    if [[ "${DRY_RUN}" -eq 1 ]]; then
        info "DRY RUN: cold-cycle confirmation gates passed; pending state was not archived"
        exit 0
    fi
    require_acknowledgement "${ACK}" "${COLD_CYCLE_CONFIRMATION}"
    CONFIRMED="${ARCHIVE_ROOT}/cold-cycle-confirmed.$(date -u +%Y%m%dT%H%M%SZ).$$.env"
    [[ ! -e "${CONFIRMED}" && ! -L "${CONFIRMED}" ]] || die "confirmation archive collision"
    mv -- "${PENDING_FILE}" "${CONFIRMED}"
    sync -f -- "${CONFIRMED}"
    sync -f -- "${STATE_DIR}"
    sync -f -- "${ARCHIVE_ROOT}"
    info "Cold-cycle acknowledgement and verified native state recorded: ${CONFIRMED}"
    exit 0
fi

require_command depmod
require_command modprobe
require_command modinfo
require_command cp
require_command sync
require_command mktemp

require_root_owned_state_directory "${STATE_ROOT}"
require_root_owned_state_directory "${STATE_DIR}"
require_root_owned_state_directory "${BACKUP_ROOT}"
require_root_owned_state_directory "${ARCHIVE_ROOT}"

[[ -d "${TARGET}" && ! -L "${TARGET}" ]] || die "experiment target is not installed: ${TARGET}"
[[ -f "${TARGET}/.cmpunlocker-610-memory" && ! -L "${TARGET}/.cmpunlocker-610-memory" ]] || \
    die "refusing to move a directory without the experiment ownership marker"
verify_artifact "${TARGET}"
verify_installed_tree_permissions "${TARGET}"
verify_install_state_identity "${STATE_FILE}" "${KVER}" "${TARGET}"

RECORDED_MODULE_SHA256="$(read_kv "${STATE_FILE}" nvidia_module_sha256)"
require_hex_sha256 "${RECORDED_MODULE_SHA256}" "recorded installed module"
[[ "$(sha256_file "${TARGET}/nvidia.ko")" == "${RECORDED_MODULE_SHA256}" ]] || \
    die "installed nvidia.ko no longer matches trusted state"
RECORDED_CHECKSUMS_SHA256="$(read_kv "${STATE_FILE}" module_checksums_sha256)"
require_hex_sha256 "${RECORDED_CHECKSUMS_SHA256}" "recorded module checksum manifest"
[[ "$(sha256_file "${TARGET}/checksums.sha256")" == "${RECORDED_CHECKSUMS_SHA256}" ]] || \
    die "installed module checksum manifest no longer matches trusted state"
verify_resolved_experiment_module_set "${TARGET}"
PREVIOUS_MODULE="$(read_kv "${STATE_FILE}" previous_module)"
PREVIOUS_MODULE_SHA256="$(read_kv "${STATE_FILE}" previous_module_sha256)"
require_hex_sha256 "${PREVIOUS_MODULE_SHA256}" "recorded predecessor module"
require_stock_module_path "${PREVIOUS_MODULE}" "${MODULE_ROOT}" "${TARGET}"
[[ "$(sha256_file "${PREVIOUS_MODULE}")" == "${PREVIOUS_MODULE_SHA256}" ]] || \
    die "recorded stock predecessor changed since installation"
verify_recorded_stock_module_files "${STATE_FILE}" "${MODULE_ROOT}" "${TARGET}"

[[ -d "${ARCHIVE_ROOT}" && ! -L "${ARCHIVE_ROOT}" ]] || \
    die "trusted archive directory is missing or unsafe: ${ARCHIVE_ROOT}"
require_same_filesystem "$(dirname -- "${TARGET}")" "${ARCHIVE_ROOT}"

INITRAMFS_TOOL="$(select_initramfs_tool)" || \
    die "need update-initramfs+lsinitramfs or dracut+lsinitrd"
INITRAMFS_IMAGE="$(initramfs_image "${INITRAMFS_TOOL}" "${KVER}")" || \
    die "the exact non-symlink initramfs for ${KVER} is unavailable"
[[ "$(read_kv "${STATE_FILE}" initramfs_tool)" == "${INITRAMFS_TOOL}" ]] || \
    die "initramfs tooling changed since installation"
[[ "$(read_kv "${STATE_FILE}" initramfs_image)" == "${INITRAMFS_IMAGE}" ]] || \
    die "initramfs image path changed since installation"

info "Installed experiment: ${TARGET}"
info "Recorded stock predecessor: ${PREVIOUS_MODULE}"
info "Initramfs: ${INITRAMFS_IMAGE} (${INITRAMFS_TOOL})"

if [[ "${DRY_RUN}" -eq 1 ]]; then
    info "DRY RUN: coherent locked removal gates passed; no module, install-state, or boot metadata was changed"
    exit 0
fi

require_acknowledgement "${ACK}" "${REMOVE_ACKNOWLEDGEMENT}"
umask 077
for directory in "${STATE_ROOT}" "${STATE_DIR}" "${BACKUP_ROOT}" "${ARCHIVE_ROOT}"; do
    if [[ -e "${directory}" || -L "${directory}" ]]; then
        [[ -d "${directory}" && ! -L "${directory}" ]] || die "unsafe state directory: ${directory}"
    fi
done
mkdir -p -- "${BACKUP_ROOT}" "${ARCHIVE_ROOT}"
for directory in "${STATE_ROOT}" "${STATE_DIR}" "${BACKUP_ROOT}" "${ARCHIVE_ROOT}"; do
    [[ -d "${directory}" && ! -L "${directory}" ]] || die "unsafe state directory: ${directory}"
done
chmod 0700 -- "${STATE_ROOT}" "${STATE_DIR}" "${BACKUP_ROOT}" "${ARCHIVE_ROOT}"
require_root_owned_state_directory "${STATE_ROOT}"
require_root_owned_state_directory "${STATE_DIR}"
require_root_owned_state_directory "${BACKUP_ROOT}"
require_root_owned_state_directory "${ARCHIVE_ROOT}"
require_same_filesystem "$(dirname -- "${TARGET}")" "${ARCHIVE_ROOT}"
require_same_filesystem "${STATE_DIR}" "${ARCHIVE_ROOT}"
shopt -s nullglob
PENDING_RECORDS=("${STATE_DIR}"/remove-pending-cold-cycle.*.env)
shopt -u nullglob
[[ "${#PENDING_RECORDS[@]}" -eq 0 ]] || \
    die "a removal is already pending cold-cycle confirmation"

STAMP="$(date -u +%Y%m%dT%H%M%SZ).$$"
ARCHIVE="${ARCHIVE_ROOT}/removed.${STAMP}"
STATE_ARCHIVE="${ARCHIVE_ROOT}/install-state.${STAMP}.env"
PENDING="${STATE_DIR}/remove-pending-cold-cycle.${STAMP}.env"
PENDING_TMP="${PENDING}.tmp"
FAILED_PENDING="${ARCHIVE_ROOT}/failed-remove-state.${STAMP}.env"
[[ ! -e "${ARCHIVE}" && ! -L "${ARCHIVE}" ]] || die "removal archive collision"

INITRAMFS_BACKUP="$(backup_initramfs "${INITRAMFS_IMAGE}" "${BACKUP_ROOT}" "remove.${STAMP}")" || \
    die "could not create and sync an exact pre-removal initramfs backup"
INITRAMFS_BACKUP_SHA256="$(sha256_file "${INITRAMFS_BACKUP}")"
[[ -r /proc/sys/kernel/random/boot_id ]] || die "current boot id is unavailable"
REMOVAL_BOOT_ID="$(< /proc/sys/kernel/random/boot_id)"

cat -- "${STATE_FILE}" > "${PENDING_TMP}"
cat >> "${PENDING_TMP}" <<EOF
removal_status=pending_cold_cycle
removal_boot_id=${REMOVAL_BOOT_ID}
removed_archive=${ARCHIVE}
removal_initramfs_backup=${INITRAMFS_BACKUP}
removal_initramfs_backup_sha256=${INITRAMFS_BACKUP_SHA256}
EOF
chmod 0600 -- "${PENDING_TMP}"
sync -f -- "${PENDING_TMP}"
sync -f -- "${STATE_DIR}"

ROLLBACK_ARMED=0
rollback_remove() {
    local original_status="$1"
    local rollback_failed=0

    warn "removal exited with status ${original_status}; restoring the installed experiment"
    if [[ -e "${ARCHIVE}" || -L "${ARCHIVE}" ]]; then
        if [[ -e "${TARGET}" || -L "${TARGET}" ]]; then
            warn "cannot restore the archived experiment because its target is occupied"
            rollback_failed=1
        elif ! mv -T -- "${ARCHIVE}" "${TARGET}"; then
            warn "could not restore the archived experiment directory"
            rollback_failed=1
        else
            sync -f -- "$(dirname -- "${TARGET}")" || rollback_failed=1
            sync -f -- "${ARCHIVE_ROOT}" || rollback_failed=1
        fi
    fi
    if ! depmod -a "${KVER}"; then
        warn "depmod failed while restoring experimental module metadata"
        rollback_failed=1
    fi
    if ! restore_initramfs "${INITRAMFS_BACKUP}" "${INITRAMFS_IMAGE}" \
        "${INITRAMFS_BACKUP_SHA256}" "remove.${STAMP}"; then
        warn "could not restore the byte-identical pre-removal initramfs"
        rollback_failed=1
    fi
    if [[ -e "${STATE_ARCHIVE}" || -L "${STATE_ARCHIVE}" ]]; then
        if [[ -e "${STATE_FILE}" || -L "${STATE_FILE}" ]]; then
            warn "cannot restore install state because its path is occupied"
            rollback_failed=1
        elif ! mv -- "${STATE_ARCHIVE}" "${STATE_FILE}"; then
            warn "could not restore active install state"
            rollback_failed=1
        elif ! sync -f -- "${STATE_FILE}"; then
            warn "could not sync the restored active install state"
            rollback_failed=1
        fi
    fi
    if [[ -e "${PENDING}" || -L "${PENDING}" ]]; then
        if ! mv -- "${PENDING}" "${FAILED_PENDING}"; then
            warn "could not archive pending removal state"
            rollback_failed=1
        elif ! sync -f -- "${FAILED_PENDING}"; then
            warn "could not sync the failed pending-removal state"
            rollback_failed=1
        fi
    elif [[ -e "${PENDING_TMP}" || -L "${PENDING_TMP}" ]]; then
        if ! mv -- "${PENDING_TMP}" "${FAILED_PENDING}"; then
            warn "could not archive temporary removal state"
            rollback_failed=1
        elif ! sync -f -- "${FAILED_PENDING}"; then
            warn "could not sync the failed temporary-removal state"
            rollback_failed=1
        fi
    fi
    if ! sync -f -- "${STATE_DIR}"; then
        warn "could not sync the state directory after removal rollback"
        rollback_failed=1
    fi
    if ! sync -f -- "${ARCHIVE_ROOT}"; then
        warn "could not sync the archive directory after removal rollback"
        rollback_failed=1
    fi
    if [[ "${rollback_failed}" -eq 0 ]]; then
        warn "rollback completed; the experiment remains installed"
    else
        warn "ROLLBACK INCOMPLETE: do not reboot; inspect module metadata and ${INITRAMFS_IMAGE}"
    fi
}

on_remove_exit() {
    local status="$1"
    trap - EXIT
    trap '' HUP INT TERM
    if [[ "${ROLLBACK_ARMED}" -eq 1 ]]; then
        rollback_remove "${status}"
        exit 1
    fi
    exit "${status}"
}

trap 'on_remove_exit $?' EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
ROLLBACK_ARMED=1

mv -T -- "${TARGET}" "${ARCHIVE}"
sync -f -- "$(dirname -- "${TARGET}")"
sync -f -- "${ARCHIVE_ROOT}"
depmod -a "${KVER}"
RESOLVED="$(resolve_nvidia_module)"
[[ "${RESOLVED}" == "${PREVIOUS_MODULE}" ]] || \
    die "module resolution is ${RESOLVED}, expected ${PREVIOUS_MODULE}"
[[ "$(sha256_file "${RESOLVED}")" == "${PREVIOUS_MODULE_SHA256}" ]] || \
    die "resolved predecessor hash differs from trusted install state"
verify_resolved_stock_module_set "${STATE_FILE}" "${MODULE_ROOT}" "${TARGET}"
run_initramfs "${INITRAMFS_TOOL}" "${KVER}" "${INITRAMFS_IMAGE}"
verify_initramfs_excludes "${INITRAMFS_TOOL}" "${INITRAMFS_IMAGE}" "${KVER}" || \
    die "rebuilt initramfs still contains isolated experimental modules"
sync -f -- "${INITRAMFS_IMAGE}"

mv -- "${PENDING_TMP}" "${PENDING}"
sync -f -- "${PENDING}"
mv -- "${STATE_FILE}" "${STATE_ARCHIVE}"
sync -f -- "${STATE_ARCHIVE}"
sync -f -- "${STATE_DIR}"
sync -f -- "${ARCHIVE_ROOT}"
ROLLBACK_ARMED=0
trap - EXIT HUP INT TERM

info "Experimental files were archived, not deleted: ${ARCHIVE}"
info "No running module was unloaded"
info "REMOVAL IS PENDING: shut down, remove AC power, wait for board power loss, then cold-start"
info "After that cold cycle, verify and record completion with:"
info "  sudo ${SCRIPT_DIR}/remove.sh --confirm-cold-cycle --acknowledge ${COLD_CYCLE_CONFIRMATION}"
