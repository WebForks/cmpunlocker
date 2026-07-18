#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only

set -Eeuo pipefail
IFS=$'\n\t'

usage() {
    cat <<'EOF'
Usage: ./install.sh

Create or update this repository's .venv, install cmpunlock in editable mode,
and run an offline profile-list smoke test.

Environment:
  CMPUNLOCK_PYTHON   Python interpreter to use (default: python3)

The installer never runs the live unlock, changes firmware, unloads drivers,
or installs a daemon. Python 3.10 or newer is required.
EOF
}

fail() {
    printf 'error: %s\n' "$*" >&2
    exit 1
}

info() {
    printf '==> %s\n' "$*"
}

if (( $# == 1 )) && [[ "$1" == "-h" || "$1" == "--help" ]]; then
    usage
    exit 0
fi
if (( $# != 0 )); then
    usage >&2
    exit 2
fi

REPO_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
VENV_DIR="${REPO_DIR}/.venv"
PYTHON_BIN="${CMPUNLOCK_PYTHON:-python3}"

[[ -f "${REPO_DIR}/pyproject.toml" ]] || fail "pyproject.toml is missing from ${REPO_DIR}"
[[ -f "${REPO_DIR}/cmpunlock/cli.py" ]] || fail "cmpunlock sources are missing"

if (( EUID == 0 )) && [[ -n "${SUDO_USER:-}" ]]; then
    fail "do not run this installer with sudo; run ./install.sh as ${SUDO_USER}"
fi
if (( EUID == 0 )); then
    printf 'warning: installing as root is unnecessary; live commands alone need root\n' >&2
fi

command -v "${PYTHON_BIN}" >/dev/null 2>&1 || fail "Python not found: ${PYTHON_BIN}"
if ! PYTHON_VERSION="$("${PYTHON_BIN}" -c '
import sys
if sys.version_info < (3, 10):
    raise SystemExit(1)
print(".".join(map(str, sys.version_info[:3])))
')"; then
    fail "${PYTHON_BIN} must be Python 3.10 or newer"
fi
info "Using ${PYTHON_BIN} ${PYTHON_VERSION}"

if [[ -e "${VENV_DIR}" ]]; then
    [[ -f "${VENV_DIR}/pyvenv.cfg" && -x "${VENV_DIR}/bin/python" ]] || fail \
        "${VENV_DIR} exists but is not a complete POSIX virtual environment; repair or move it"
    info "Reusing ${VENV_DIR}"
else
    info "Creating ${VENV_DIR}"
    if ! "${PYTHON_BIN}" -m venv "${VENV_DIR}"; then
        fail "virtual environment creation failed; install your distro's python3-venv package"
    fi
fi

VENV_PYTHON="${VENV_DIR}/bin/python"
CMPUNLOCK="${VENV_DIR}/bin/cmpunlock"

info "Installing cmpunlock"
if ! "${VENV_PYTHON}" -m pip install \
    --disable-pip-version-check \
    --no-deps \
    --editable "${REPO_DIR}"; then
    fail "package installation failed; the first build needs setuptools>=77 from the network or a local wheel cache"
fi

info "Running offline smoke test"
if ! "${CMPUNLOCK}" profile list >/dev/null; then
    fail "offline profile-list smoke test failed"
fi

printf '\nInstalled successfully.\n'
printf 'CLI: %s\n' "${CMPUNLOCK}"
printf 'Next safe check: %q profile list\n' "${CMPUNLOCK}"
printf 'No GPU, driver, firmware, service, or systemd changes were made.\n'
