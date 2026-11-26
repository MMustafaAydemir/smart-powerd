#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

TARGET_USER=""
NO_AUTOSTART=0
NO_START=0
SKIP_DEPS=0

usage() {
  cat <<'EOF'
Usage: sudo ./install.sh [OPTIONS]

Install smart-powerd, shared Python modules, CLI, system tray, systemd unit,
and validated sudoers rules.

Options:
  --user <name>    Desktop user to receive tray autostart entry
  --no-autostart   Skip installing ~/.config/autostart/smart-power-tray.desktop
  --no-start       Enable smart-powerd.service without starting it immediately
  --skip-deps      Skip required dependency check
  -h, --help       Show this help message and exit
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --user)
      if [[ $# -lt 2 || -z "$2" ]]; then
        echo "Error: --user requires a username." >&2
        exit 2
      fi
      TARGET_USER="$2"
      shift 2
      ;;
    --no-autostart)
      NO_AUTOSTART=1
      shift
      ;;
    --no-start)
      NO_START=1
      shift
      ;;
    --skip-deps)
      SKIP_DEPS=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Error: unknown option '$1'." >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ "${EUID}" -ne 0 ]]; then
  echo "install.sh must run as root (use sudo or pkexec)." >&2
  exit 1
fi

find_bin() {
  local name="$1"
  local found=""
  if found="$(command -v "${name}" 2>/dev/null)"; then
    echo "${found}"
    return 0
  fi
  for dir in /usr/bin /usr/sbin /bin /sbin /usr/local/bin /usr/local/sbin; do
    if [[ -x "${dir}/${name}" ]]; then
      echo "${dir}/${name}"
      return 0
    fi
  done
  return 1
}

PYTHON_BIN="/usr/bin/python3"
if [[ ! -x "${PYTHON_BIN}" ]]; then
  PYTHON_BIN="$(find_bin python3 || true)"
fi
VISUDO_BIN="$(find_bin visudo || true)"
IW_BIN="$(find_bin iw || true)"
SYSTEMCTL_BIN="$(find_bin systemctl || true)"

# ---------------------------------------------------------------------------
# Dependency checks
# ---------------------------------------------------------------------------
MISSING_REQ=()
if [[ -z "${PYTHON_BIN}" || ! -x "${PYTHON_BIN}" ]]; then
  MISSING_REQ+=("python3 (>= 3.11)")
elif ! "${PYTHON_BIN}" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
  PY_VER="$("${PYTHON_BIN}" -c 'import sys; print(".".join(map(str, sys.version_info[:3])))' 2>/dev/null || echo unknown)"
  MISSING_REQ+=("python3 >= 3.11 (found ${PY_VER})")
else
  if ! "${PYTHON_BIN}" -c 'import PyQt6' >/dev/null 2>&1; then
    MISSING_REQ+=("PyQt6 (Python module)")
  fi
  if ! "${PYTHON_BIN}" -c 'import dbus' >/dev/null 2>&1; then
    MISSING_REQ+=("dbus-python (Python module)")
  fi
fi

if [[ -z "${IW_BIN}" ]]; then
  MISSING_REQ+=("iw")
fi
if [[ -z "${VISUDO_BIN}" ]]; then
  MISSING_REQ+=("visudo (sudo)")
fi
if [[ -z "${SYSTEMCTL_BIN}" ]]; then
  MISSING_REQ+=("systemctl (systemd)")
fi

if [[ "${#MISSING_REQ[@]}" -gt 0 && "${SKIP_DEPS}" -eq 0 ]]; then
  echo "Error: missing required dependencies:" >&2
  for dep in "${MISSING_REQ[@]}"; do
    echo "  - ${dep}" >&2
  done
  cat >&2 <<'EOF'

