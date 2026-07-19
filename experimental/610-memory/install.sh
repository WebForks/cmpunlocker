#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "${SCRIPT_DIR}/lib.sh"

DRY_RUN=0
ACK=""
ARTIFACT="${SCRIPT_DIR}/artifacts/$(uname -r)"

usage() {
    cat <<EOF
Usage: sudo ./install.sh [--dry-run] [--artifact DIR]
       --acknowledge ${INSTALL_ACKNOWLEDGEMENT}

Installs a verified artifact into the isolated updates/cmpunlocker-610-memory
directory. It never unloads a module, kills a process, installs userspace, or
claims that expanded memory works. A collision at the isolated target is a
hard failure; remove an earlier installation before reinstalling.
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run) DRY_RUN=1; shift ;;
        --acknowledge) [[ $# -ge 2 ]] || die "--acknowledge needs a value"; ACK="$2"; shift 2 ;;
        --artifact) [[ $# -ge 2 ]] || die "--artifact needs a value"; ARTIFACT="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown argument: $1" ;;
    esac
done

require_linux_x86_64
require_manifest
require_command depmod
require_command modprobe
require_command modinfo
require_command readelf
require_command strings
require_command sha256sum
require_command readlink
require_command stat
require_command cp
require_command sync
require_command mktemp
require_root

KVER="$(uname -r)"
MODULE_ROOT="$(readlink -f -- "/lib/modules/${KVER}")" || die "kernel module root is missing"
TARGET_PARENT="${MODULE_ROOT}/updates"
TARGET="${TARGET_PARENT}/cmpunlocker-610-memory"
STATE_ROOT="/var/lib/${PROJECT_ID}"
STATE_DIR="${STATE_ROOT}/${KVER}"
STATE_FILE="${STATE_DIR}/install.env"
BACKUP_ROOT="${STATE_ROOT}/backups/${KVER}"
ARCHIVE_ROOT="${STATE_ROOT}/archives/${KVER}"

# The state root and lock are the only infrastructure this may create before
# preflight. Every state-dependent read and every later mutation is serialized.
acquire_operation_lock "${STATE_ROOT}" 1

case "${TARGET}" in
    "${MODULE_ROOT}"/updates/cmpunlocker-610-memory) ;;
    *) die "refusing unexpected module target: ${TARGET}" ;;
esac
[[ ! -e "${TARGET}" && ! -L "${TARGET}" ]] || \
    die "isolated target already exists; run remove.sh before reinstalling"
[[ ! -e "${STATE_FILE}" && ! -L "${STATE_FILE}" ]] || \
    die "active install state already exists; run remove.sh before reinstalling"
shopt -s nullglob
PENDING_RECORDS=("${STATE_DIR}"/remove-pending-cold-cycle.*.env)
shopt -u nullglob
[[ "${#PENDING_RECORDS[@]}" -eq 0 ]] || \
    die "a removal is still pending its required cold-cycle confirmation"

BDF="$(discover_single_cmp_bdf)"
require_secure_boot_disabled
require_installed_stack "${BDF}"
require_native_memory_total "${BDF}"
ARTIFACT="$(readlink -f -- "${ARTIFACT}")" || die "artifact path does not exist"
verify_artifact "${ARTIFACT}"
verify_artifact_permissions "${ARTIFACT}"
verify_module_metadata "${ARTIFACT}" "${KVER}"
verify_required_module_markers "${ARTIFACT}/nvidia.ko"

PREVIOUS_MODULE="$(resolve_nvidia_module)" || die "cannot resolve the current nvidia module"
require_stock_module_path "${PREVIOUS_MODULE}" "${MODULE_ROOT}" "${TARGET}"
PREVIOUS_MODULE_SHA256="$(sha256_file "${PREVIOUS_MODULE}")"
PREDECESSOR_STATE="$(capture_stock_module_set "${MODULE_ROOT}" "${TARGET}")"
INITRAMFS_TOOL="$(select_initramfs_tool)" || \
    die "need update-initramfs+lsinitramfs or dracut+lsinitrd"
INITRAMFS_IMAGE="$(initramfs_image "${INITRAMFS_TOOL}" "${KVER}")" || \
    die "the exact non-symlink initramfs for ${KVER} is unavailable"

TARGET_FS_PROBE="${MODULE_ROOT}"
if [[ -e "${TARGET_PARENT}" || -L "${TARGET_PARENT}" ]]; then
    [[ -d "${TARGET_PARENT}" && ! -L "${TARGET_PARENT}" ]] || \
        die "unsafe module updates directory: ${TARGET_PARENT}"
    TARGET_FS_PROBE="${TARGET_PARENT}"
fi
ARCHIVE_FS_PROBE="/var/lib"
if [[ -e "${ARCHIVE_ROOT}" || -L "${ARCHIVE_ROOT}" ]]; then
    [[ -d "${ARCHIVE_ROOT}" && ! -L "${ARCHIVE_ROOT}" ]] || \
        die "unsafe archive directory: ${ARCHIVE_ROOT}"
    ARCHIVE_FS_PROBE="${ARCHIVE_ROOT}"
elif [[ -e "${STATE_ROOT}" || -L "${STATE_ROOT}" ]]; then
    [[ -d "${STATE_ROOT}" && ! -L "${STATE_ROOT}" ]] || \
        die "unsafe state root: ${STATE_ROOT}"
    ARCHIVE_FS_PROBE="${STATE_ROOT}"
fi
require_same_filesystem "${TARGET_FS_PROBE}" "${ARCHIVE_FS_PROBE}"

info "Target: ${BDF}"
info "Artifact: ${ARTIFACT}"
info "Install directory: ${TARGET}"
info "Current stock module: ${PREVIOUS_MODULE}"
info "Initramfs: ${INITRAMFS_IMAGE} (${INITRAMFS_TOOL})"

if [[ "${DRY_RUN}" -eq 1 ]]; then
    info "DRY RUN: coherent locked preflight passed; no module, install-state, or boot metadata was changed"
    exit 0
fi

require_acknowledgement "${ACK}" "${INSTALL_ACKNOWLEDGEMENT}"
umask 077

for directory in "${TARGET_PARENT}" "${STATE_ROOT}" "${STATE_DIR}" "${BACKUP_ROOT}" "${ARCHIVE_ROOT}"; do
    if [[ -e "${directory}" || -L "${directory}" ]]; then
        [[ -d "${directory}" && ! -L "${directory}" ]] || die "unsafe state or target directory: ${directory}"
    fi
done
mkdir -p -- "${TARGET_PARENT}" "${STATE_DIR}" "${BACKUP_ROOT}" "${ARCHIVE_ROOT}"
for directory in "${TARGET_PARENT}" "${STATE_ROOT}" "${STATE_DIR}" "${BACKUP_ROOT}" "${ARCHIVE_ROOT}"; do
    [[ -d "${directory}" && ! -L "${directory}" ]] || die "unsafe state or target directory: ${directory}"
done
chmod 0700 -- "${STATE_ROOT}" "${STATE_DIR}" "${BACKUP_ROOT}" "${ARCHIVE_ROOT}"
require_root_owned_state_directory "${STATE_ROOT}"
require_root_owned_state_directory "${STATE_DIR}"
require_root_owned_state_directory "${BACKUP_ROOT}"
require_root_owned_state_directory "${ARCHIVE_ROOT}"
require_same_filesystem "${TARGET_PARENT}" "${ARCHIVE_ROOT}"
require_same_filesystem "${STATE_DIR}" "${ARCHIVE_ROOT}"
[[ ! -e "${STATE_FILE}" && ! -L "${STATE_FILE}" && \
   ! -e "${TARGET}" && ! -L "${TARGET}" ]] || \
    die "install target/state appeared during preflight"
shopt -s nullglob
PENDING_RECORDS=("${STATE_DIR}"/remove-pending-cold-cycle.*.env)
shopt -u nullglob
[[ "${#PENDING_RECORDS[@]}" -eq 0 ]] || \
    die "a removal became pending during install preflight"

STAMP="$(date -u +%Y%m%dT%H%M%SZ).$$"
STAGE="${ARCHIVE_ROOT}/install-stage.${STAMP}"
FAILED_TARGET="${ARCHIVE_ROOT}/failed-install.${STAMP}"
STATE_TMP="${STATE_DIR}/install.env.tmp.${STAMP}"
FAILED_STATE="${ARCHIVE_ROOT}/failed-install-state.${STAMP}.env"
[[ ! -e "${STAGE}" && ! -L "${STAGE}" ]] || die "staging path already exists"
[[ ! -e "${FAILED_TARGET}" && ! -L "${FAILED_TARGET}" ]] || die "failure archive already exists"

install -d -m 0755 -- "${STAGE}"
while IFS= read -r module; do
    install -m 0644 -- "${ARTIFACT}/${module}" "${STAGE}/${module}"
done < <(module_filenames)
install -m 0644 -- "${ARTIFACT}/artifact.env" "${STAGE}/artifact.env"
install -m 0644 -- "${ARTIFACT}/checksums.sha256" "${STAGE}/checksums.sha256"
cat > "${STAGE}/.cmpunlocker-610-memory" <<EOF
project_id=${PROJECT_ID}
kernel_release=${KVER}
source_commit=${NVIDIA_SOURCE_COMMIT}
patch_sha256=${PATCH_SHA256}
memory_capacity_verified=false
EOF
chmod 0644 -- "${STAGE}/.cmpunlocker-610-memory"
verify_artifact "${STAGE}"
verify_installed_tree_permissions "${STAGE}"
verify_module_metadata "${STAGE}" "${KVER}"
verify_required_module_markers "${STAGE}/nvidia.ko"
sync -f -- "${STAGE}"
sync -f -- "${ARCHIVE_ROOT}"

INITRAMFS_BACKUP="$(backup_initramfs "${INITRAMFS_IMAGE}" "${BACKUP_ROOT}" "${STAMP}")" || \
    die "could not create and sync an exact initramfs backup"
INITRAMFS_BACKUP_SHA256="$(sha256_file "${INITRAMFS_BACKUP}")"
NVIDIA_MODULE_SHA256="$(sha256_file "${STAGE}/nvidia.ko")"
MODULE_CHECKSUMS_SHA256="$(sha256_file "${STAGE}/checksums.sha256")"
[[ -r /proc/sys/kernel/random/boot_id ]] || die "current boot id is unavailable"
INSTALL_BOOT_ID="$(< /proc/sys/kernel/random/boot_id)"

cat > "${STATE_TMP}" <<EOF
schema=1
project_id=${PROJECT_ID}
kernel_release=${KVER}
target_dir=${TARGET}
previous_module=${PREVIOUS_MODULE}
previous_module_sha256=${PREVIOUS_MODULE_SHA256}
${PREDECESSOR_STATE}
source_commit=${NVIDIA_SOURCE_COMMIT}
patch_sha256=${PATCH_SHA256}
nvidia_module_sha256=${NVIDIA_MODULE_SHA256}
module_checksums_sha256=${MODULE_CHECKSUMS_SHA256}
initramfs_tool=${INITRAMFS_TOOL}
initramfs_image=${INITRAMFS_IMAGE}
install_initramfs_backup=${INITRAMFS_BACKUP}
install_initramfs_backup_sha256=${INITRAMFS_BACKUP_SHA256}
install_boot_id=${INSTALL_BOOT_ID}
memory_capacity_verified=false
EOF
chmod 0600 -- "${STATE_TMP}"
sync -f -- "${STATE_TMP}"
sync -f -- "${STATE_DIR}"

ROLLBACK_ARMED=0
rollback_install() {
    local original_status="$1"
    local rollback_failed=0

    warn "installation exited with status ${original_status}; restoring pre-install state"
    if [[ -e "${TARGET}" || -L "${TARGET}" ]]; then
        if [[ ! -f "${TARGET}/.cmpunlocker-610-memory" || \
              ! -f "${TARGET}/nvidia.ko" || \
              "$(sha256_file "${TARGET}/nvidia.ko")" != "${NVIDIA_MODULE_SHA256}" ]]; then
            warn "refusing to archive an unexpected occupant of the isolated target"
            rollback_failed=1
        elif ! mv -T -- "${TARGET}" "${FAILED_TARGET}"; then
            warn "could not archive the installed target during rollback"
            rollback_failed=1
        else
            sync -f -- "${TARGET_PARENT}" || rollback_failed=1
            sync -f -- "${ARCHIVE_ROOT}" || rollback_failed=1
        fi
    fi
    if [[ -e "${STAGE}" || -L "${STAGE}" ]]; then
        if [[ -e "${FAILED_TARGET}" || -L "${FAILED_TARGET}" ]]; then
            warn "cannot preserve the uncommitted install stage because its failure archive is occupied"
            rollback_failed=1
        elif ! mv -T -- "${STAGE}" "${FAILED_TARGET}"; then
            warn "could not preserve the uncommitted install stage during rollback"
            rollback_failed=1
        elif ! sync -f -- "${FAILED_TARGET}"; then
            warn "could not sync the preserved uncommitted install stage"
            rollback_failed=1
        fi
        sync -f -- "${ARCHIVE_ROOT}" || rollback_failed=1
    fi
    if ! depmod -a "${KVER}"; then
        warn "depmod failed while restoring pre-install module metadata"
        rollback_failed=1
    fi
    if ! restore_initramfs "${INITRAMFS_BACKUP}" "${INITRAMFS_IMAGE}" \
        "${INITRAMFS_BACKUP_SHA256}" "${STAMP}"; then
        warn "could not restore the byte-identical pre-install initramfs"
        rollback_failed=1
    fi
    if [[ -e "${STATE_FILE}" || -L "${STATE_FILE}" ]]; then
        if ! mv -- "${STATE_FILE}" "${FAILED_STATE}"; then
            warn "could not archive the uncommitted install state"
            rollback_failed=1
        fi
    elif [[ -e "${STATE_TMP}" || -L "${STATE_TMP}" ]]; then
        if ! mv -- "${STATE_TMP}" "${FAILED_STATE}"; then
            warn "could not archive the temporary install state"
            rollback_failed=1
        fi
    fi
    sync -f -- "${STATE_DIR}" || rollback_failed=1
    sync -f -- "${ARCHIVE_ROOT}" || rollback_failed=1
    if [[ "${rollback_failed}" -eq 0 ]]; then
        warn "rollback completed; failed installed files, if any, are at ${FAILED_TARGET}"
    else
        warn "ROLLBACK INCOMPLETE: do not reboot; inspect module metadata and ${INITRAMFS_IMAGE}"
    fi
}

on_install_exit() {
    local status="$1"
    trap - EXIT
    trap '' HUP INT TERM
    if [[ "${ROLLBACK_ARMED}" -eq 1 ]]; then
        rollback_install "${status}"
        exit 1
    fi
    exit "${status}"
}

trap 'on_install_exit $?' EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
ROLLBACK_ARMED=1

mv -T -- "${STAGE}" "${TARGET}"
sync -f -- "${ARCHIVE_ROOT}"
sync -f -- "${TARGET_PARENT}"
depmod -a "${KVER}"
RESOLVED="$(resolve_nvidia_module)"
[[ "${RESOLVED}" == "${TARGET}/nvidia.ko" ]] || \
    die "modprobe resolves ${RESOLVED}, not the isolated target"
[[ "$(sha256_file "${RESOLVED}")" == "${NVIDIA_MODULE_SHA256}" ]] || \
    die "resolved nvidia module does not match the installed artifact"
verify_resolved_experiment_module_set "${TARGET}"
run_initramfs "${INITRAMFS_TOOL}" "${KVER}" "${INITRAMFS_IMAGE}"
verify_initramfs_selection "${INITRAMFS_TOOL}" "${INITRAMFS_IMAGE}" "${KVER}" || \
    die "rebuilt initramfs contains a competing or duplicate NVIDIA module"
sync -f -- "${INITRAMFS_IMAGE}"

mv -- "${STATE_TMP}" "${STATE_FILE}"
sync -f -- "${STATE_FILE}"
sync -f -- "${STATE_DIR}"
ROLLBACK_ARMED=0
trap - EXIT HUP INT TERM

info "Installed hash-verified modules without touching the running driver"
info "Perform a full AC power-off/cold start before attempting the fixed validator"
info "Memory capacity remains UNVERIFIED until validate.sh completes every fixed stage"
