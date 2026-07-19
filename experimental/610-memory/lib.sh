#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only

# Shared fail-closed checks for the 610.43.03 memory experiment.

LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=manifest.env
source "${LIB_DIR}/manifest.env"

info() { printf '[INFO] %s\n' "$*"; }
warn() { printf '[WARN] %s\n' "$*" >&2; }
die() { printf '[FAIL] %s\n' "$*" >&2; exit 1; }

require_command() {
    command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

sha256_file() {
    sha256sum -- "$1" | awk '{print $1}'
}

file_size() {
    stat -c '%s' -- "$1"
}

require_hex_sha256() {
    [[ "$1" =~ ^[0-9a-f]{64}$ ]] || die "$2 is not a lowercase SHA-256: $1"
}

require_hex_commit() {
    [[ "$1" =~ ^[0-9a-f]{40}$ ]] || die "$2 is not a lowercase 40-hex commit id: $1"
}

require_manifest() {
    [[ "${MANIFEST_SCHEMA}" == "1" ]] || die "unsupported manifest schema: ${MANIFEST_SCHEMA}"
    [[ "${PROJECT_ID}" == "cmpunlocker-610-memory" ]] || die "unexpected project id"
    [[ "${NVIDIA_DRIVER_VERSION}" == "610.43.03" ]] || die "unexpected driver version"
    require_hex_commit "${NVIDIA_SOURCE_COMMIT}" "NVIDIA source commit"
    require_hex_sha256 "${NVIDIA_RUNFILE_SHA256}" "official runfile"
    require_hex_sha256 "${GSP_TU10X_SHA256}" "GSP firmware"
    [[ "${PATCH_SHA256}" != "PENDING_REVIEWED_PATCH_SHA256" ]] || \
        die "manifest patch hash has not been finalized"
    require_hex_sha256 "${PATCH_SHA256}" "experiment patch"
}

require_linux_x86_64() {
    [[ "$(uname -s)" == "Linux" ]] || die "this experiment supports Linux only"
    [[ "$(uname -m)" == "x86_64" ]] || die "this experiment supports x86_64 only"
}

require_root() {
    [[ "${EUID}" -eq 0 ]] || die "run this operation as root"
}

require_non_root() {
    [[ "${EUID}" -ne 0 ]] || die "refusing to fetch/build kernel source as root"
}

require_acknowledgement() {
    local actual="$1"
    local expected="$2"
    [[ "${actual}" == "${expected}" ]] || die "required acknowledgement: ${expected}"
}

discover_single_cmp_bdf() {
    local sysdev vendor device
    local -a nvidia_functions=()

    shopt -s nullglob
    for sysdev in /sys/bus/pci/devices/*; do
        [[ -r "${sysdev}/vendor" && -r "${sysdev}/device" ]] || continue
        vendor="$(tr '[:upper:]' '[:lower:]' < "${sysdev}/vendor")"
        if [[ "${vendor}" == "0x10de" ]]; then
            nvidia_functions+=("${sysdev}")
        fi
    done
    shopt -u nullglob

    [[ "${#nvidia_functions[@]}" -eq 1 ]] || \
        die "required exactly one NVIDIA PCI function; found ${#nvidia_functions[@]}"
    sysdev="${nvidia_functions[0]}"
    device="$(tr '[:upper:]' '[:lower:]' < "${sysdev}/device")"
    [[ "${device}" == "0x20c2" ]] || \
        die "the sole NVIDIA function is ${device}, not CMP 170HX 0x20c2"
    basename -- "${sysdev}"
}

require_secure_boot_disabled() {
    local state lockdown

    if [[ -d /sys/firmware/efi ]]; then
        require_command mokutil
        state="$(mokutil --sb-state 2>&1)" || die "could not determine Secure Boot state"
        grep -qi 'SecureBoot disabled' <<<"${state}" || \
            die "Secure Boot is enabled or its state is indeterminate"
    fi

    if [[ -r /sys/kernel/security/lockdown ]]; then
        lockdown="$(< /sys/kernel/security/lockdown)"
        grep -q '\[none\]' <<<"${lockdown}" || \
            die "kernel lockdown is active: ${lockdown}"
    fi
}

find_exact_gsp_firmware() {
    local candidate resolved actual_hash actual_size
    local -a candidates=()
    local -a matches=()

    if [[ -n "${CMPUNLOCKER_GSP_FIRMWARE:-}" ]]; then
        candidates+=("${CMPUNLOCKER_GSP_FIRMWARE}")
    else
        candidates+=(
            "/lib/firmware/nvidia/${NVIDIA_DRIVER_VERSION}/gsp_tu10x.bin"
            "/usr/lib/firmware/nvidia/${NVIDIA_DRIVER_VERSION}/gsp_tu10x.bin"
        )
    fi

    for candidate in "${candidates[@]}"; do
        [[ -f "${candidate}" ]] || continue
        resolved="$(readlink -f -- "${candidate}")" || continue
        actual_size="$(file_size "${resolved}")"
        actual_hash="$(sha256_file "${resolved}")"
        if [[ "${actual_size}" == "${GSP_TU10X_SIZE}" && \
              "${actual_hash}" == "${GSP_TU10X_SHA256}" ]]; then
            if [[ ! " ${matches[*]-} " =~ " ${resolved} " ]]; then
                matches+=("${resolved}")
            fi
        fi
    done

    [[ "${#matches[@]}" -eq 1 ]] || \
        die "could not identify exactly one official ${NVIDIA_DRIVER_VERSION} gsp_tu10x.bin"
    printf '%s\n' "${matches[0]}"
}

require_installed_stack() {
    local bdf="$1"
    local proc_version module_file module_version module_src loaded_src firmware
    local version_output smi_version nvml_version
    local gsp_version_output loaded_gsp_version
    local -a versions=()

    require_command nvidia-smi
    require_command modinfo
    require_command sha256sum
    require_command stat
    require_command readlink

    mapfile -t versions < <(
        nvidia-smi --query-gpu=driver_version --format=csv,noheader,nounits 2>/dev/null |
            sed 's/^[[:space:]]*//;s/[[:space:]]*$//' |
            sed '/^$/d'
    )
    [[ "${#versions[@]}" -eq 1 ]] || die "nvidia-smi did not report exactly one GPU"
    [[ "${versions[0]}" == "${NVIDIA_DRIVER_VERSION}" ]] || \
        die "nvidia-smi reports driver ${versions[0]}, not ${NVIDIA_DRIVER_VERSION}"
    version_output="$(nvidia-smi --version 2>&1)" || die "nvidia-smi --version failed"
    smi_version="$(read_version_line "${version_output}" 'NVIDIA-SMI version')"
    nvml_version="$(read_version_line "${version_output}" 'NVML version')"
    [[ "${smi_version}" == "${NVIDIA_DRIVER_VERSION}" ]] || \
        die "NVIDIA-SMI userspace is ${smi_version}, not ${NVIDIA_DRIVER_VERSION}"
    [[ "${nvml_version}" == "${NVIDIA_DRIVER_VERSION}" ]] || \
        die "NVML userspace is ${nvml_version}, not ${NVIDIA_DRIVER_VERSION}"
    gsp_version_output="$(nvidia-smi -i "${bdf}" -q -d GSP_FIRMWARE_VERSION 2>&1)" || \
        die "nvidia-smi could not query the loaded GSP firmware version"
    loaded_gsp_version="$(read_version_line "${gsp_version_output}" 'GSP Firmware Version')"
    [[ "${loaded_gsp_version}" == "${NVIDIA_DRIVER_VERSION}" ]] || \
        die "loaded GSP firmware is ${loaded_gsp_version}, not ${NVIDIA_DRIVER_VERSION}"
    nvidia-smi -i "${bdf}" --query-gpu=pci.bus_id --format=csv,noheader >/dev/null 2>&1 || \
        die "nvidia-smi cannot address target ${bdf}"

    [[ -r /proc/driver/nvidia/version ]] || die "loaded NVIDIA module version is unavailable"
    proc_version="$(< /proc/driver/nvidia/version)"
    grep -Fq "${NVIDIA_DRIVER_VERSION}" <<<"${proc_version}" || \
        die "loaded NVIDIA module does not report ${NVIDIA_DRIVER_VERSION}"
    grep -qi 'Open Kernel Module' <<<"${proc_version}" || \
        die "loaded NVIDIA module is not identified as the open kernel module"

    module_file="$(modinfo -n nvidia 2>/dev/null)" || die "cannot resolve nvidia.ko"
    module_file="$(readlink -f -- "${module_file}")" || die "cannot canonicalize resolved nvidia.ko"
    module_version="$(modinfo -F version "${module_file}" 2>/dev/null)" || \
        die "cannot read resolved nvidia.ko metadata"
    [[ "${module_version}" == "${NVIDIA_DRIVER_VERSION}" ]] || \
        die "resolved nvidia.ko is ${module_version}, not ${NVIDIA_DRIVER_VERSION}"
    [[ -r /sys/module/nvidia/srcversion ]] || die "loaded nvidia srcversion is unavailable"
    module_src="$(modinfo -F srcversion "${module_file}" 2>/dev/null)" || \
        die "cannot read resolved nvidia.ko srcversion"
    loaded_src="$(< /sys/module/nvidia/srcversion)"
    [[ -n "${module_src}" && "${loaded_src}" == "${module_src}" ]] || \
        die "loaded nvidia core does not match the resolved on-disk predecessor"

    firmware="$(find_exact_gsp_firmware)"
    info "Exact GSP firmware: ${firmware}"
    info "Loaded GSP firmware version: ${loaded_gsp_version}"
}