Install required packages for your distribution:
  Arch / CachyOS / EndeavourOS:
    sudo pacman -S python python-pyqt6 python-dbus iw sudo
  Fedora:
    sudo dnf install python3 python3-pyqt6 python3-dbus iw sudo
  Debian / Ubuntu:
    sudo apt install python3 python3-pyqt6 python3-dbus iw sudo
  openSUSE:
    sudo zypper install python311 python311-qt6 python311-dbus-python iw sudo

(Or re-run with --skip-deps to bypass this check.)
EOF
  exit 1
fi

# Informational optional dependency report
OPT_PRESENT=()
OPT_MISSING=()
for opt_cmd in kscreen-doctor wlr-randr gdctl gnome-randr xrandr brightnessctl; do
  if find_bin "${opt_cmd}" >/dev/null 2>&1; then
    OPT_PRESENT+=("${opt_cmd}")
  else
    OPT_MISSING+=("${opt_cmd}")
  fi
done

echo "Optional tools detected: ${OPT_PRESENT[*]:-none}"
if [[ "${#OPT_MISSING[@]}" -gt 0 ]]; then
  echo "Optional tools not found (only needed for matching DE/features): ${OPT_MISSING[*]}"
fi

# ---------------------------------------------------------------------------
# Verify source files exist before modifying the system
# ---------------------------------------------------------------------------
REQUIRED_SOURCES=(
  "smart_power_config.py"
  "adaptive_brightness.py"
  "display_refresh.py"
  "smart-powerd.py"
  "smart-powerd-helper.py"
  "smart-powerctl.py"
  "smart-power-tray.py"
  "uninstall.sh"
  "config.example.toml"
  "smart-powerd.service"
  "smart-powerd.sudoers"
  "smart-power-tray.desktop"
  "99-smart-powerd.types"
  "99-smart-powerd.rules"
)

for src in "${REQUIRED_SOURCES[@]}"; do
  if [[ ! -f "${SCRIPT_DIR}/${src}" ]]; then
    echo "Error: required source file not found: ${SCRIPT_DIR}/${src}" >&2
    exit 1
  fi
done

# ---------------------------------------------------------------------------
# Dynamically resolve target desktop user (no hardcoded usernames)
# ---------------------------------------------------------------------------
resolve_desktop_user() {
  local candidate=""

  if [[ -n "${TARGET_USER}" ]]; then
    if ! id "${TARGET_USER}" >/dev/null 2>&1; then
      echo "Error: specified --user '${TARGET_USER}' does not exist." >&2
      exit 1
    fi
    echo "${TARGET_USER}"
    return 0
  fi

  if [[ -n "${PKEXEC_UID:-}" && "${PKEXEC_UID}" != "0" ]]; then
    candidate="$(getent passwd "${PKEXEC_UID}" | cut -d: -f1 || true)"
    if [[ -n "${candidate}" && "${candidate}" != "root" && "${candidate}" != "nobody" ]] && id "${candidate}" >/dev/null 2>&1; then
      echo "${candidate}"
      return 0
    fi
  fi

  if [[ -n "${SUDO_USER:-}" && "${SUDO_USER}" != "root" && "${SUDO_USER}" != "nobody" ]]; then
    if id "${SUDO_USER}" >/dev/null 2>&1; then
      echo "${SUDO_USER}"
      return 0
    fi
  fi

  if command -v loginctl >/dev/null 2>&1; then
    while read -r uid uname _rest; do
      if [[ "${uid}" =~ ^[0-9]+$ ]] && (( uid >= 1000 && uid < 65534 )) && [[ -n "${uname}" && "${uname}" != "nobody" ]]; then
        if id "${uname}" >/dev/null 2>&1; then
          echo "${uname}"
          return 0
        fi
      fi
    done < <(loginctl list-users --no-legend 2>/dev/null || true)
  fi

  while IFS=: read -r uname _pw uid _gid _gecos home shell; do
    if [[ "${uid}" =~ ^[0-9]+$ ]] && (( uid >= 1000 && uid < 65534 )) \
       && [[ "${uname}" != "nobody" && -d "${home}" ]] \
       && [[ "${shell}" != */nologin && "${shell}" != */false ]]; then
      echo "${uname}"
      return 0
    fi
  done < <(getent passwd)

  return 0
}

