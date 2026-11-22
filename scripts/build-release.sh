#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: ./scripts/build-release.sh <tag>

Build clean Linux (.tar.gz) and Windows (.zip) release archives plus
SHA256SUMS.txt under dist/.

Example:
  ./scripts/build-release.sh v2.1.0
EOF
}

if [[ $# -ne 1 ]]; then
  usage >&2
  exit 2
fi

case "$1" in
  -h|--help)
    usage
    exit 0
    ;;
esac

TAG="$1"
if [[ ! "${TAG}" =~ ^[A-Za-z0-9._-]+$ || "${TAG}" == *".."* ]]; then
  echo "Error: invalid release tag '${TAG}' (allowed: letters, digits, '.', '_', '-')." >&2
  exit 2
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
DIST_DIR="${REPO_ROOT}/dist"

LINUX_FILES=(
  "smart_power_config.py"
  "adaptive_brightness.py"
  "display_refresh.py"
  "smart-powerd.py"
  "smart-powerd-helper.py"
  "smart-powerctl.py"
  "smart-power-tray.py"
  "smart-powerd.service"
  "smart-powerd.sudoers"
  "99-smart-powerd.types"
  "99-smart-powerd.rules"
  "smart-power-tray.desktop"
  "install.sh"
  "uninstall.sh"
  "config.example.toml"
  "README.md"
  "LICENSE"
  "CHANGELOG.md"
)

WINDOWS_FILES=(
  "windows/smart_power_win.py"
  "windows/install.ps1"
  "windows/uninstall.ps1"
  "smart_power_config.py"
  "adaptive_brightness.py"
  "display_refresh.py"
  "requirements-windows.txt"
  "config.example.toml"
  "README.md"
  "LICENSE"
  "CHANGELOG.md"
)

# Verify all required files exist before creating archives
MISSING=()
for rel in "${LINUX_FILES[@]}" "${WINDOWS_FILES[@]}"; do
  if [[ ! -f "${REPO_ROOT}/${rel}" ]]; then
    MISSING+=("${rel}")
  fi
done

if [[ "${#MISSING[@]}" -gt 0 ]]; then
  echo "Error: missing required release files:" >&2
  printf '  - %s\n' "${MISSING[@]}" >&2
  exit 1
fi

STAGE_DIR="$(mktemp -d)"
trap 'rm -rf "${STAGE_DIR}"' EXIT

LINUX_NAME="smart-powerd-${TAG}-linux"
WINDOWS_NAME="smart-powerd-${TAG}-windows"
LINUX_STAGE="${STAGE_DIR}/${LINUX_NAME}"
WINDOWS_STAGE="${STAGE_DIR}/${WINDOWS_NAME}"

mkdir -p "${LINUX_STAGE}" "${WINDOWS_STAGE}/windows" "${DIST_DIR}"

copy_file() {
  local src="$1"
  local dst="$2"
  mkdir -p "$(dirname -- "${dst}")"
  cp -p "${src}" "${dst}"
}

for rel in "${LINUX_FILES[@]}"; do
  copy_file "${REPO_ROOT}/${rel}" "${LINUX_STAGE}/${rel}"
done

# Ensure installer/uninstaller/executable scripts retain executable permissions
chmod 0755 \
  "${LINUX_STAGE}/install.sh" \
  "${LINUX_STAGE}/uninstall.sh" \
  "${LINUX_STAGE}/smart-powerd.py" \
  "${LINUX_STAGE}/smart-powerd-helper.py" \
  "${LINUX_STAGE}/smart-powerctl.py" \
  "${LINUX_STAGE}/smart-power-tray.py"

for rel in "${WINDOWS_FILES[@]}"; do
  copy_file "${REPO_ROOT}/${rel}" "${WINDOWS_STAGE}/${rel}"
done

LINUX_ARCHIVE="${DIST_DIR}/${LINUX_NAME}.tar.gz"
WINDOWS_ARCHIVE="${DIST_DIR}/${WINDOWS_NAME}.zip"
CHECKSUM_FILE="${DIST_DIR}/SHA256SUMS.txt"

rm -f "${LINUX_ARCHIVE}" "${WINDOWS_ARCHIVE}" "${CHECKSUM_FILE}"

# Build Linux .tar.gz archive (normalized root ownership for reproducible/portable headers)
tar --owner=0 --group=0 --numeric-owner -C "${STAGE_DIR}" -czf "${LINUX_ARCHIVE}" "${LINUX_NAME}"

# Build Windows .zip archive using Python stdlib zipfile (no external zip binary needed)
python3 - "${STAGE_DIR}" "${WINDOWS_NAME}" "${WINDOWS_ARCHIVE}" <<'PYEOF'
import sys
import zipfile
from pathlib import Path

stage_dir = Path(sys.argv[1])
root_name = sys.argv[2]
archive_path = Path(sys.argv[3])
root_dir = stage_dir / root_name

with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
    for path in sorted(root_dir.rglob("*")):
        if path.is_file():
            arcname = path.relative_to(stage_dir).as_posix()
            zf.write(path, arcname)
PYEOF

# Generate and verify SHA-256 checksums (using archive basenames for standard release verification)
(
  cd "${DIST_DIR}"
  sha256sum "${LINUX_NAME}.tar.gz" "${WINDOWS_NAME}.zip" > SHA256SUMS.txt
  sha256sum -c SHA256SUMS.txt
)

echo ""
echo "Release artifacts built under ${DIST_DIR}:"
ls -lh "${LINUX_ARCHIVE}" "${WINDOWS_ARCHIVE}" "${CHECKSUM_FILE}"