require_native_memory_total() {
    local bdf="$1"
    local -a totals=()

    mapfile -t totals < <(
        nvidia-smi -i "${bdf}" --query-gpu=memory.total \
            --format=csv,noheader,nounits 2>/dev/null |
            sed 's/^[[:space:]]*//;s/[[:space:]]*$//' |
            sed '/^$/d'
    )
    [[ "${#totals[@]}" -eq 1 ]] || \
        die "nvidia-smi did not return exactly one memory.total value for ${bdf}"
    [[ "${totals[0]}" =~ ^[0-9]+$ ]] || \
        die "nvidia-smi returned a non-integer memory.total value: ${totals[0]}"
    [[ "${totals[0]}" == "8192" ]] || \
        die "memory.total is ${totals[0]} MiB, not the native 8192 MiB; a cold power cycle may still be required"
}

patch_path() {
    printf '%s/%s\n' "${LIB_DIR}" "${PATCH_RELATIVE_PATH}"
}

verify_patch_hash() {
    local patch actual
    patch="$(patch_path)"
    [[ -f "${patch}" ]] || die "experiment patch is missing: ${patch}"
    actual="$(sha256_file "${patch}")"
    [[ "${actual}" == "${PATCH_SHA256}" ]] || \
        die "experiment patch hash mismatch: expected ${PATCH_SHA256}, got ${actual}"
}