TARGET_USER="$(resolve_desktop_user)"
TARGET_GROUP=""
USER_HOME=""
if [[ -n "${TARGET_USER}" ]]; then
  TARGET_GROUP="$(id -gn "${TARGET_USER}")"
  USER_HOME="$(getent passwd "${TARGET_USER}" | cut -d: -f6)"
fi

# ---------------------------------------------------------------------------
# Generate and validate portable sudoers fragment BEFORE touching /etc
# ---------------------------------------------------------------------------
HELPER_PATH="/usr/local/libexec/smart-powerd-helper"
HELPER_CMDS=(
  on
  off
  toggle
  reload
  brightness-on
  brightness-off
  brightness-toggle
  charge-limit-on
  charge-limit-off
  charge-limit-toggle
)

ADMIN_GROUPS=()
for grp in wheel sudo; do
  if getent group "${grp}" >/dev/null 2>&1; then
    ADMIN_GROUPS+=("%${grp}")
  fi
done

if [[ "${#ADMIN_GROUPS[@]}" -eq 0 ]]; then
  if [[ -n "${TARGET_USER}" ]]; then
    ADMIN_GROUPS=("${TARGET_USER}")
  else
    ADMIN_GROUPS=("%wheel")
  fi
fi

TMP_SUDOERS="$(mktemp)"
trap 'rm -f "${TMP_SUDOERS}"' EXIT

