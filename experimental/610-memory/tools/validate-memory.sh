#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
set -Eeuo pipefail

# Fail-closed orchestration for the unverified 610.43.03 memory experiment.
# This script never installs a driver, changes a register, or changes link state.
# It validates the already-loaded target, then runs only the fixed sibling CUDA
# tester at progressively larger capacities.

readonly REQUIRED_ACK='I-ACCEPT-UNVERIFIED-610-MEMORY-STRESS-AND-CONFIRM-FORCED-AIRFLOW'
readonly REQUIRED_COLD_ACK='I-CONFIRM-FULL-AC-POWER-CYCLE-AFTER-610-MEMORY-INSTALL'
readonly EXPECTED_PROJECT_ID='cmpunlocker-610-memory'
readonly EXPECTED_DRIVER_VERSION='610.43.03'
readonly EXPECTED_SOURCE_COMMIT='452cec62d827034798072827d3866d1881662b77'
readonly EXPECTED_PATCH_SHA256='f377efcb000035449a4520c3f306d0983c4de9b3dbe8a71f2ee616a5c0571c6b'
readonly EXPECTED_GSP_SIZE=29352832
readonly EXPECTED_GSP_SHA256='73065619db9ec921d19fc4e519dd04d91a9199b525eaca9b257b89fb8c5ec52c'
readonly EXPECTED_REPORTED_MIB=65536
readonly MINIMUM_PMA_BYTES=$((60 * 1024 * 1024 * 1024))
readonly -a STAGES_GIB=(8 16 32 48 60)

SCRIPT_DIR=''
TESTER=''
BDF=''
EXPECTED_MODULE_SHA256=''
ACKNOWLEDGEMENT=''
COLD_CYCLE_ACKNOWLEDGEMENT=''
OPERATION_LOCK_FD=''
PASSES=5
MAX_TEMPERATURE_C=75
LOG_ROOT='/var/log/cmp170-memory-validation'
PREFLIGHT_ONLY=0
RUN_DIR=''
ACTIVE_TESTER_PID=''
ACTIVE_TESTER_STARTTIME=''
ACTIVE_MONITOR_PID=''
ACTIVE_MONITOR_STARTTIME=''
ACTIVE_TEE_PID=''
ACTIVE_TEE_STARTTIME=''
ACTIVE_FIFO=''

usage() {
    cat <<'EOF'
Usage:
  sudo ./validate-memory.sh \
    --bdf 0000:BB:DD.F \
    --module-sha256 HEX64 \
    --operation-lock-fd FD \
    --cold-cycle-acknowledge I-CONFIRM-FULL-AC-POWER-CYCLE-AFTER-610-MEMORY-INSTALL \
    --acknowledge I-ACCEPT-UNVERIFIED-610-MEMORY-STRESS-AND-CONFIRM-FORCED-AIRFLOW \
    [--passes N] [--max-temperature-c N] [--log-root DIR]

  sudo ./validate-memory.sh \
    --bdf 0000:BB:DD.F --module-sha256 HEX64 \
    --operation-lock-fd FD \
    --cold-cycle-acknowledge I-CONFIRM-FULL-AC-POWER-CYCLE-AFTER-610-MEMORY-INSTALL \
    --preflight-only

Full validation uses the fixed sibling executable `memory-pattern-test` and the
fixed 8/16/32/48/60-GiB stages. Build it from the sibling source first:

  nvcc -O3 -std=c++17 -arch=sm_80 memory-pattern-test.cu -o memory-pattern-test

This is destructive stress testing of an unverified memory configuration. It
requires server-grade forced airflow and may crash the GPU or host. Passing can
catch address aliasing and observed corruption, but it is evidence only for the
tested patterns and duration, not a lifetime-reliability guarantee.
The default stop threshold is 75 C; `--max-temperature-c` may explicitly raise
it no higher than 90 C.

`--operation-lock-fd` is supplied by the reviewed ../validate.sh wrapper. The
descriptor must name and hold the root-owned project operation lock. Invoke the
wrapper rather than this internal tool directly.
EOF
}

fail() {
    printf 'VALIDATION_FAILED: %s\n' "$*" >&2
    exit 1
}

trim() {
    local value=$1
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    printf '%s' "$value"
}