read_kv() {
    local file="$1"
    local key="$2"
    local value
    value="$(awk -F= -v wanted="${key}" '
        $1 == wanted { count++; sub(/^[^=]*=/, ""); value=$0 }
        END { if (count != 1) exit 1; print value }
    ' "${file}")" || die "missing or duplicate ${key} in ${file}"
    [[ "${value}" != *$'\n'* && "${value}" != *$'\r'* ]] || die "invalid ${key} value"
    printf '%s\n' "${value}"
}

read_version_line() {
    local output="$1"
    local wanted="$2"
    local value

    value="$(awk -F: -v wanted="${wanted}" '
        {
            key=$1
            gsub(/^[[:space:]]+|[[:space:]]+$/, "", key)
            if (key == wanted) {
                count++
                sub(/^[^:]*:/, "")
                gsub(/^[[:space:]]+|[[:space:]]+$/, "")
                value=$0
            }
        }
        END { if (count != 1) exit 1; print value }
    ' <<<"${output}")" || die "missing or duplicate ${wanted} in nvidia-smi --version"
    printf '%s\n' "${value}"
}

module_filenames() {
    printf '%s\n' \
        nvidia.ko \
        nvidia-modeset.ko \
        nvidia-uvm.ko \
        nvidia-drm.ko \
        nvidia-peermem.ko
}

module_pairs() {
    printf '%s\n' \
        'nvidia nvidia.ko nvidia' \
        'nvidia-modeset nvidia-modeset.ko nvidia_modeset' \
        'nvidia-uvm nvidia-uvm.ko nvidia_uvm' \
        'nvidia-drm nvidia-drm.ko nvidia_drm' \
        'nvidia-peermem nvidia-peermem.ko nvidia_peermem'
}

required_module_markers() {
    printf '%s\n' \
        'cmpunlock610: gate-active device=10de:20c2' \
        'cmpunlock610: unlock-ok device=10de:20c2' \
        'cmpunlock610: metadata-ok bytes=' \
        'cmpunlock610: pma-ok regions-before=' \
        'cmpunlock610: fail stage=pma-overlap' \
        'cmpunlock610: fail stage=pma-numa' \
        'cmpunlock610: fail stage=pma-total-overflow' \
        'cmpunlock610: fail stage=pma-init'
}

verify_required_module_markers() {
    local module_file="$1"
    local listing marker rc=0

    [[ -f "${module_file}" && ! -L "${module_file}" ]] || \
        die "final nvidia module is missing or symlinked: ${module_file}"
    listing="$(mktemp "${TMPDIR:-/var/tmp}/cmpunlocker-nvidia-strings.XXXXXXXX")" || \
        die "cannot create marker inspection file"
    if ! strings -a "${module_file}" > "${listing}"; then
        rm -f -- "${listing}"
        die "cannot extract strings from final nvidia module"
    fi
    while IFS= read -r marker; do
        if ! grep -Fq -- "${marker}" "${listing}"; then
            warn "final nvidia module lacks required marker: ${marker}"
            rc=1
        fi
    done < <(required_module_markers)
    rm -f -- "${listing}"
    [[ "${rc}" -eq 0 ]] || die "final nvidia module omitted a required experimental path"
}

verify_artifact() {
    local artifact="$1"
    local metadata checksums file expected actual count

    metadata="${artifact}/artifact.env"
    checksums="${artifact}/checksums.sha256"
    [[ -f "${metadata}" && -f "${checksums}" ]] || die "artifact metadata is incomplete"
    [[ "$(read_kv "${metadata}" project_id)" == "${PROJECT_ID}" ]] || die "wrong artifact project"
    [[ "$(read_kv "${metadata}" driver_version)" == "${NVIDIA_DRIVER_VERSION}" ]] || die "wrong artifact driver"
    [[ "$(read_kv "${metadata}" source_commit)" == "${NVIDIA_SOURCE_COMMIT}" ]] || die "wrong source commit"
    [[ "$(read_kv "${metadata}" patch_sha256)" == "${PATCH_SHA256}" ]] || die "wrong patch hash"
    [[ "$(read_kv "${metadata}" kernel_release)" == "$(uname -r)" ]] || die "artifact kernel mismatch"
    [[ "$(read_kv "${metadata}" gsp_tu10x_sha256)" == "${GSP_TU10X_SHA256}" ]] || die "wrong firmware gate"

    while IFS= read -r file; do
        [[ -f "${artifact}/${file}" ]] || die "artifact is missing ${file}"
        count="$(awk -v wanted="${file}" '$2 == wanted { count++ } END { print count+0 }' "${checksums}")"
        [[ "${count}" == "1" ]] || die "checksum manifest must contain ${file} exactly once"
        expected="$(awk -v wanted="${file}" '$2 == wanted { print $1 }' "${checksums}")"
        require_hex_sha256 "${expected}" "${file} checksum"
        actual="$(sha256_file "${artifact}/${file}")"
        [[ "${actual}" == "${expected}" ]] || die "artifact checksum mismatch for ${file}"
    done < <(module_filenames)

    count="$(awk 'NF { count++ } END { print count+0 }' "${checksums}")"
    [[ "${count}" == "5" ]] || die "checksum manifest must contain exactly five modules"
}

