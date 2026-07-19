#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "${SCRIPT_DIR}/lib.sh"

if (($#)); then
    case "$1" in
        -h|--help)
            printf '%s\n' \
                'Usage: ./build-validator.sh' \
                'Builds the fixed sibling CUDA validator for GA100 (sm_80).'
            exit 0
            ;;
        *) die "unknown argument: $1" ;;
    esac
fi

require_linux_x86_64
require_non_root
require_command nvcc
require_command sha256sum
require_command stat
require_command mktemp

SOURCE="${SCRIPT_DIR}/tools/memory-pattern-test.cu"
OUTPUT="${SCRIPT_DIR}/tools/memory-pattern-test"
[[ -f "${SOURCE}" && ! -L "${SOURCE}" ]] || die "fixed CUDA validator source is missing"
[[ "$(stat -c '%u' -- "${SOURCE}")" == "${EUID}" ]] || \
    die "fixed CUDA validator source is not owned by the invoking user"
mode_is_not_group_or_world_writable "$(stat -c '%a' -- "${SOURCE}")" || \
    die "fixed CUDA validator source is group/world writable"
[[ -d "$(dirname -- "${OUTPUT}")" && ! -L "$(dirname -- "${OUTPUT}")" ]] || \
    die "validator output directory is unsafe"
[[ "$(stat -c '%u' -- "$(dirname -- "${OUTPUT}")")" == "${EUID}" ]] || \
    die "validator output directory is not owned by the invoking user"
mode_is_not_group_or_world_writable "$(stat -c '%a' -- "$(dirname -- "${OUTPUT}")")" || \
    die "validator output directory is group/world writable"

mkdir -p -- "${SCRIPT_DIR}/.work"
[[ -d "${SCRIPT_DIR}/.work" && ! -L "${SCRIPT_DIR}/.work" ]] || die "validator work root is unsafe"
[[ "$(stat -c '%u' -- "${SCRIPT_DIR}/.work")" == "${EUID}" ]] || \
    die "validator work root is not owned by the invoking user"
mode_is_not_group_or_world_writable "$(stat -c '%a' -- "${SCRIPT_DIR}/.work")" || \
    die "validator work root is group/world writable"
BUILD_DIR="$(mktemp -d "${SCRIPT_DIR}/.work/validator.XXXXXXXX")"
STAGE="${BUILD_DIR}/memory-pattern-test"
nvcc -O3 -std=c++17 -arch=sm_80 "${SOURCE}" -o "${STAGE}"
[[ -f "${STAGE}" && ! -L "${STAGE}" ]] || die "nvcc did not produce a regular validator binary"
[[ "$(stat -c '%u' -- "${STAGE}")" == "${EUID}" ]] || die "validator stage has the wrong owner"
chmod 0755 -- "${STAGE}"
mode_is_not_group_or_world_writable "$(stat -c '%a' -- "${STAGE}")" || \
    die "validator stage is group/world writable"

if [[ -e "${OUTPUT}" || -L "${OUTPUT}" ]]; then
    [[ -f "${OUTPUT}" && ! -L "${OUTPUT}" ]] || die "existing validator output is unsafe"
    [[ "$(stat -c '%u' -- "${OUTPUT}")" == "${EUID}" ]] || \
        die "existing validator output is not owned by the invoking user"
    PREVIOUS="${OUTPUT}.previous.$(date -u +%Y%m%dT%H%M%SZ).$$"
    [[ ! -e "${PREVIOUS}" && ! -L "${PREVIOUS}" ]] || die "validator backup path collision"
    mv -T -- "${OUTPUT}" "${PREVIOUS}"
    info "Previous validator binary preserved at ${PREVIOUS}"
fi
mv -T -- "${STAGE}" "${OUTPUT}"
info "Fixed validator binary: ${OUTPUT}"
info "SHA-256: $(sha256_file "${OUTPUT}")"
info "Build directory retained for audit: ${BUILD_DIR}"