read_colon_field() {
    local file=$1
    local wanted=$2
    local value

    value=$(awk -F: -v wanted="$wanted" '
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
    ' "$file") || fail "missing or duplicate $wanted in $file"
    printf '%s\n' "$value"
}

read_env_key() {
    local file=$1
    local wanted=$2
    local value

    value=$(awk -F= -v wanted="$wanted" '
        $1 == wanted {
            count++
            sub(/^[^=]*=/, "")
            value=$0
        }
        END { if (count != 1) exit 1; print value }
    ' "$file") || fail "missing or duplicate $wanted in $file"
    [[ $value != *$'\r'* && $value != *$'\n'* ]] ||
        fail "invalid control character in $wanted"
    printf '%s\n' "$value"
}

sha256_file() {
    local output

    output=$(sha256sum -- "$1") || fail "cannot hash $1"
    printf '%s\n' "${output%% *}"
}

mode_is_not_group_or_world_writable() {
    local mode=$1

    [[ $mode =~ ^[0-7]{3,4}$ ]] || return 1
    (( (8#$mode & 8#022) == 0 ))
}

require_trusted_directory() {
    local directory=$1

    [[ -d $directory && ! -L $directory ]] ||
        fail "trusted directory is missing, invalid, or symlinked: $directory"
    [[ $(stat -c '%u' -- "$directory") == 0 ]] ||
        fail "trusted directory is not owned by root: $directory"
    mode_is_not_group_or_world_writable "$(stat -c '%a' -- "$directory")" ||
        fail "trusted directory is group- or world-writable: $directory"
}

require_trusted_file() {
    local file=$1

    [[ -f $file && ! -L $file ]] ||
        fail "trusted file is missing, invalid, or symlinked: $file"
    [[ $(stat -c '%u' -- "$file") == 0 ]] ||
        fail "trusted file is not owned by root: $file"
    mode_is_not_group_or_world_writable "$(stat -c '%a' -- "$file")" ||
        fail "trusted file is group- or world-writable: $file"
}

module_pairs() {
    printf '%s\n' \
        'nvidia nvidia.ko nvidia' \
        'nvidia-modeset nvidia-modeset.ko nvidia_modeset' \
        'nvidia-uvm nvidia-uvm.ko nvidia_uvm' \
        'nvidia-drm nvidia-drm.ko nvidia_drm' \
        'nvidia-peermem nvidia-peermem.ko nvidia_peermem'
}

resolve_module_file() {
    local module_name=$1
    local filename=$2
    local output directive candidate remainder base resolved
    local -a matches=()

    output=$(modprobe --show-depends "$module_name" 2>/dev/null) || return 1
    while read -r directive candidate remainder; do
        [[ $directive == insmod && -n ${candidate:-} ]] || continue
        base=${candidate##*/}
        case "$base" in
            "$filename"|"$filename.gz"|"$filename.xz"|"$filename.zst"|\
            "$filename.bz2"|"$filename.lz4") matches+=("$candidate") ;;
        esac
    done <<<"$output"
    (( ${#matches[@]} == 1 )) || return 1
    resolved=$(readlink -f -- "${matches[0]}") || return 1
    [[ -f $resolved ]] || return 1
    printf '%s\n' "$resolved"
}

need_command() {
    command -v -- "$1" >/dev/null 2>&1 || fail "required command is missing: $1"
}

normalize_bdf() {
    local value=${1,,}
    if [[ $value =~ ^[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]$ ]]; then
        value="0000:$value"
    fi
    [[ $value =~ ^[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]$ ]] ||
        fail "invalid PCI BDF: $1"
    printf '%s' "$value"
}

capture_kernel_log() {
    local destination=$1
    if command -v journalctl >/dev/null 2>&1 &&
        journalctl -k -b 0 --no-pager >"$destination" 2>&1; then
        return
    fi
    if dmesg --color=never >"$destination" 2>&1; then
        return
    fi
    dmesg >"$destination" 2>&1 || fail "cannot capture the current kernel log"
}

require_kernel_pattern() {
    local log=$1
    local pattern=$2
    local description=$3
    grep -Eiq -- "$pattern" "$log" ||
        fail "missing hardened driver success marker: $description"
}

reject_kernel_failures() {
    local log=$1
    local failure_pattern=
    failure_pattern='NVRM:.*Xid|GPU has fallen off|cmpunlock610: fail |COLD_POWER_CYCLE_REQUIRED'
    if grep -Ei -- "$failure_pattern" "$log" >"$RUN_DIR/kernel-failure-markers.txt"; then
        fail "kernel log contains Xid, fallen-off-GPU, or hardened-driver failure markers"
    fi
}

read_gpu_health() {
    local label=$1
    local output="$RUN_DIR/nvidia-smi-${label}.csv"
    nvidia-smi \
        --id="$BDF" \
        --query-gpu=pci.bus_id,driver_version,memory.total,temperature.gpu \
        --format=csv,noheader,nounits >"$output" ||
        fail "nvidia-smi health query failed ($label)"

    [[ $(wc -l <"$output") -eq 1 ]] ||
        fail "nvidia-smi did not return exactly one target ($label)"

    local smi_bdf smi_version smi_memory_mib smi_temperature extra
    IFS=',' read -r smi_bdf smi_version smi_memory_mib smi_temperature extra <"$output"
    [[ -z ${extra:-} ]] || fail "unexpected nvidia-smi output shape ($label)"
    smi_bdf=$(normalize_bdf "$(trim "$smi_bdf")")
    smi_version=$(trim "$smi_version")
    smi_memory_mib=$(trim "$smi_memory_mib")
    smi_temperature=$(trim "$smi_temperature")

    [[ $smi_bdf == "$BDF" ]] || fail "nvidia-smi returned a different BDF ($label)"
    [[ $smi_version == "$EXPECTED_DRIVER_VERSION" ]] ||
        fail "nvidia-smi driver version is $smi_version, expected $EXPECTED_DRIVER_VERSION"
    [[ $smi_memory_mib =~ ^[0-9]+$ ]] ||
        fail "nvidia-smi memory.total is not an integer: $smi_memory_mib"
    (( smi_memory_mib == EXPECTED_REPORTED_MIB )) ||
        fail "nvidia-smi reports ${smi_memory_mib} MiB; exactly ${EXPECTED_REPORTED_MIB} MiB is required"
    [[ $smi_temperature =~ ^[0-9]+$ ]] ||
        fail "nvidia-smi temperature is not an integer: $smi_temperature"
    (( smi_temperature <= MAX_TEMPERATURE_C )) ||
        fail "GPU temperature ${smi_temperature} C exceeds ${MAX_TEMPERATURE_C} C ($label)"

    printf 'GPU_HEALTH label=%s memory_mib=%s temperature_c=%s\n' \
        "$label" "$smi_memory_mib" "$smi_temperature"
}

monitor_stage_temperature() {
    local tester_pid=$1
    local tester_starttime=$2
    local stage_gib=$3
    local output=$4
    local temperature

    while process_is_live "$tester_pid" "$tester_starttime"; do
        if ! temperature=$(timeout --signal=TERM --kill-after=2s 10s nvidia-smi \
            --id="$BDF" \
            --query-gpu=temperature.gpu \
            --format=csv,noheader,nounits 2>/dev/null); then
            terminate_process_bounded \
                "$tester_pid" "$tester_starttime" "CUDA tester at ${stage_gib} GiB" || true
            printf 'TEMPERATURE_MONITOR_ERROR stage_gib=%s\n' "$stage_gib" |
                tee -a "$output" || true
            return 1
        fi
        temperature=$(trim "$temperature")
        if [[ ! $temperature =~ ^[0-9]+$ ]]; then
            terminate_process_bounded \
                "$tester_pid" "$tester_starttime" "CUDA tester at ${stage_gib} GiB" || true
            printf 'TEMPERATURE_MONITOR_ERROR stage_gib=%s value=%q\n' \
                "$stage_gib" "$temperature" | tee -a "$output" || true
            return 1
        fi
        if (( temperature > MAX_TEMPERATURE_C )); then
            terminate_process_bounded \
                "$tester_pid" "$tester_starttime" "CUDA tester at ${stage_gib} GiB" || true
            printf 'TEMPERATURE_LIMIT_EXCEEDED stage_gib=%s temperature_c=%s limit_c=%s\n' \
                "$stage_gib" "$temperature" "$MAX_TEMPERATURE_C" |
                tee -a "$output" || true
            return 2
        fi
        if ! printf 'TEMPERATURE_SAMPLE stage_gib=%s temperature_c=%s limit_c=%s\n' \
            "$stage_gib" "$temperature" "$MAX_TEMPERATURE_C" |
            tee -a "$output"; then
            terminate_process_bounded \
                "$tester_pid" "$tester_starttime" "CUDA tester at ${stage_gib} GiB" || true
            return 1
        fi
        sleep 2
    done
    return 0
}

process_starttime() {
    local pid=$1
    local stat_line remainder
    local -a stat_fields=()

    [[ $pid =~ ^[1-9][0-9]*$ && -r /proc/$pid/stat ]] || return 1
    IFS= read -r stat_line <"/proc/$pid/stat" || return 1
    remainder=${stat_line##*) }
    [[ $remainder != "$stat_line" ]] || return 1
    read -r -a stat_fields <<<"$remainder"
    # remainder starts at proc stat field 3; array index 19 is field 22.
    (( ${#stat_fields[@]} > 19 )) || return 1
    [[ ${stat_fields[19]} =~ ^[0-9]+$ ]] || return 1
    printf '%s\n' "${stat_fields[19]}"
}

process_is_live() {
    local pid=$1
    local expected_starttime=$2
    local observed_starttime

    [[ $pid =~ ^[1-9][0-9]*$ && $expected_starttime =~ ^[0-9]+$ ]] || return 1
    observed_starttime=$(process_starttime "$pid") || return 1
    [[ $observed_starttime == "$expected_starttime" ]] || return 1
    kill -0 "$pid" 2>/dev/null || return 1
    if [[ -r /proc/$pid/status ]] &&
        grep -q '^State:[[:space:]]*Z' "/proc/$pid/status"; then
        return 1
    fi
    return 0
}

terminate_process_bounded() {
    local pid=$1
    local expected_starttime=$2
    local label=$3
    local attempt

    process_is_live "$pid" "$expected_starttime" || return 0
    printf 'PROCESS_TERMINATE label=%q pid=%s signal=TERM\n' "$label" "$pid"
    if process_is_live "$pid" "$expected_starttime"; then
        kill -TERM "$pid" 2>/dev/null || true
    fi
    for attempt in 1 2 3 4 5; do
        sleep 1
        process_is_live "$pid" "$expected_starttime" || return 0
    done
    printf 'PROCESS_TERMINATE label=%q pid=%s signal=KILL after_seconds=5\n' \
        "$label" "$pid"
    if process_is_live "$pid" "$expected_starttime"; then
        kill -KILL "$pid" 2>/dev/null || true
    fi
    for attempt in 1 2; do
        sleep 1
        process_is_live "$pid" "$expected_starttime" || return 0
    done
    printf 'PROCESS_STILL_LIVE label=%q pid=%s after_signal=KILL\n' \
        "$label" "$pid" >&2
    return 1
}

while (($#)); do
    case "$1" in
        --bdf)
            (($# >= 2)) || fail '--bdf requires a value'
            BDF=$2
            shift 2
            ;;
        --module-sha256)
            (($# >= 2)) || fail '--module-sha256 requires a value'
            EXPECTED_MODULE_SHA256=${2,,}
            shift 2
            ;;
        --acknowledge)
            (($# >= 2)) || fail '--acknowledge requires a value'
            ACKNOWLEDGEMENT=$2
            shift 2
            ;;
        --cold-cycle-acknowledge)
            (($# >= 2)) || fail '--cold-cycle-acknowledge requires a value'
            COLD_CYCLE_ACKNOWLEDGEMENT=$2
            shift 2
            ;;
        --operation-lock-fd)
            (($# >= 2)) || fail '--operation-lock-fd requires a value'
            OPERATION_LOCK_FD=$2
            shift 2
            ;;
        --passes)
            (($# >= 2)) || fail '--passes requires a value'
            PASSES=$2
            shift 2
            ;;
        --max-temperature-c)
            (($# >= 2)) || fail '--max-temperature-c requires a value'
            MAX_TEMPERATURE_C=$2
            shift 2
            ;;
        --log-root)
            (($# >= 2)) || fail '--log-root requires a value'
            LOG_ROOT=$2
            shift 2
            ;;
        --preflight-only)
            PREFLIGHT_ONLY=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            fail "unknown option: $1"
            ;;
    esac
done

[[ $(uname -s) == Linux ]] || fail 'this validator only runs on Linux'
(( EUID == 0 )) || fail 'run this validator as root'
[[ -n $BDF ]] || fail '--bdf is required'
BDF=$(normalize_bdf "$BDF")
[[ $EXPECTED_MODULE_SHA256 =~ ^[0-9a-f]{64}$ ]] ||
    fail '--module-sha256 must be the audited installed nvidia module SHA-256'
[[ $COLD_CYCLE_ACKNOWLEDGEMENT == "$REQUIRED_COLD_ACK" ]] ||
    fail "validation requires --cold-cycle-acknowledge $REQUIRED_COLD_ACK"
[[ $OPERATION_LOCK_FD =~ ^[0-9]+$ ]] && (( OPERATION_LOCK_FD >= 3 )) ||
    fail '--operation-lock-fd must be the inherited project-lock descriptor'
[[ $PASSES =~ ^[1-9][0-9]*$ ]] && (( PASSES >= 1 && PASSES <= 10 )) ||
    fail '--passes must be an integer from 1 through 10 without leading zeros'
[[ $MAX_TEMPERATURE_C =~ ^[1-9][0-9]*$ ]] &&
    (( MAX_TEMPERATURE_C >= 40 && MAX_TEMPERATURE_C <= 90 )) ||
    fail '--max-temperature-c must be an integer from 40 through 90 without leading zeros'

if (( PREFLIGHT_ONLY == 0 )); then
    [[ $ACKNOWLEDGEMENT == "$REQUIRED_ACK" ]] ||
        fail "full validation requires --acknowledge $REQUIRED_ACK"
    (( PASSES >= 3 )) || fail 'full validation requires at least 3 passes'
fi

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
TESTER="$SCRIPT_DIR/memory-pattern-test"

need_command modinfo
need_command modprobe
need_command nvidia-smi
need_command sha256sum
need_command readlink
need_command dmesg
need_command grep
need_command awk
need_command stat
need_command wc
need_command mktemp
need_command uname
need_command dirname
need_command basename
need_command mkdir
need_command date
need_command tee
need_command tail
need_command cp
need_command chmod
need_command timeout
need_command sleep
need_command mkfifo
need_command rm
need_command flock

operation_lock_path="/var/lib/$EXPECTED_PROJECT_ID/operation.lock"
require_trusted_directory "/var/lib/$EXPECTED_PROJECT_ID"
require_trusted_file "$operation_lock_path"
[[ $(stat -c '%a' -- "$operation_lock_path") == 600 ]] ||
    fail "operation lock must have mode 0600: $operation_lock_path"
[[ $(stat -c '%h' -- "$operation_lock_path") == 1 ]] ||
    fail "operation lock must have exactly one link: $operation_lock_path"
[[ -e /proc/self/fd/$OPERATION_LOCK_FD ]] ||
    fail 'inherited operation-lock descriptor is not open'
operation_lock_path_identity=$(stat -c '%d:%i' -- "$operation_lock_path")
operation_lock_fd_identity=$(stat -Lc '%d:%i' -- "/proc/self/fd/$OPERATION_LOCK_FD") ||
    fail 'cannot identify inherited operation-lock descriptor'
[[ $operation_lock_path_identity == "$operation_lock_fd_identity" ]] ||
    fail 'inherited descriptor does not name the project operation lock'
flock --exclusive --nonblock "$OPERATION_LOCK_FD" ||
    fail 'inherited project operation lock is not held by this validation transaction'

umask 077
case "$LOG_ROOT" in
    /*) ;;
    *) fail "log root must be an absolute path: $LOG_ROOT" ;;
esac
canonical_log_root=$(readlink -m -- "$LOG_ROOT") ||
    fail "cannot canonicalize log root: $LOG_ROOT"
[[ $canonical_log_root == "$LOG_ROOT" ]] ||
    fail "log root must be canonical and contain no symlink component: $LOG_ROOT"
if [[ -e $LOG_ROOT && -L $LOG_ROOT ]]; then
    fail "log root must not be a symlink: $LOG_ROOT"
fi
log_parent=$(dirname -- "$LOG_ROOT")
[[ -d $log_parent && ! -L $log_parent ]] ||
    fail "log-root parent must be an existing real directory: $log_parent"
[[ $(stat -c '%u' -- "$log_parent") == 0 ]] ||
    fail "log-root parent must be owned by root: $log_parent"
log_parent_mode=$(stat -c '%a' -- "$log_parent")
(( (8#$log_parent_mode & 8#022) == 0 )) ||
    fail "log-root parent must not be group- or world-writable: $log_parent"
if [[ ! -e $LOG_ROOT ]]; then
    mkdir -m 0700 -- "$LOG_ROOT"
fi
[[ -d $LOG_ROOT && ! -L $LOG_ROOT ]] || fail "invalid log root: $LOG_ROOT"
[[ $(stat -c '%u' -- "$LOG_ROOT") == 0 ]] ||
    fail "log root must be owned by root: $LOG_ROOT"
log_root_mode=$(stat -c '%a' -- "$LOG_ROOT")
(( (8#$log_root_mode & 8#077) == 0 )) ||
    fail "log root must not grant group or world access: $LOG_ROOT"
RUN_DIR=$(mktemp -d --tmpdir="$LOG_ROOT" "run-$(date -u +%Y%m%dT%H%M%SZ)-XXXXXX")
exec > >(tee -a "$RUN_DIR/validation.log") 2>&1

on_exit() {
    local status=$?
    trap - EXIT
    trap '' HUP INT TERM
    # Stop the stress workload first. Do not spend the monitor timeout budget
    # while CUDA continues touching HBM.
    terminate_process_bounded \
        "$ACTIVE_TESTER_PID" "$ACTIVE_TESTER_STARTTIME" 'CUDA tester' || true
    terminate_process_bounded \
        "$ACTIVE_MONITOR_PID" "$ACTIVE_MONITOR_STARTTIME" 'temperature monitor' || true
    terminate_process_bounded \
        "$ACTIVE_TEE_PID" "$ACTIVE_TEE_STARTTIME" 'stage log tee' || true

    if ! process_is_live "$ACTIVE_TESTER_PID" "$ACTIVE_TESTER_STARTTIME"; then
        wait "$ACTIVE_TESTER_PID" 2>/dev/null || true
    fi
    if ! process_is_live "$ACTIVE_MONITOR_PID" "$ACTIVE_MONITOR_STARTTIME"; then
        wait "$ACTIVE_MONITOR_PID" 2>/dev/null || true
    fi
    if ! process_is_live "$ACTIVE_TEE_PID" "$ACTIVE_TEE_STARTTIME"; then
        wait "$ACTIVE_TEE_PID" 2>/dev/null || true
    fi
    if [[ -n $ACTIVE_FIFO && -p $ACTIVE_FIFO ]]; then
        rm -f -- "$ACTIVE_FIFO"
    fi
    if (( status != 0 )); then
        printf 'VALIDATION_ABORTED exit_code=%d log_dir=%s\n' "$status" "$RUN_DIR"
    fi
    exit "$status"
}
trap on_exit EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

printf 'VALIDATION_START bdf=%s passes=%s log_dir=%s preflight_only=%s\n' \
    "$BDF" "$PASSES" "$RUN_DIR" "$PREFLIGHT_ONLY"

readonly SYSFS_DEVICE="/sys/bus/pci/devices/$BDF"
[[ -d $SYSFS_DEVICE ]] || fail "PCI function does not exist: $BDF"
vendor=$(<"$SYSFS_DEVICE/vendor")
device=$(<"$SYSFS_DEVICE/device")
[[ ${vendor,,} == 0x10de ]] || fail "$BDF is not an NVIDIA PCI function"
[[ ${device,,} == 0x20c2 ]] || fail "$BDF is not a CMP 170HX (expected 20c2)"
[[ -L $SYSFS_DEVICE/driver ]] || fail "$BDF is not bound to a driver"
driver=$(basename -- "$(readlink -f -- "$SYSFS_DEVICE/driver")")
[[ $driver == nvidia ]] || fail "$BDF is bound to $driver, not nvidia"

nvidia_function_count=0
for candidate in /sys/bus/pci/devices/*; do
    [[ -r $candidate/vendor ]] || continue
    candidate_vendor=$(<"$candidate/vendor")
    if [[ ${candidate_vendor,,} == 0x10de ]]; then
        ((nvidia_function_count += 1))
    fi
done
(( nvidia_function_count == 1 )) ||
    fail "validation requires exactly one NVIDIA PCI function; found $nvidia_function_count"

grep -q '^nvidia ' /proc/modules || fail 'the nvidia kernel module is not loaded'

kernel_release=$(uname -r)
module_path=$(modinfo -n nvidia) || fail 'modinfo could not locate nvidia'
module_path=$(readlink -f -- "$module_path") || fail 'cannot resolve nvidia module path'
case "$module_path" in
    "/lib/modules/$kernel_release/"*|"/usr/lib/modules/$kernel_release/"*) ;;
    *) fail "nvidia module is outside the running kernel module tree: $module_path" ;;
esac

installed_version=$(trim "$(modinfo -F version "$module_path")")
loaded_version=$(trim "$(< /sys/module/nvidia/version)")
[[ $installed_version == "$EXPECTED_DRIVER_VERSION" ]] ||
    fail "installed module version is $installed_version, expected $EXPECTED_DRIVER_VERSION"
[[ $loaded_version == "$EXPECTED_DRIVER_VERSION" ]] ||
    fail "loaded module version is $loaded_version, expected $EXPECTED_DRIVER_VERSION"

module_license=$(trim "$(modinfo -F license "$module_path")")
[[ $module_license == *MIT* && $module_license == *GPL* ]] ||
    fail "loaded module does not identify as an open MIT/GPL module"
[[ -r /proc/driver/nvidia/version ]] || fail 'missing /proc/driver/nvidia/version'
grep -q 'Open Kernel Module' /proc/driver/nvidia/version ||
    fail 'the loaded NVIDIA driver does not identify as the open kernel module'

[[ -r /sys/module/nvidia/srcversion ]] || fail 'loaded nvidia srcversion is unavailable'
installed_srcversion=$(trim "$(modinfo -F srcversion "$module_path")")
loaded_srcversion=$(trim "$(< /sys/module/nvidia/srcversion)")
[[ -n $installed_srcversion && $installed_srcversion == "$loaded_srcversion" ]] ||
    fail 'loaded nvidia srcversion differs from the installed module'

actual_module_sha256=$(sha256sum -- "$module_path")
actual_module_sha256=${actual_module_sha256%% *}
[[ $actual_module_sha256 == "$EXPECTED_MODULE_SHA256" ]] ||
    fail "nvidia module SHA-256 mismatch: $actual_module_sha256"

# Re-run and retain the wrapper's decisive provenance gates so the run
# directory is independently auditable after the invoking shell is gone.
module_root=$(readlink -f -- "/lib/modules/$kernel_release") ||
    fail "cannot canonicalize the running kernel module root"
target_dir="$module_root/updates/$EXPECTED_PROJECT_ID"
[[ $(dirname -- "$module_path") == "$target_dir" && \
   $(basename -- "$module_path") == nvidia.ko ]] ||
    fail "nvidia resolves outside the isolated experiment target: $module_path"
require_trusted_directory "$target_dir"

state_root="/var/lib/$EXPECTED_PROJECT_ID"
state_dir="$state_root/$kernel_release"
state_file="$state_dir/install.env"
require_trusted_directory "$state_root"
require_trusted_directory "$state_dir"
require_trusted_file "$state_file"
[[ $(stat -c '%a' -- "$state_file") == 600 ]] ||
    fail "install state must have mode 0600: $state_file"

[[ $(read_env_key "$state_file" schema) == 1 ]] || fail 'wrong install-state schema'
[[ $(read_env_key "$state_file" project_id) == "$EXPECTED_PROJECT_ID" ]] ||
    fail 'wrong install-state project'
[[ $(read_env_key "$state_file" kernel_release) == "$kernel_release" ]] ||
    fail 'install state belongs to another kernel'
[[ $(read_env_key "$state_file" target_dir) == "$target_dir" ]] ||
    fail 'install state names another module target'
[[ $(read_env_key "$state_file" source_commit) == "$EXPECTED_SOURCE_COMMIT" ]] ||
    fail 'install state names another NVIDIA source commit'
[[ $(read_env_key "$state_file" patch_sha256) == "$EXPECTED_PATCH_SHA256" ]] ||
    fail 'install state names another experiment patch'
[[ $(read_env_key "$state_file" nvidia_module_sha256) == "$actual_module_sha256" ]] ||
    fail 'install state names another nvidia.ko hash'
[[ $(read_env_key "$state_file" memory_capacity_verified) == false ]] ||
    fail 'install state has an unexpected capacity status'

[[ -r /proc/sys/kernel/random/boot_id ]] || fail 'current boot id is unavailable'
install_boot_id=$(read_env_key "$state_file" install_boot_id)
current_boot_id=$(< /proc/sys/kernel/random/boot_id)
[[ -n $install_boot_id && -n $current_boot_id && \
   $install_boot_id != "$current_boot_id" ]] ||
    fail 'the install and current boot IDs do not prove a post-install boot'

artifact_file="$target_dir/artifact.env"
marker_file="$target_dir/.cmpunlocker-610-memory"
checksums_file="$target_dir/checksums.sha256"
require_trusted_file "$artifact_file"
require_trusted_file "$marker_file"
require_trusted_file "$checksums_file"

[[ $(read_env_key "$artifact_file" schema) == 1 ]] || fail 'wrong artifact schema'
[[ $(read_env_key "$artifact_file" project_id) == "$EXPECTED_PROJECT_ID" ]] ||
    fail 'wrong artifact project'
[[ $(read_env_key "$artifact_file" driver_version) == "$EXPECTED_DRIVER_VERSION" ]] ||
    fail 'wrong artifact driver version'
[[ $(read_env_key "$artifact_file" source_commit) == "$EXPECTED_SOURCE_COMMIT" ]] ||
    fail 'wrong artifact source commit'
[[ $(read_env_key "$artifact_file" patch_sha256) == "$EXPECTED_PATCH_SHA256" ]] ||
    fail 'wrong artifact patch hash'
[[ $(read_env_key "$artifact_file" kernel_release) == "$kernel_release" ]] ||
    fail 'artifact belongs to another kernel'
[[ $(normalize_bdf "$(read_env_key "$artifact_file" target_bdf)") == "$BDF" ]] ||
    fail 'artifact was built for another PCI function'
[[ $(read_env_key "$artifact_file" gsp_tu10x_sha256) == "$EXPECTED_GSP_SHA256" ]] ||
    fail 'artifact names another GSP firmware image'
recorded_gsp_path=$(read_env_key "$artifact_file" gsp_tu10x_path)
case "$recorded_gsp_path" in
    /*) ;;
    *) fail 'artifact GSP path is not absolute' ;;
esac
[[ $(read_env_key "$artifact_file" memory_capacity_verified) == false ]] ||
    fail 'artifact has an unexpected capacity status'

[[ $(read_env_key "$marker_file" project_id) == "$EXPECTED_PROJECT_ID" ]] ||
    fail 'installed ownership marker names another project'
[[ $(read_env_key "$marker_file" kernel_release) == "$kernel_release" ]] ||
    fail 'installed ownership marker belongs to another kernel'
[[ $(read_env_key "$marker_file" source_commit) == "$EXPECTED_SOURCE_COMMIT" ]] ||
    fail 'installed ownership marker names another source commit'
[[ $(read_env_key "$marker_file" patch_sha256) == "$EXPECTED_PATCH_SHA256" ]] ||
    fail 'installed ownership marker names another patch'
[[ $(read_env_key "$marker_file" memory_capacity_verified) == false ]] ||
    fail 'installed ownership marker has an unexpected capacity status'

recorded_checksums_sha256=$(read_env_key "$state_file" module_checksums_sha256)
[[ $recorded_checksums_sha256 =~ ^[0-9a-f]{64}$ ]] ||
    fail 'install state contains an invalid module-manifest SHA-256'
actual_checksums_sha256=$(sha256_file "$checksums_file")
[[ $actual_checksums_sha256 == "$recorded_checksums_sha256" ]] ||
    fail 'five-module checksum manifest differs from install state'
awk '
    NF {
        count++
        if (NF != 2 || length($1) != 64 || $1 !~ /^[0-9a-f]+$/) bad=1
    }
    END { if (bad || count != 5) exit 1 }
' "$checksums_file" || fail 'module checksum manifest is malformed or not exactly five lines'

printf 'module_name\tpath\tsha256\tversion\tsrcversion\tloaded\n' \
    >"$RUN_DIR/module-provenance.tsv"
resolved_module_count=0
while read -r module_name filename sys_module; do
    resolved_module=$(resolve_module_file "$module_name" "$filename") ||
        fail "cannot resolve $module_name through modprobe metadata"
    [[ $resolved_module == "$target_dir/$filename" ]] ||
        fail "$module_name resolves outside the isolated target: $resolved_module"
    require_trusted_file "$resolved_module"
    checksum_count=$(awk -v wanted="$filename" '$2 == wanted { count++ } END { print count+0 }' \
        "$checksums_file")
    (( checksum_count == 1 )) ||
        fail "checksum manifest must contain $filename exactly once"
    expected_hash=$(awk -v wanted="$filename" '$2 == wanted { print $1 }' "$checksums_file")
    actual_hash=$(sha256_file "$resolved_module")
    [[ $actual_hash == "$expected_hash" ]] ||
        fail "resolved $module_name differs from its installed checksum"
    resolved_version=$(trim "$(modinfo -F version "$resolved_module")")
    [[ $resolved_version == "$EXPECTED_DRIVER_VERSION" ]] ||
        fail "$module_name has version $resolved_version, expected $EXPECTED_DRIVER_VERSION"
    resolved_srcversion=$(trim "$(modinfo -F srcversion "$resolved_module")")
    [[ -n $resolved_srcversion ]] || fail "$module_name has no source version"
    loaded_state=no
    if [[ -d /sys/module/$sys_module ]]; then
        loaded_state=yes
        [[ -r /sys/module/$sys_module/version && \
           -r /sys/module/$sys_module/srcversion ]] ||
            fail "loaded $module_name identity is unavailable"
        sys_version=$(trim "$(< /sys/module/$sys_module/version)")
        sys_srcversion=$(trim "$(< /sys/module/$sys_module/srcversion)")
        [[ $sys_version == "$resolved_version" && \
           $sys_srcversion == "$resolved_srcversion" ]] ||
            fail "loaded $module_name differs from the resolved installed file"
    fi
    if [[ $sys_module == nvidia || $sys_module == nvidia_uvm ]]; then
        [[ $loaded_state == yes ]] || fail "$module_name must be loaded for CUDA validation"
    fi
    modinfo "$resolved_module" >"$RUN_DIR/modinfo-${module_name}.txt"
    printf '%s\t%s\t%s\t%s\t%s\t%s\n' \
        "$module_name" "$resolved_module" "$actual_hash" "$resolved_version" \
        "$resolved_srcversion" "$loaded_state" >>"$RUN_DIR/module-provenance.tsv"
    ((resolved_module_count += 1))
done < <(module_pairs)
(( resolved_module_count == 5 )) || fail 'did not verify exactly five experiment modules'

nvidia_smi_version_file="$RUN_DIR/nvidia-smi-version.txt"
nvidia-smi --version >"$nvidia_smi_version_file" 2>&1 ||
    fail 'nvidia-smi --version failed'
nvidia_smi_version=$(read_colon_field "$nvidia_smi_version_file" 'NVIDIA-SMI version')
nvml_version=$(read_colon_field "$nvidia_smi_version_file" 'NVML version')
[[ $nvidia_smi_version == "$EXPECTED_DRIVER_VERSION" ]] ||
    fail "NVIDIA-SMI userspace is $nvidia_smi_version, expected $EXPECTED_DRIVER_VERSION"
[[ $nvml_version == "$EXPECTED_DRIVER_VERSION" ]] ||
    fail "NVML userspace is $nvml_version, expected $EXPECTED_DRIVER_VERSION"

gsp_path=$(readlink -f -- "$recorded_gsp_path") ||
    fail "cannot resolve artifact-recorded GSP path: $recorded_gsp_path"
[[ $gsp_path == "$recorded_gsp_path" ]] ||
    fail 'artifact-recorded GSP path is not canonical'
require_trusted_file "$gsp_path"
gsp_size=$(stat -c '%s' -- "$gsp_path")
gsp_sha256=$(sha256_file "$gsp_path")
(( gsp_size == EXPECTED_GSP_SIZE )) ||
    fail "on-disk GSP size is $gsp_size, expected $EXPECTED_GSP_SIZE"
[[ $gsp_sha256 == "$EXPECTED_GSP_SHA256" ]] ||
    fail "on-disk GSP SHA-256 is $gsp_sha256, expected $EXPECTED_GSP_SHA256"

install_state_sha256=$(sha256_file "$state_file")
artifact_sha256=$(sha256_file "$artifact_file")
marker_sha256=$(sha256_file "$marker_file")
cp -- "$state_file" "$RUN_DIR/install-state.env"
cp -- "$artifact_file" "$RUN_DIR/installed-artifact.env"
cp -- "$marker_file" "$RUN_DIR/installed-marker.env"
cp -- "$checksums_file" "$RUN_DIR/module-checksums.sha256"
chmod 0600 -- \
    "$RUN_DIR/install-state.env" \
    "$RUN_DIR/installed-artifact.env" \
    "$RUN_DIR/installed-marker.env" \
    "$RUN_DIR/module-checksums.sha256"

{
    printf 'project_id=%s\n' "$EXPECTED_PROJECT_ID"
    printf 'bdf=%s\n' "$BDF"
    printf 'kernel_release=%s\n' "$kernel_release"
    printf 'install_boot_id=%s\n' "$install_boot_id"
    printf 'current_boot_id=%s\n' "$current_boot_id"
    printf 'cold_cycle_acknowledgement=%s\n' "$COLD_CYCLE_ACKNOWLEDGEMENT"
    printf 'operation_lock_path=%s\n' "$operation_lock_path"
    printf 'operation_lock_identity=%s\n' "$operation_lock_path_identity"
    printf 'source_commit=%s\n' "$EXPECTED_SOURCE_COMMIT"
    printf 'patch_sha256=%s\n' "$EXPECTED_PATCH_SHA256"
    printf 'target_dir=%s\n' "$target_dir"
    printf 'install_state_path=%s\n' "$state_file"
    printf 'install_state_sha256=%s\n' "$install_state_sha256"
    printf 'artifact_sha256=%s\n' "$artifact_sha256"
    printf 'installed_marker_sha256=%s\n' "$marker_sha256"
    printf 'module_checksums_sha256=%s\n' "$actual_checksums_sha256"
    printf 'module_path=%s\n' "$module_path"
    printf 'module_sha256=%s\n' "$actual_module_sha256"
    printf 'module_version=%s\n' "$loaded_version"
    printf 'module_srcversion=%s\n' "$loaded_srcversion"
    printf 'module_license=%s\n' "$module_license"
    printf 'nvidia_smi_version=%s\n' "$nvidia_smi_version"
    printf 'nvml_version=%s\n' "$nvml_version"
    printf 'gsp_path=%s\n' "$gsp_path"
    printf 'gsp_size=%s\n' "$gsp_size"
    printf 'gsp_sha256=%s\n' "$gsp_sha256"
    printf 'validator_sha256=%s\n' "$(sha256_file "$SCRIPT_DIR/validate-memory.sh")"
} >"$RUN_DIR/provenance.env"
modinfo "$module_path" >"$RUN_DIR/nvidia.modinfo.txt"
cp /proc/driver/nvidia/version "$RUN_DIR/nvidia-proc-version.txt"

nvidia-smi --id="$BDF" -q >"$RUN_DIR/nvidia-smi-before.txt" ||
    fail 'nvidia-smi detail query failed'
read_gpu_health before
gsp_before_file="$RUN_DIR/nvidia-smi-gsp-before.txt"
nvidia-smi --id="$BDF" -q -d GSP_FIRMWARE_VERSION >"$gsp_before_file" ||
    fail 'loaded GSP firmware version query failed'
loaded_gsp_version=$(read_colon_field "$gsp_before_file" 'GSP Firmware Version')
[[ $loaded_gsp_version == "$EXPECTED_DRIVER_VERSION" ]] ||
    fail "loaded GSP firmware is $loaded_gsp_version, expected $EXPECTED_DRIVER_VERSION"
printf 'loaded_gsp_firmware_version=%s\n' "$loaded_gsp_version" \
    >>"$RUN_DIR/provenance.env"
nvidia-smi \
    --id="$BDF" \
    --query-compute-apps=pid \
    --format=csv,noheader,nounits >"$RUN_DIR/compute-pids-before.txt" ||
    fail 'cannot query active compute processes'
if grep -Eq '[0-9]+' "$RUN_DIR/compute-pids-before.txt"; then
    fail 'the target has an active compute process'
fi

kernel_before="$RUN_DIR/kernel-before.txt"
capture_kernel_log "$kernel_before"
reject_kernel_failures "$kernel_before"
require_kernel_pattern \
    "$kernel_before" \
    'cmpunlock610: gate-active device=10de:20c2' \
    '20c2 unlock gate'
for plm_name in WPR_CFG FBPA WPR FEAT; do
    require_kernel_pattern \
        "$kernel_before" \
        "cmpunlock610: plm-ok name=${plm_name}([[:space:]]|$)" \
        "PLM readback $plm_name"
done
for host_name in SS0 SS1 CFG1 LMR; do
    require_kernel_pattern \
        "$kernel_before" \
        "cmpunlock610: host-ok name=${host_name}([[:space:]]|$)" \
        "host register readback $host_name"
done
require_kernel_pattern \
    "$kernel_before" \
    'cmpunlock610: unlock-ok device=10de:20c2' \
    'completed device unlock gate'
require_kernel_pattern \
    "$kernel_before" \
    'cmpunlock610: metadata-ok bytes=0x0000001000000000 regions=[1-9][0-9]*' \
    '64-GiB metadata and nonzero region count'
require_kernel_pattern \
    "$kernel_before" \
    'cmpunlock610: pma-ok regions-before=[0-9]+ capacity=32 base=0x[0-9a-f]+ limit=0x[0-9a-f]+ total=0x[0-9a-f]+' \
    'late PMA registration and total memory'

pma_line=$(grep -Ei 'cmpunlock610: pma-ok ' "$kernel_before" | tail -n 1)
pma_total_regex='total=0x([0-9a-fA-F]+)'
[[ $pma_line =~ $pma_total_regex ]] || fail 'cannot parse hardened PMA total-memory marker'
pma_total_hex=${BASH_REMATCH[1]}
pma_total=$((16#$pma_total_hex))
(( pma_total >= MINIMUM_PMA_BYTES )) ||
    fail "late PMA total is below 60 GiB: 0x$pma_total_hex"
printf 'DRIVER_MARKERS_OK pma_total=0x%s\n' "$pma_total_hex"

if (( PREFLIGHT_ONLY == 1 )); then
    printf 'PREFLIGHT_OK log_dir=%s\n' "$RUN_DIR"
    exit 0
fi

[[ -f $TESTER && -x $TESTER && ! -L $TESTER ]] ||
    fail "build the fixed, non-symlink CUDA tester first: $TESTER"
tester_mode=$(stat -c '%a' -- "$TESTER")
(( (8#$tester_mode & 8#022) == 0 )) ||
    fail 'memory-pattern-test must not be group- or world-writable'
tester_sha256=$(sha256sum -- "$TESTER")
tester_sha256=${tester_sha256%% *}
printf 'tester_path=%s\ntester_sha256=%s\n' "$TESTER" "$tester_sha256" \
    >>"$RUN_DIR/provenance.env"

printf '%s\n' \
    'WARNING: beginning unverified full-capacity HBM stress; forced airflow was acknowledged.'

for stage_gib in "${STAGES_GIB[@]}"; do
    read_gpu_health "stage-${stage_gib}-before"
    current_tester_sha256=$(sha256sum -- "$TESTER")
    current_tester_sha256=${current_tester_sha256%% *}
    [[ $current_tester_sha256 == "$tester_sha256" ]] ||
        fail 'memory-pattern-test changed after preflight'

    stage_log="$RUN_DIR/stage-${stage_gib}gib.txt"
    temperature_log="$RUN_DIR/temperature-${stage_gib}gib.txt"
    ACTIVE_FIFO="$RUN_DIR/stage-${stage_gib}gib.fifo"
    mkfifo -m 0600 -- "$ACTIVE_FIFO"
    set +e
    tee "$stage_log" <"$ACTIVE_FIFO" &
    ACTIVE_TEE_PID=$!
    ACTIVE_TEE_STARTTIME=$(process_starttime "$ACTIVE_TEE_PID") ||
        fail 'cannot fingerprint the stage log process'
    "$TESTER" \
        --pci-bdf "$BDF" \
        --stage-gib "$stage_gib" \
        --passes "$PASSES" \
        --acknowledge "$REQUIRED_ACK" >"$ACTIVE_FIFO" 2>&1 &
    ACTIVE_TESTER_PID=$!
    ACTIVE_TESTER_STARTTIME=$(process_starttime "$ACTIVE_TESTER_PID") ||
        fail 'cannot fingerprint the CUDA tester process'
    monitor_stage_temperature \
        "$ACTIVE_TESTER_PID" "$ACTIVE_TESTER_STARTTIME" \
        "$stage_gib" "$temperature_log" &
    ACTIVE_MONITOR_PID=$!
    ACTIVE_MONITOR_STARTTIME=$(process_starttime "$ACTIVE_MONITOR_PID") ||
        fail 'cannot fingerprint the temperature monitor process'

    # The monitor must finish first. This keeps the tester PID unreaped while
    # the monitor observes it and ensures an unexpected monitor failure stops
    # the stress workload immediately.
    wait "$ACTIVE_MONITOR_PID"
    monitor_status=$?
    ACTIVE_MONITOR_PID=''
    ACTIVE_MONITOR_STARTTIME=''
    if (( monitor_status != 0 )); then
        terminate_process_bounded \
            "$ACTIVE_TESTER_PID" "$ACTIVE_TESTER_STARTTIME" \
            "CUDA tester at ${stage_gib} GiB" || true
    fi
    if process_is_live "$ACTIVE_TESTER_PID" "$ACTIVE_TESTER_STARTTIME"; then
        terminate_process_bounded \
            "$ACTIVE_TESTER_PID" "$ACTIVE_TESTER_STARTTIME" \
            "CUDA tester at ${stage_gib} GiB" || true
    fi
    if ! process_is_live "$ACTIVE_TESTER_PID" "$ACTIVE_TESTER_STARTTIME"; then
        wait "$ACTIVE_TESTER_PID"
        tester_status=$?
        ACTIVE_TESTER_PID=''
        ACTIVE_TESTER_STARTTIME=''
    else
        tester_status=125
    fi
    if (( tester_status == 125 )); then
        terminate_process_bounded \
            "$ACTIVE_TEE_PID" "$ACTIVE_TEE_STARTTIME" 'stage log tee' || true
        if process_is_live "$ACTIVE_TEE_PID" "$ACTIVE_TEE_STARTTIME"; then
            tee_status=125
        else
            wait "$ACTIVE_TEE_PID"
            tee_status=$?
            ACTIVE_TEE_PID=''
            ACTIVE_TEE_STARTTIME=''
        fi
    else
        # The tester has closed its FIFO writer; let tee drain the complete
        # stage record before validating its output contract.
        wait "$ACTIVE_TEE_PID"
        tee_status=$?
        ACTIVE_TEE_PID=''
        ACTIVE_TEE_STARTTIME=''
    fi
    rm -f -- "$ACTIVE_FIFO"
    ACTIVE_FIFO=''
    set -e
    (( tee_status == 0 )) || fail "could not record the ${stage_gib}-GiB stage log"
    (( monitor_status == 0 )) ||
        fail "temperature monitor stopped the ${stage_gib}-GiB stage (exit $monitor_status)"
    (( tester_status == 0 )) ||
        fail "CUDA memory test failed at ${stage_gib} GiB (exit $tester_status)"
    grep -q '^VRAM_VERIFY_RESULT status=PASS mismatch_count=0$' "$stage_log" ||
        fail "CUDA tester omitted its zero-mismatch result at ${stage_gib} GiB"
    grep -Eq \
        "^STAGE_START stage_gib=${stage_gib} .*free_bytes=[0-9]+ total_bytes=[0-9]+ passes=${PASSES}$" \
        "$stage_log" || fail "CUDA tester omitted cudaMemGetInfo totals at ${stage_gib} GiB"
    grep -q \
        "^STAGE_OK stage_gib=${stage_gib} passes=${PASSES} mismatch_count=0$" \
        "$stage_log" || fail "CUDA tester omitted its stage result at ${stage_gib} GiB"
    pass_ok_count=$(grep -c '^PASS_OK ' "$stage_log" || true)
    pattern_ok_count=$(grep -c '^PATTERN_OK ' "$stage_log" || true)
    (( pass_ok_count == PASSES )) ||
        fail "CUDA tester reported $pass_ok_count/$PASSES successful passes at ${stage_gib} GiB"
    (( pattern_ok_count == PASSES * 3 )) ||
        fail "CUDA tester reported $pattern_ok_count/$((PASSES * 3)) successful patterns at ${stage_gib} GiB"
    if grep -Eq '^(CUDA_ERROR|MISMATCH|VALIDATION_ERROR|VRAM_VERIFY_RESULT status=FAIL)' "$stage_log"; then
        fail "CUDA tester logged an error despite returning success at ${stage_gib} GiB"
    fi

    read_gpu_health "stage-${stage_gib}-after"
    kernel_stage="$RUN_DIR/kernel-stage-${stage_gib}gib.txt"
    capture_kernel_log "$kernel_stage"
    reject_kernel_failures "$kernel_stage"
    post_stage_tester_sha256=$(sha256sum -- "$TESTER")
    post_stage_tester_sha256=${post_stage_tester_sha256%% *}
    [[ $post_stage_tester_sha256 == "$tester_sha256" ]] ||
        fail 'memory-pattern-test changed during a stage'
    printf 'ORCHESTRATOR_STAGE_OK stage_gib=%s passes=%s patterns_per_pass=3\n' \
        "$stage_gib" "$PASSES"
done

final_kernel="$RUN_DIR/kernel-final.txt"
capture_kernel_log "$final_kernel"
reject_kernel_failures "$final_kernel"
nvidia-smi --id="$BDF" -q >"$RUN_DIR/nvidia-smi-after.txt" ||
    fail 'final nvidia-smi detail query failed'
read_gpu_health final
gsp_after_file="$RUN_DIR/nvidia-smi-gsp-after.txt"
nvidia-smi --id="$BDF" -q -d GSP_FIRMWARE_VERSION >"$gsp_after_file" ||
    fail 'final loaded GSP firmware version query failed'
final_gsp_version=$(read_colon_field "$gsp_after_file" 'GSP Firmware Version')
[[ $final_gsp_version == "$loaded_gsp_version" && \
   $final_gsp_version == "$EXPECTED_DRIVER_VERSION" ]] ||
    fail "loaded GSP firmware version changed during validation: $final_gsp_version"
final_module_sha256=$(sha256sum -- "$module_path")
final_module_sha256=${final_module_sha256%% *}
[[ $final_module_sha256 == "$EXPECTED_MODULE_SHA256" ]] ||
    fail 'installed nvidia module changed during validation'

printf 'stages_gib=8,16,32,48,60\npasses=%s\nstatus=PASS\n' "$PASSES" \
    >"$RUN_DIR/result.env"
printf '%s\n' \
    'VALIDATION_SCOPE: address aliasing/corruption was not observed; this is not a lifetime guarantee.'
printf 'LLM_READY\n'