mode_is_not_group_or_world_writable() {
    local mode="$1"
    [[ "${mode}" =~ ^[0-7]{3,4}$ ]] || return 1
    (( (8#${mode} & 8#022) == 0 ))
}

verify_artifact_permissions() {
    local artifact="$1"
    local owner mode file
    local -a files=(artifact.env checksums.sha256)

    [[ -d "${artifact}" && ! -L "${artifact}" ]] || die "artifact directory is unsafe"
    owner="$(stat -c '%u' -- "${artifact}")"
    mode="$(stat -c '%a' -- "${artifact}")"
    mode_is_not_group_or_world_writable "${mode}" || \
        die "artifact directory is group/world writable"
    while IFS= read -r file; do files+=("${file}"); done < <(module_filenames)
    for file in "${files[@]}"; do
        [[ -f "${artifact}/${file}" && ! -L "${artifact}/${file}" ]] || \
            die "artifact member is missing or symlinked: ${file}"
        [[ "$(stat -c '%u' -- "${artifact}/${file}")" == "${owner}" ]] || \
            die "artifact member has a different owner: ${file}"
        mode="$(stat -c '%a' -- "${artifact}/${file}")"
        mode_is_not_group_or_world_writable "${mode}" || \
            die "artifact member is group/world writable: ${file}"
    done
}

verify_installed_tree_permissions() {
    local target="$1"
    local mode file
    local -a files=(artifact.env checksums.sha256 .cmpunlocker-610-memory)

    [[ -d "${target}" && ! -L "${target}" ]] || die "installed target directory is unsafe"
    [[ "$(stat -c '%u' -- "${target}")" == "0" ]] || die "installed target is not root-owned"
    mode="$(stat -c '%a' -- "${target}")"
    mode_is_not_group_or_world_writable "${mode}" || die "installed target is group/world writable"
    while IFS= read -r file; do files+=("${file}"); done < <(module_filenames)
    for file in "${files[@]}"; do
        [[ -f "${target}/${file}" && ! -L "${target}/${file}" ]] || \
            die "installed member is missing or symlinked: ${file}"
        [[ "$(stat -c '%u' -- "${target}/${file}")" == "0" ]] || \
            die "installed member is not root-owned: ${file}"
        mode="$(stat -c '%a' -- "${target}/${file}")"
        mode_is_not_group_or_world_writable "${mode}" || \
            die "installed member is group/world writable: ${file}"
    done
}

verify_module_metadata() {
    local artifact="$1"
    local kver="$2"
    local module version vermagic

    while IFS= read -r module; do
        version="$(modinfo -F version "${artifact}/${module}" 2>/dev/null)" || \
            die "cannot read ${module} version metadata"
        [[ "${version}" == "${NVIDIA_DRIVER_VERSION}" ]] || \
            die "${module} reports driver ${version}, not ${NVIDIA_DRIVER_VERSION}"
        vermagic="$(modinfo -F vermagic "${artifact}/${module}" 2>/dev/null)" || \
            die "cannot read ${module} vermagic"
        [[ "${vermagic}" == "${kver} "* || "${vermagic}" == "${kver}" ]] || \
            die "${module} vermagic does not begin with ${kver}"
        readelf -h "${artifact}/${module}" >/dev/null || \
            die "${module} is not a readable ELF module"
    done < <(module_filenames)
}

resolve_module_file() {
    local module_name="$1"
    local filename="$2"
    local output directive candidate remainder base resolved
    local -a matches=()

    output="$(modprobe --show-depends "${module_name}" 2>/dev/null)" || return 1
    while read -r directive candidate remainder; do
        [[ "${directive}" == "insmod" && -n "${candidate:-}" ]] || continue
        base="${candidate##*/}"
        case "${base}" in
            "${filename}"|"${filename}.gz"|"${filename}.xz"|"${filename}.zst"|\
            "${filename}.bz2"|"${filename}.lz4") matches+=("${candidate}") ;;
        esac
    done <<<"${output}"
    [[ "${#matches[@]}" -eq 1 ]] || return 1
    resolved="$(readlink -f -- "${matches[0]}")" || return 1
    [[ -f "${resolved}" ]] || return 1
    printf '%s\n' "${resolved}"
}

verify_resolved_experiment_module_set() {
    local target="$1"
    local checksums="${target}/checksums.sha256"
    local module_name filename key resolved expected actual

    while read -r module_name filename key; do
        resolved="$(resolve_module_file "${module_name}" "${filename}")" || \
            die "cannot resolve ${module_name} through modprobe metadata"
        [[ "${resolved}" == "${target}/${filename}" ]] || \
            die "${module_name} resolves to ${resolved}, not ${target}/${filename}"
        expected="$(awk -v wanted="${filename}" '$2 == wanted { print $1 }' "${checksums}")"
        require_hex_sha256 "${expected}" "${filename} installed checksum"
        actual="$(sha256_file "${resolved}")"
        [[ "${actual}" == "${expected}" ]] || \
            die "resolved ${module_name} differs from the installed checksum"
    done < <(module_pairs)
}

capture_stock_module_set() {
    local module_root="$1"
    local target="$2"
    local module_name filename key resolved hash

    while read -r module_name filename key; do
        resolved="$(resolve_module_file "${module_name}" "${filename}")" || \
            die "cannot resolve required stock module ${module_name}"
        require_stock_module_path "${resolved}" "${module_root}" "${target}"
        hash="$(sha256_file "${resolved}")"
        printf 'previous_%s_path=%s\n' "${key}" "${resolved}"
        printf 'previous_%s_sha256=%s\n' "${key}" "${hash}"
    done < <(module_pairs)
}

verify_recorded_stock_module_files() {
    local state_file="$1"
    local module_root="$2"
    local target="$3"
    local module_name filename key recorded_path recorded_hash

    while read -r module_name filename key; do
        recorded_path="$(read_kv "${state_file}" "previous_${key}_path")"
        recorded_hash="$(read_kv "${state_file}" "previous_${key}_sha256")"
        require_hex_sha256 "${recorded_hash}" "recorded ${module_name} predecessor"
        require_stock_module_path "${recorded_path}" "${module_root}" "${target}"
        [[ "$(sha256_file "${recorded_path}")" == "${recorded_hash}" ]] || \
            die "recorded stock predecessor ${module_name} changed since installation"
    done < <(module_pairs)
}

verify_resolved_stock_module_set() {
    local state_file="$1"
    local module_root="$2"
    local target="$3"
    local module_name filename key recorded_path recorded_hash resolved

    while read -r module_name filename key; do
        recorded_path="$(read_kv "${state_file}" "previous_${key}_path")"
        recorded_hash="$(read_kv "${state_file}" "previous_${key}_sha256")"
        require_hex_sha256 "${recorded_hash}" "recorded ${module_name} predecessor"
        require_stock_module_path "${recorded_path}" "${module_root}" "${target}"
        resolved="$(resolve_module_file "${module_name}" "${filename}")" || \
            die "cannot resolve restored stock module ${module_name}"
        [[ "${resolved}" == "${recorded_path}" ]] || \
            die "${module_name} resolves to ${resolved}, not recorded predecessor ${recorded_path}"
        [[ "$(sha256_file "${resolved}")" == "${recorded_hash}" ]] || \
            die "resolved stock predecessor ${module_name} has the wrong hash"
    done < <(module_pairs)
}

require_loaded_module_matches_file() {
    local sys_name="$1"
    local module_file="$2"
    local description="$3"
    local loaded_version loaded_src installed_version installed_src

    [[ -r "/sys/module/${sys_name}/version" && \
       -r "/sys/module/${sys_name}/srcversion" ]] || \
        die "required ${description} is not loaded with version/srcversion metadata"
    loaded_version="$(< "/sys/module/${sys_name}/version")"
    loaded_src="$(< "/sys/module/${sys_name}/srcversion")"
    installed_version="$(modinfo -F version "${module_file}")" || \
        die "cannot read ${description} file version"
    installed_src="$(modinfo -F srcversion "${module_file}")" || \
        die "cannot read ${description} file srcversion"
    [[ "${loaded_version}" == "${NVIDIA_DRIVER_VERSION}" && \
       "${installed_version}" == "${NVIDIA_DRIVER_VERSION}" && \
       -n "${installed_src}" && "${loaded_src}" == "${installed_src}" ]] || \
        die "loaded ${description} does not match ${module_file}"
}

require_loaded_core_match() {
    local target="$1"
    require_loaded_module_matches_file nvidia "${target}/nvidia.ko" \
        'experimental nvidia core module'
}

require_loaded_recorded_core_match() {
    local state_file="$1"
    local recorded_path recorded_hash

    recorded_path="$(read_kv "${state_file}" previous_nvidia_path)"
    recorded_hash="$(read_kv "${state_file}" previous_nvidia_sha256)"
    require_hex_sha256 "${recorded_hash}" "recorded nvidia predecessor"
    [[ "$(sha256_file "${recorded_path}")" == "${recorded_hash}" ]] || \
        die "recorded nvidia predecessor hash changed"
    require_loaded_module_matches_file nvidia "${recorded_path}" \
        'stock nvidia core module'
}

require_loaded_core_and_uvm_match() {
    local target="$1"
    local sys_name filename loaded_version loaded_src installed_version installed_src

    while read -r sys_name filename; do
        [[ -r "/sys/module/${sys_name}/version" && -r "/sys/module/${sys_name}/srcversion" ]] || \
            die "required ${sys_name} module is not loaded with version/srcversion metadata"
        loaded_version="$(< "/sys/module/${sys_name}/version")"
        loaded_src="$(< "/sys/module/${sys_name}/srcversion")"
        installed_version="$(modinfo -F version "${target}/${filename}")" || \
            die "cannot read installed ${filename} version"
        installed_src="$(modinfo -F srcversion "${target}/${filename}")" || \
            die "cannot read installed ${filename} srcversion"
        [[ "${loaded_version}" == "${NVIDIA_DRIVER_VERSION}" && \
           -n "${installed_src}" && "${loaded_src}" == "${installed_src}" && \
           "${installed_version}" == "${NVIDIA_DRIVER_VERSION}" ]] || \
            die "loaded ${sys_name} does not match the installed experimental module"
    done <<'EOF'
nvidia nvidia.ko
nvidia_uvm nvidia-uvm.ko
EOF
}

require_loaded_recorded_core_and_uvm_match() {
    local state_file="$1"
    local sys_name key recorded_path recorded_hash loaded_version loaded_src
    local installed_version installed_src

    while read -r sys_name key; do
        recorded_path="$(read_kv "${state_file}" "previous_${key}_path")"
        recorded_hash="$(read_kv "${state_file}" "previous_${key}_sha256")"
        require_hex_sha256 "${recorded_hash}" "recorded ${sys_name} predecessor"
        [[ "$(sha256_file "${recorded_path}")" == "${recorded_hash}" ]] || \
            die "recorded ${sys_name} predecessor hash changed"
        [[ -r "/sys/module/${sys_name}/version" && -r "/sys/module/${sys_name}/srcversion" ]] || \
            die "required stock ${sys_name} module is not loaded with version/srcversion metadata"
        loaded_version="$(< "/sys/module/${sys_name}/version")"
        loaded_src="$(< "/sys/module/${sys_name}/srcversion")"
        installed_version="$(modinfo -F version "${recorded_path}")" || \
            die "cannot read recorded ${sys_name} version"
        installed_src="$(modinfo -F srcversion "${recorded_path}")" || \
            die "cannot read recorded ${sys_name} srcversion"
        [[ "${loaded_version}" == "${NVIDIA_DRIVER_VERSION}" && \
           "${installed_version}" == "${NVIDIA_DRIVER_VERSION}" && \
           -n "${installed_src}" && "${loaded_src}" == "${installed_src}" ]] || \
            die "loaded stock ${sys_name} does not match its recorded predecessor"
    done <<'EOF'
nvidia nvidia
nvidia_uvm nvidia_uvm
EOF
}

verify_install_state_identity() {
    local state_file="$1"
    local kver="$2"
    local target="$3"

    [[ -f "${state_file}" && ! -L "${state_file}" ]] || \
        die "trusted install state is missing or is a symlink: ${state_file}"
    [[ "$(stat -c '%u' -- "${state_file}")" == "0" ]] || \
        die "trusted install state is not owned by root"
    [[ "$(stat -c '%a' -- "${state_file}")" == "600" ]] || \
        die "trusted install state permissions are not 0600"
    [[ "$(read_kv "${state_file}" schema)" == "1" ]] || die "unsupported install-state schema"
    [[ "$(read_kv "${state_file}" project_id)" == "${PROJECT_ID}" ]] || die "wrong state project"
    [[ "$(read_kv "${state_file}" kernel_release)" == "${kver}" ]] || die "state kernel mismatch"
    [[ "$(read_kv "${state_file}" target_dir)" == "${target}" ]] || die "state target mismatch"
    [[ "$(read_kv "${state_file}" source_commit)" == "${NVIDIA_SOURCE_COMMIT}" ]] || \
        die "state source commit mismatch"
    [[ "$(read_kv "${state_file}" patch_sha256)" == "${PATCH_SHA256}" ]] || \
        die "state patch hash mismatch"
    [[ "$(read_kv "${state_file}" memory_capacity_verified)" == "false" ]] || \
        die "install state contains an unsupported capacity claim"
}

select_initramfs_tool() {
    if command -v update-initramfs >/dev/null 2>&1 && \
       command -v lsinitramfs >/dev/null 2>&1; then
        printf '%s\n' update-initramfs
    elif command -v dracut >/dev/null 2>&1 && \
         command -v lsinitrd >/dev/null 2>&1; then
        printf '%s\n' dracut
    else
        return 1
    fi
}

initramfs_image() {
    local tool="$1"
    local kver="$2"
    local image

    case "${tool}" in
        update-initramfs) image="/boot/initrd.img-${kver}" ;;
        dracut) image="/boot/initramfs-${kver}.img" ;;
        *) return 1 ;;
    esac
    [[ -f "${image}" && ! -L "${image}" ]] || return 1
    printf '%s\n' "${image}"
}