{
  echo "# Generated by smart-power install.sh — passwordless allowlisted controls."
  for principal in "${ADMIN_GROUPS[@]}"; do
    printf '%s ALL=(root) NOPASSWD: %s\n' "${principal}" '\'
    for i in "${!HELPER_CMDS[@]}"; do
      if (( i < ${#HELPER_CMDS[@]} - 1 )); then
        printf '    %s %s, %s\n' "${HELPER_PATH}" "${HELPER_CMDS[$i]}" '\'
      else
        printf '    %s %s\n' "${HELPER_PATH}" "${HELPER_CMDS[$i]}"
      fi
    done
  done
} > "${TMP_SUDOERS}"

if [[ -n "${VISUDO_BIN}" ]]; then
  "${VISUDO_BIN}" -cf "${SCRIPT_DIR}/smart-powerd.sudoers" >/dev/null
  "${VISUDO_BIN}" -cf "${TMP_SUDOERS}" >/dev/null
else
  echo "Warning: visudo not found (--skip-deps); skipping sudoers syntax check." >&2
fi

# ---------------------------------------------------------------------------
# Capture portable pre-install hardware baseline (only if missing; never wakes dGPU)
# ---------------------------------------------------------------------------
TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP_DIR="/var/lib/smart-powerd/backups/${TIMESTAMP}"
mkdir -p "${BACKUP_DIR}" /var/lib/smart-powerd /run/smart-powerd

BASELINE_FILE="/var/lib/smart-powerd/baseline.json"
"${PYTHON_BIN}" - "${BASELINE_FILE}" "${IW_BIN}" <<'PYEOF'
import glob
import json
import shutil
import subprocess
import sys
from pathlib import Path

baseline_path = Path(sys.argv[1])
iw_bin = sys.argv[2] or shutil.which("iw") or "/usr/bin/iw"


def read_text(path: Path, default: str | None = None) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="ignore").strip()
    except OSError:
        return default


def capture_charge_limit_baseline() -> dict[str, str | None] | None:
    bat_dir: Path | None = None
    ps_root = Path("/sys/class/power_supply")
    if ps_root.is_dir():
        candidates = sorted(ps_root.iterdir(), key=lambda p: (not p.name.startswith("BAT"), p.name))
        for entry in candidates:
            if (read_text(entry / "type", "") or "").lower() == "battery":
                bat_dir = entry
                break

    if bat_dir is not None:
        end_p = bat_dir / "charge_control_end_threshold"
        if end_p.is_file():
            val = read_text(end_p)
            if val is not None:
                start_p = bat_dir / "charge_control_start_threshold"
                s_val = read_text(start_p) if start_p.is_file() else None
                return {
                    "backend": "charge_control_end_threshold",
                    "path": str(end_p),
                    "value": val,
                    "start_path": str(start_p) if s_val is not None else None,
                    "start_value": s_val,
                }

        stop_p = bat_dir / "charge_stop_threshold"
        if stop_p.is_file():
            val = read_text(stop_p)
            if val is not None:
                start_p = bat_dir / "charge_start_threshold"
                s_val = read_text(start_p) if start_p.is_file() else None
                return {
                    "backend": "charge_stop_threshold",
                    "path": str(stop_p),
                    "value": val,
                    "start_path": str(start_p) if s_val is not None else None,
                    "start_value": s_val,
                }

        ct_p = bat_dir / "charge_types"
        if ct_p.is_file():
            raw_ct = read_text(ct_p, "") or ""
            tokens = [t.strip("[]") for t in raw_ct.split() if t.strip("[]")]
            if "Long_Life" in tokens and "Standard" in tokens:
                active = None
                for tok in raw_ct.split():
                    if tok.startswith("[") and tok.endswith("]"):
                        active = tok[1:-1]
                        break
                if active is None and tokens:
                    active = tokens[0]
                return {
                    "backend": "charge_types",
                    "path": str(ct_p),
                    "value": active,
                    "start_path": None,
                    "start_value": None,
                }

    for cm in sorted(glob.glob("/sys/bus/platform/drivers/ideapad_acpi/*/conservation_mode")):
        cm_p = Path(cm)
        val = read_text(cm_p)
        if val is not None:
            return {
                "backend": "conservation_mode",
                "path": str(cm_p),
                "value": val,
                "start_path": None,
                "start_value": None,
            }

    lg_p = Path("/sys/devices/platform/lg-laptop/battery_care_limit")
    if lg_p.is_file():
        val = read_text(lg_p)
        if val is not None:
            return {
                "backend": "battery_care_limit",
                "path": str(lg_p),
                "value": val,
                "start_path": None,
                "start_value": None,
            }

    sam_p = Path("/sys/devices/platform/samsung/battery_life_extender")
    if sam_p.is_file():
        val = read_text(sam_p)
        if val is not None:
            return {
                "backend": "battery_life_extender",
                "path": str(sam_p),
                "value": val,
                "start_path": None,
                "start_value": None,
            }

    return None


if baseline_path.is_file():
    try:
        existing = json.loads(baseline_path.read_text(encoding="utf-8", errors="ignore"))
    except ValueError:
        existing = None
    if isinstance(existing, dict):
        if "charge_limit_baseline" not in existing:
            existing["charge_limit_baseline"] = capture_charge_limit_baseline()
            baseline_path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
            baseline_path.chmod(0o644)
        sys.exit(0)

aspm = "default"
raw_aspm = read_text(Path("/sys/module/pcie_aspm/parameters/policy"), "") or ""
for tok in raw_aspm.split():
    if tok.startswith("[") and tok.endswith("]"):
        aspm = tok[1:-1]
        break

audio_ps = read_text(Path("/sys/module/snd_hda_intel/parameters/power_save"), "0") or "0"

wifi_ps = "on"
net_root = Path("/sys/class/net")
if net_root.is_dir() and iw_bin and Path(iw_bin).exists():
    for net_dev in sorted(net_root.iterdir()):
        if (net_dev / "wireless").is_dir():
            res = subprocess.run(
                [iw_bin, "dev", net_dev.name, "get", "power_save"],
                capture_output=True,
                text=True,
                check=False,
            )
            if res.returncode == 0 and "off" in res.stdout.lower():
                wifi_ps = "off"
            break

dirty_writeback = read_text(Path("/proc/sys/vm/dirty_writeback_centisecs"), "500") or "500"
nmi_watchdog = read_text(Path("/proc/sys/kernel/nmi_watchdog"), "0") or "0"
platform_profile = read_text(Path("/sys/firmware/acpi/platform_profile"), None)

sata_alpm: dict[str, str] = {}
scsi_root = Path("/sys/class/scsi_host")
if scsi_root.is_dir():
    for host in sorted(scsi_root.iterdir()):
        pol = read_text(host / "link_power_management_policy")
        if pol:
            sata_alpm[host.name] = pol

amd_abm: dict[str, str] = {}
drm_root = Path("/sys/class/drm")
if drm_root.is_dir():
    for conn in sorted(drm_root.glob("card*-eDP-*")):
        val = read_text(conn / "amdgpu/panel_power_savings")
        if val is not None and val != "":
            amd_abm[conn.name] = val

kbd_backlight: dict[str, str] = {}
leds_root = Path("/sys/class/leds")
if leds_root.is_dir():
    for led in sorted(leds_root.glob("*kbd_backlight*")):
        val = read_text(led / "brightness")
        if val is not None and val != "":
            kbd_backlight[led.name] = val

# Dynamically discover Ethernet (0x0200) and SD card reader (0x0805) PCI devices.
# Reading sysfs class metadata never wakes a suspended discrete GPU.
pci_controls: dict[str, str] = {}
pci_root = Path("/sys/bus/pci/devices")
if pci_root.is_dir():
    for dev in sorted(pci_root.iterdir()):
        cls = (read_text(dev / "class", "") or "").lower()
        if cls.startswith("0x0200") or cls.startswith("0x0805"):
            ctrl = read_text(dev / "power/control")
            if ctrl:
                pci_controls[dev.name] = ctrl

# Non-hub, non-HID (03), non-Wireless/BT (e0), non-Audio (01) USB devices.
usb_controls: dict[str, str] = {}
skip_classes = {"03", "e0", "01"}
usb_root = Path("/sys/bus/usb/devices")
if usb_root.is_dir():
    for entry in sorted(usb_root.iterdir()):
        if not (entry / "idVendor").is_file():
            continue
        dev_cls = (read_text(entry / "bDeviceClass", "") or "").lower()
        if dev_cls == "09" or dev_cls in skip_classes:
            continue
        iface_classes: set[str] = set()
        prefix = entry.name + ":"
        for child in entry.iterdir():
            if child.name.startswith(prefix) and child.is_dir():
                icls = read_text(child / "bInterfaceClass")
                if icls:
                    iface_classes.add(icls.lower())
        if iface_classes & skip_classes:
            continue
        ctrl = read_text(entry / "power/control")
        if ctrl:
            usb_controls[entry.name] = ctrl

data = {
    "pcie_aspm": aspm,
    "snd_hda_power_save": audio_ps,
    "wifi_power_save": wifi_ps,
    "dirty_writeback_centisecs": dirty_writeback,
    "nmi_watchdog": nmi_watchdog,
    "platform_profile": platform_profile,
    "sata_alpm": sata_alpm,
    "amd_abm": amd_abm,
    "kbd_backlight": kbd_backlight,
    "pci_controls": pci_controls,
    "usb_controls": usb_controls,
    "charge_limit_baseline": capture_charge_limit_baseline(),
}
baseline_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
baseline_path.chmod(0o644)
PYEOF

# ---------------------------------------------------------------------------
# Back up existing installed files
# ---------------------------------------------------------------------------
backup_if_exists() {
  local dest="$1"
  if [[ -e "${dest}" || -L "${dest}" ]]; then
    local rel="${dest#/}"
    mkdir -p "${BACKUP_DIR}/$(dirname "${rel}")"
    cp -a "${dest}" "${BACKUP_DIR}/${rel}"
  fi
}

TARGETS=(
  "/usr/local/lib/smart-power/smart_power_config.py"
  "/usr/local/lib/smart-power/adaptive_brightness.py"
  "/usr/local/lib/smart-power/display_refresh.py"
  "/usr/local/sbin/smart-powerd"
  "/usr/local/sbin/smart-power-uninstall"
  "/usr/local/libexec/smart-powerd-helper"
  "/usr/local/bin/smart-powerctl"
  "/usr/local/bin/smart-power-tray"
  "/etc/smart-powerd/config.toml"
  "/etc/systemd/system/smart-powerd.service"
  "/etc/sudoers.d/smart-powerd"
  "/etc/ananicy.d/99-smart-powerd.types"
  "/etc/ananicy.d/99-smart-powerd.rules"
  "/usr/local/share/applications/smart-power-tray.desktop"
)

AUTOSTART_DIR=""
AUTOSTART_FILE=""
if [[ -n "${USER_HOME}" && "${NO_AUTOSTART}" -eq 0 ]]; then
  AUTOSTART_DIR="${USER_HOME}/.config/autostart"
  AUTOSTART_FILE="${AUTOSTART_DIR}/smart-power-tray.desktop"
  TARGETS+=("${AUTOSTART_FILE}")
fi

for t in "${TARGETS[@]}"; do
  backup_if_exists "${t}"
done

# ---------------------------------------------------------------------------
# Install files
# ---------------------------------------------------------------------------
install -d -m 0755 \
  /usr/local/lib/smart-power \
  /usr/local/sbin \
  /usr/local/libexec \
  /usr/local/bin \
  /usr/local/share/applications \
  /etc/smart-powerd

if [[ ! -d /etc/sudoers.d ]]; then
  install -d -m 0750 /etc/sudoers.d
fi

# Shared Python library modules
install -m 0644 -o root -g root "${SCRIPT_DIR}/smart_power_config.py" /usr/local/lib/smart-power/smart_power_config.py
install -m 0644 -o root -g root "${SCRIPT_DIR}/adaptive_brightness.py" /usr/local/lib/smart-power/adaptive_brightness.py
install -m 0644 -o root -g root "${SCRIPT_DIR}/display_refresh.py" /usr/local/lib/smart-power/display_refresh.py

# Executables
install -m 0755 -o root -g root "${SCRIPT_DIR}/smart-powerd.py" /usr/local/sbin/smart-powerd
install -m 0755 -o root -g root "${SCRIPT_DIR}/uninstall.sh" /usr/local/sbin/smart-power-uninstall
install -m 0755 -o root -g root "${SCRIPT_DIR}/smart-powerd-helper.py" /usr/local/libexec/smart-powerd-helper
install -m 0755 -o root -g root "${SCRIPT_DIR}/smart-powerctl.py" /usr/local/bin/smart-powerctl
install -m 0755 -o root -g root "${SCRIPT_DIR}/smart-power-tray.py" /usr/local/bin/smart-power-tray

# Configuration (preserve existing user edits across upgrades)
if [[ ! -f /etc/smart-powerd/config.toml ]]; then
  install -m 0644 -o root -g root "${SCRIPT_DIR}/config.example.toml" /etc/smart-powerd/config.toml
  echo "Installed default configuration to /etc/smart-powerd/config.toml"
else
  echo "Preserving existing /etc/smart-powerd/config.toml (new defaults available in config.example.toml)"
fi

# Systemd service & validated sudoers rule
install -m 0644 -o root -g root "${SCRIPT_DIR}/smart-powerd.service" /etc/systemd/system/smart-powerd.service
install -m 0440 -o root -g root "${TMP_SUDOERS}" /etc/sudoers.d/smart-powerd

# Optional ananicy-cpp rules (only if /etc/ananicy.d exists on target system)
if [[ -d /etc/ananicy.d ]]; then
  install -m 0644 -o root -g root "${SCRIPT_DIR}/99-smart-powerd.types" /etc/ananicy.d/99-smart-powerd.types
  install -m 0644 -o root -g root "${SCRIPT_DIR}/99-smart-powerd.rules" /etc/ananicy.d/99-smart-powerd.rules
  echo "Installed optional ananicy-cpp rules under /etc/ananicy.d/"
fi

# Desktop entry & per-user autostart
install -m 0644 -o root -g root "${SCRIPT_DIR}/smart-power-tray.desktop" /usr/local/share/applications/smart-power-tray.desktop

if [[ -n "${AUTOSTART_FILE}" && -n "${TARGET_USER}" && -n "${TARGET_GROUP}" ]]; then
  install -d -m 0755 -o "${TARGET_USER}" -g "${TARGET_GROUP}" "${USER_HOME}/.config" "${AUTOSTART_DIR}"
  install -m 0644 -o "${TARGET_USER}" -g "${TARGET_GROUP}" "${SCRIPT_DIR}/smart-power-tray.desktop" "${AUTOSTART_FILE}"
  echo "Installed tray autostart for user '${TARGET_USER}': ${AUTOSTART_FILE}"
elif [[ "${NO_AUTOSTART}" -eq 1 ]]; then
  echo "Skipped user tray autostart (--no-autostart)."
else
  echo "Note: no non-root desktop user detected; skipped per-user autostart."
fi

# Helpful note if target desktop user is not in detected sudoers admin group(s)
if [[ -n "${TARGET_USER}" ]]; then
  USER_IN_ADMIN=0
  USER_GROUPS=" $(id -Gn "${TARGET_USER}" 2>/dev/null || true) "
  for principal in "${ADMIN_GROUPS[@]}"; do
    if [[ "${principal}" == "${TARGET_USER}" ]]; then
      USER_IN_ADMIN=1
      break
    fi
    grp_name="${principal#%}"
    if [[ "${USER_GROUPS}" == *" ${grp_name} "* ]]; then
      USER_IN_ADMIN=1
      break
    fi
  done
  if [[ "${USER_IN_ADMIN}" -eq 0 ]]; then
    echo "Note: user '${TARGET_USER}' is not in ${ADMIN_GROUPS[*]}; passwordless tray/CLI toggles require membership in one of those groups."
  fi
fi

# ---------------------------------------------------------------------------
# Reload systemd, optional ananicy-cpp, and start smart-powerd
# ---------------------------------------------------------------------------
if [[ -n "${SYSTEMCTL_BIN}" ]]; then
  "${SYSTEMCTL_BIN}" daemon-reload
  if [[ -d /etc/ananicy.d ]] && (
    "${SYSTEMCTL_BIN}" is-enabled --quiet ananicy-cpp 2>/dev/null \
    || "${SYSTEMCTL_BIN}" is-active --quiet ananicy-cpp 2>/dev/null
  ); then
    "${SYSTEMCTL_BIN}" restart ananicy-cpp || true
  fi
  "${SYSTEMCTL_BIN}" enable smart-powerd.service
  if [[ "${NO_START}" -eq 0 ]]; then
    "${SYSTEMCTL_BIN}" restart smart-powerd.service
  fi
fi

echo ""
echo "smart-powerd installed successfully."
echo "  Admin sudoers principals : ${ADMIN_GROUPS[*]}"
echo "  Configuration file       : /etc/smart-powerd/config.toml"
echo "  Backups saved under      : ${BACKUP_DIR}"
echo "  Check live status        : smart-powerctl status"
echo "  Launch tray now          : nohup /usr/local/bin/smart-power-tray >/dev/null 2>&1 &"
