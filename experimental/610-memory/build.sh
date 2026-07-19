#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "${SCRIPT_DIR}/lib.sh"

DRY_RUN=0
ACK=""
JOBS="$(nproc 2>/dev/null || printf '1')"
WORK_ROOT="${SCRIPT_DIR}/.work"
OUTPUT="${SCRIPT_DIR}/artifacts/$(uname -r)"

usage() {
    cat <<EOF
Usage: ./build.sh [--dry-run] [--jobs N] [--work-root DIR] [--output DIR]
                  --acknowledge ${BUILD_ACKNOWLEDGEMENT}

Fetches exact NVIDIA source commit ${NVIDIA_SOURCE_COMMIT}, applies the
hash-pinned experimental patch, and creates a kernel-specific module artifact.
It never installs userspace, firmware, or kernel modules.
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run) DRY_RUN=1; shift ;;
        --acknowledge) [[ $# -ge 2 ]] || die "--acknowledge needs a value"; ACK="$2"; shift 2 ;;
        --jobs) [[ $# -ge 2 ]] || die "--jobs needs a value"; JOBS="$2"; shift 2 ;;
        --work-root) [[ $# -ge 2 ]] || die "--work-root needs a value"; WORK_ROOT="$2"; shift 2 ;;
        --output) [[ $# -ge 2 ]] || die "--output needs a value"; OUTPUT="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown argument: $1" ;;
    esac
done

require_linux_x86_64
require_manifest
require_command git
require_command make
require_command sha256sum
require_command modinfo
require_command readelf
require_command stat
require_command strings
[[ "${JOBS}" =~ ^[1-9][0-9]*$ ]] || die "--jobs must be a positive integer"

BDF="$(discover_single_cmp_bdf)"
require_secure_boot_disabled
require_installed_stack "${BDF}"
verify_patch_hash

KVER="$(uname -r)"
KSRC="/lib/modules/${KVER}/build"
[[ -d "${KSRC}" ]] || die "matching kernel headers are missing: ${KSRC}"

info "Target: ${BDF} (the only NVIDIA PCI function)"
info "Driver/GSP: exact ${NVIDIA_DRIVER_VERSION}"
info "Source: ${NVIDIA_SOURCE_REPOSITORY}@${NVIDIA_SOURCE_COMMIT}"
info "Patch SHA-256: ${PATCH_SHA256}"
info "Kernel: ${KVER}"
info "Output: ${OUTPUT}"

if [[ "${DRY_RUN}" -eq 1 ]]; then
    info "DRY RUN: no source was fetched and nothing was built or installed"
    exit 0
fi

require_non_root
require_acknowledgement "${ACK}" "${BUILD_ACKNOWLEDGEMENT}"

mkdir -p -- "${WORK_ROOT}" "$(dirname -- "${OUTPUT}")"
OUTPUT_PARENT="$(dirname -- "${OUTPUT}")"
[[ -d "${WORK_ROOT}" && ! -L "${WORK_ROOT}" ]] || die "unsafe build work root"
[[ "$(stat -c '%u' -- "${WORK_ROOT}")" == "${EUID}" ]] || \
    die "build work root is not owned by the invoking user"
mode_is_not_group_or_world_writable "$(stat -c '%a' -- "${WORK_ROOT}")" || \
    die "build work root is group/world writable"
[[ -d "${OUTPUT_PARENT}" && ! -L "${OUTPUT_PARENT}" ]] || die "unsafe artifact output parent"
[[ "$(stat -c '%u' -- "${OUTPUT_PARENT}")" == "${EUID}" ]] || \
    die "artifact output parent is not owned by the invoking user"
mode_is_not_group_or_world_writable "$(stat -c '%a' -- "${OUTPUT_PARENT}")" || \
    die "artifact output parent is group/world writable"
BUILD_DIR="$(mktemp -d "${WORK_ROOT%/}/build.XXXXXXXX")"
SOURCE_DIR="${BUILD_DIR}/source"
PATCH="$(patch_path)"

info "Fetching the exact source commit into ${SOURCE_DIR}"
git init -q "${SOURCE_DIR}"
git -C "${SOURCE_DIR}" remote add origin "${NVIDIA_SOURCE_REPOSITORY}"
git -C "${SOURCE_DIR}" fetch -q --depth 1 origin "${NVIDIA_SOURCE_COMMIT}"
git -C "${SOURCE_DIR}" checkout -q --detach FETCH_HEAD
[[ "$(git -C "${SOURCE_DIR}" rev-parse HEAD)" == "${NVIDIA_SOURCE_COMMIT}" ]] || \
    die "fetched source commit does not match the manifest"
[[ -z "$(git -C "${SOURCE_DIR}" status --porcelain)" ]] || die "source checkout is not clean"

git -C "${SOURCE_DIR}" apply --check --whitespace=error-all "${PATCH}"
git -C "${SOURCE_DIR}" apply --whitespace=error-all "${PATCH}"
git -C "${SOURCE_DIR}" diff --check
[[ -n "$(git -C "${SOURCE_DIR}" diff --name-only)" ]] || die "patch made no source changes"

info "Building five open NVIDIA modules"
make -C "${SOURCE_DIR}" -j"${JOBS}" modules SYSSRC="${KSRC}"
verify_required_module_markers "${SOURCE_DIR}/kernel-open/nvidia.ko"

STAGE="$(mktemp -d "$(dirname -- "${OUTPUT}")/.artifact.XXXXXXXX")"
while IFS= read -r module; do
    SOURCE_MODULE="${SOURCE_DIR}/kernel-open/${module}"
    [[ -f "${SOURCE_MODULE}" ]] || die "build did not produce ${SOURCE_MODULE}"
    [[ "$(modinfo -F version "${SOURCE_MODULE}")" == "${NVIDIA_DRIVER_VERSION}" ]] || \
        die "${module} reports the wrong driver version"
    modinfo -F vermagic "${SOURCE_MODULE}" | grep -Fq "${KVER}" || \
        die "${module} vermagic does not match ${KVER}"
    readelf -h "${SOURCE_MODULE}" >/dev/null
    install -m 0644 -- "${SOURCE_MODULE}" "${STAGE}/${module}"
done < <(module_filenames)

FIRMWARE="$(find_exact_gsp_firmware)"
cat > "${STAGE}/artifact.env" <<EOF
schema=1
project_id=${PROJECT_ID}
driver_version=${NVIDIA_DRIVER_VERSION}
source_repository=${NVIDIA_SOURCE_REPOSITORY}
source_commit=${NVIDIA_SOURCE_COMMIT}
source_tag=${NVIDIA_SOURCE_TAG}
patch_sha256=${PATCH_SHA256}
kernel_release=${KVER}
target_bdf=${BDF}
gsp_tu10x_path=${FIRMWARE}
gsp_tu10x_sha256=${GSP_TU10X_SHA256}
memory_capacity_verified=false
EOF

(
    cd "${STAGE}"
    while IFS= read -r module; do
        sha256sum -- "${module}"
    done < <(module_filenames)
) > "${STAGE}/checksums.sha256"
chmod 0644 -- "${STAGE}/artifact.env" "${STAGE}/checksums.sha256"
verify_artifact "${STAGE}"
verify_artifact_permissions "${STAGE}"

if [[ -e "${OUTPUT}" || -L "${OUTPUT}" ]]; then
    [[ -d "${OUTPUT}" && ! -L "${OUTPUT}" ]] || die "existing artifact output is not a safe directory"
    [[ "$(stat -c '%u' -- "${OUTPUT}")" == "${EUID}" ]] || \
        die "existing artifact output is not owned by the invoking user"
    mode_is_not_group_or_world_writable "$(stat -c '%a' -- "${OUTPUT}")" || \
        die "existing artifact output is group/world writable"
    [[ -f "${OUTPUT}/artifact.env" && ! -L "${OUTPUT}/artifact.env" ]] || \
        die "refusing to archive an output directory without artifact metadata"
    [[ "$(read_kv "${OUTPUT}/artifact.env" project_id)" == "${PROJECT_ID}" ]] || \
        die "refusing to archive an output directory owned by another project"
    [[ "$(read_kv "${OUTPUT}/artifact.env" kernel_release)" == "${KVER}" ]] || \
        die "refusing to archive an artifact for another kernel"
    PREVIOUS="${OUTPUT}.previous.$(date -u +%Y%m%dT%H%M%SZ).$$"
    [[ ! -e "${PREVIOUS}" && ! -L "${PREVIOUS}" ]] || die "previous-artifact path collision"
    mv -T -- "${OUTPUT}" "${PREVIOUS}"
    info "Previous artifact preserved at ${PREVIOUS}"
fi
mv -T -- "${STAGE}" "${OUTPUT}"

info "Artifact ready: ${OUTPUT}"
info "Build tree retained for audit: ${BUILD_DIR}"
info "Memory capacity remains unverified; building modules is not a hardware result"