backup_initramfs() {
    local image="$1"
    local backup_root="$2"
    local stamp="$3"
    local backup before_hash after_hash

    [[ -f "${image}" && ! -L "${image}" ]] || return 1
    case "${image}" in /boot/*) ;; *) return 1 ;; esac
    [[ -d "${backup_root}" && ! -L "${backup_root}" ]] || return 1
    backup="${backup_root}/$(basename -- "${image}").before.${stamp}"
    [[ ! -e "${backup}" && ! -L "${backup}" ]] || return 1
    before_hash="$(sha256_file "${image}")" || return 1
    cp --preserve=all --reflink=auto -- "${image}" "${backup}" || return 1
    [[ -f "${backup}" && ! -L "${backup}" ]] || return 1
    after_hash="$(sha256_file "${backup}")" || return 1
    [[ "${after_hash}" == "${before_hash}" ]] || return 1
    sync -f -- "${backup}" || return 1
    sync -f -- "${backup_root}" || return 1
    printf '%s\n' "${backup}"
}

restore_initramfs() {
    local backup="$1"
    local image="$2"
    local expected_hash="$3"
    local stamp="$4"
    local parent stage restored_hash

    require_hex_sha256 "${expected_hash}" "recorded initramfs backup"
    [[ -f "${backup}" && ! -L "${backup}" ]] || return 1
    [[ "$(sha256_file "${backup}")" == "${expected_hash}" ]] || return 1
    case "${image}" in /boot/*) ;; *) return 1 ;; esac
    parent="$(dirname -- "${image}")"
    [[ -d "${parent}" && ! -L "${parent}" ]] || return 1
    stage="${parent}/.$(basename -- "${image}").cmpunlocker-restore.${stamp}"
    [[ ! -e "${stage}" && ! -L "${stage}" ]] || return 1
    cp --preserve=all --reflink=auto -- "${backup}" "${stage}" || return 1
    restored_hash="$(sha256_file "${stage}")" || return 1
    [[ "${restored_hash}" == "${expected_hash}" ]] || return 1
    sync -f -- "${stage}" || return 1
    mv -f -- "${stage}" "${image}" || return 1
    sync -f -- "${image}" || return 1
    sync -f -- "${parent}" || return 1
}

run_initramfs() {
    local tool="$1"
    local kver="$2"
    local image="$3"
    [[ "$(initramfs_image "${tool}" "${kver}")" == "${image}" ]] || \
        die "refusing an unexpected initramfs image: ${image}"
    case "${tool}" in
        update-initramfs) update-initramfs -u -k "${kver}" ;;
        dracut) dracut --force "${image}" "${kver}" ;;
        *) die "unsupported initramfs tool: ${tool}" ;;
    esac
}

list_initramfs() {
    local tool="$1"
    local image="$2"
    case "${tool}" in
        update-initramfs) lsinitramfs "${image}" ;;
        dracut) lsinitrd "${image}" ;;
        *) return 1 ;;
    esac
}

verify_initramfs_selection() {
    local tool="$1"
    local image="$2"
    local kver="$3"
    local listing module path target_count rc=0
    local -a matches=()

    [[ -f "${image}" && ! -L "${image}" ]] || return 1
    listing="$(mktemp "${TMPDIR:-/var/tmp}/cmpunlocker-initramfs.XXXXXXXX")" || return 1
    if ! list_initramfs "${tool}" "${image}" > "${listing}"; then
        rm -f -- "${listing}"
        return 1
    fi
    while IFS= read -r module; do
        mapfile -t matches < <(
            awk -v prefix="modules/${kver}/" -v wanted="${module}" '
                {
                    for (i = 1; i <= NF; i++) {
                        token=$i
                        sub(/^\.\//, "", token)
                        if (index(token, prefix) == 0)
                            continue
                        count=split(token, part, "/")
                        base=part[count]
                        if (base == wanted || base == wanted ".gz" ||
                            base == wanted ".xz" || base == wanted ".zst" ||
                            base == wanted ".bz2" || base == wanted ".lz4")
                            print token
                    }
                }
            ' "${listing}"
        )
        target_count=0
        for path in "${matches[@]}"; do
            case "${path}" in
                *"modules/${kver}/${INSTALL_RELATIVE_DIR}/${module}"|\
                *"modules/${kver}/${INSTALL_RELATIVE_DIR}/${module}.gz"|\
                *"modules/${kver}/${INSTALL_RELATIVE_DIR}/${module}.xz"|\
                *"modules/${kver}/${INSTALL_RELATIVE_DIR}/${module}.zst"|\
                *"modules/${kver}/${INSTALL_RELATIVE_DIR}/${module}.bz2"|\
                *"modules/${kver}/${INSTALL_RELATIVE_DIR}/${module}.lz4")
                    ((target_count += 1))
                    ;;
                *)
                    warn "initramfs contains competing ${module}: ${path}"
                    rc=1
                    ;;
            esac
        done
        if (( target_count > 1 )); then
            warn "initramfs contains ${target_count} copies of the isolated ${module}"
            rc=1
        fi
    done < <(module_filenames)
    rm -f -- "${listing}"
    return "${rc}"
}

verify_initramfs_excludes() {
    local tool="$1"
    local image="$2"
    local kver="$3"
    local listing module needle rc=0

    [[ -f "${image}" && ! -L "${image}" ]] || return 1
    listing="$(mktemp "${TMPDIR:-/var/tmp}/cmpunlocker-initramfs.XXXXXXXX")" || return 1
    if ! list_initramfs "${tool}" "${image}" > "${listing}"; then
        rm -f -- "${listing}"
        return 1
    fi
    while IFS= read -r module; do
        needle="modules/${kver}/${INSTALL_RELATIVE_DIR}/${module}"
        if grep -Fq -- "${needle}" "${listing}"; then
            warn "initramfs still lists removed module ${needle}"
            rc=1
        fi
    done < <(module_filenames)
    rm -f -- "${listing}"
    return "${rc}"
}

resolve_nvidia_module() {
    resolve_module_file nvidia nvidia.ko
}

require_stock_module_path() {
    local module_path="$1"
    local module_root="$2"
    local target="$3"
    local canonical

    [[ -f "${module_path}" ]] || die "resolved predecessor module is missing: ${module_path}"
    canonical="$(readlink -f -- "${module_path}")" || die "cannot canonicalize predecessor module"
    [[ "${canonical}" == "${module_path}" ]] || \
        die "predecessor module path is not canonical: ${module_path}"
    case "${module_path}" in
        "${module_root}"/*) ;;
        *) die "resolved predecessor is outside ${module_root}: ${module_path}" ;;
    esac
    case "${module_path}" in
        "${target}"/*) die "the experiment is already the resolved module; remove it before reinstalling" ;;
    esac
}

require_same_filesystem() {
    local first="$1"
    local second="$2"
    [[ "$(stat -c '%d' -- "${first}")" == "$(stat -c '%d' -- "${second}")" ]] || \
        die "atomic rename requires the same filesystem: ${first} and ${second}"
}

require_root_owned_state_directory() {
    local directory="$1"
    [[ -d "${directory}" && ! -L "${directory}" ]] || die "state directory is missing or symlinked"
    [[ "$(stat -c '%u' -- "${directory}")" == "0" ]] || die "state directory is not root-owned"
    [[ "$(stat -c '%a' -- "${directory}")" == "700" ]] || die "state directory permissions are not 0700"
}

require_secure_operation_lock_file() {
    local lock_file="$1"
    local mode

    [[ -f "${lock_file}" && ! -L "${lock_file}" ]] || \
        die "operation lock is missing, non-regular, or symlinked: ${lock_file}"
    [[ "$(stat -c '%u' -- "${lock_file}")" == "0" ]] || \
        die "operation lock is not root-owned: ${lock_file}"
    [[ "$(stat -c '%h' -- "${lock_file}")" == "1" ]] || \
        die "operation lock must have exactly one link: ${lock_file}"
    mode="$(stat -c '%a' -- "${lock_file}")"
    mode_is_not_group_or_world_writable "${mode}" || \
        die "operation lock is group/world writable: ${lock_file}"
    [[ "${mode}" == "600" ]] || \
        die "operation lock permissions are not 0600: ${lock_file}"
}

acquire_operation_lock() {
    local state_root="$1"
    local allow_state_root_create="${2:-0}"
    local state_parent lock_file old_umask path_identity fd_identity

    require_root
    require_command flock
    require_command mkdir
    require_command stat

    [[ "${state_root}" == "/var/lib/${PROJECT_ID}" ]] || \
        die "refusing unexpected operation-lock root: ${state_root}"
    [[ "${allow_state_root_create}" == "0" || \
       "${allow_state_root_create}" == "1" ]] || \
        die "invalid state-root creation policy"
    [[ -z "${CMPUNLOCKER_OPERATION_LOCK_FD:-}" ]] || \
        die "operation lock is already held by this process"

    state_parent="$(dirname -- "${state_root}")"
    [[ "${state_parent}" == "/var/lib" ]] || \
        die "unexpected operation-lock parent: ${state_parent}"
    [[ -d "${state_parent}" && ! -L "${state_parent}" ]] || \
        die "operation-lock parent is missing or symlinked: ${state_parent}"
    [[ "$(stat -c '%u' -- "${state_parent}")" == "0" ]] || \
        die "operation-lock parent is not root-owned: ${state_parent}"
    mode_is_not_group_or_world_writable "$(stat -c '%a' -- "${state_parent}")" || \
        die "operation-lock parent is group/world writable: ${state_parent}"

    if [[ ! -e "${state_root}" && ! -L "${state_root}" ]]; then
        [[ "${allow_state_root_create}" == "1" ]] || \
            die "state root is missing: ${state_root}"
        old_umask="$(umask)"
        umask 077
        mkdir -m 0700 -- "${state_root}" 2>/dev/null || true
        umask "${old_umask}"
    fi
    require_root_owned_state_directory "${state_root}"

    lock_file="${state_root}/operation.lock"
    if [[ -e "${lock_file}" || -L "${lock_file}" ]]; then
        require_secure_operation_lock_file "${lock_file}"
    fi

    old_umask="$(umask)"
    umask 077
    if ! exec {CMPUNLOCKER_OPERATION_LOCK_FD}>>"${lock_file}"; then
        umask "${old_umask}"
        die "cannot open operation lock: ${lock_file}"
    fi
    umask "${old_umask}"

    require_secure_operation_lock_file "${lock_file}"
    path_identity="$(stat -c '%d:%i' -- "${lock_file}")"
    fd_identity="$(stat -Lc '%d:%i' -- \
        "/proc/self/fd/${CMPUNLOCKER_OPERATION_LOCK_FD}")" || \
        die "cannot identify the opened operation lock"
    [[ "${path_identity}" == "${fd_identity}" ]] || \
        die "operation lock changed while it was opened"

    if ! flock --exclusive --nonblock "${CMPUNLOCKER_OPERATION_LOCK_FD}"; then
        exec {CMPUNLOCKER_OPERATION_LOCK_FD}>&-
        die "another install, remove, confirmation, or validation operation is active"
    fi

    # Revalidate both pathname and descriptor after ownership of the lock.
    require_root_owned_state_directory "${state_root}"
    require_secure_operation_lock_file "${lock_file}"
    path_identity="$(stat -c '%d:%i' -- "${lock_file}")"
    fd_identity="$(stat -Lc '%d:%i' -- \
        "/proc/self/fd/${CMPUNLOCKER_OPERATION_LOCK_FD}")" || \
        die "cannot revalidate the opened operation lock"
    [[ "${path_identity}" == "${fd_identity}" ]] || \
        die "operation lock changed after acquisition"

    CMPUNLOCKER_OPERATION_LOCK_PATH="${lock_file}"
    info "Acquired exclusive operation lock: ${lock_file}"
}
