#!/usr/bin/env bash
set -euo pipefail

TARGET_USER=""
PURGE=0

usage() {
  cat <<'EOF'
Usage: sudo smart-power-uninstall [OPTIONS]

Stop smart-powerd and smart-power-tray, restore captured hardware defaults
(including original battery charge-limit settings), and remove installed
files. Never modifies desktop environment power settings.

Options:
  --user <name>   Desktop user whose tray & autostart entry should be removed
  --purge         Also remove /etc/smart-powerd (including config.toml)
  -h, --help      Show this help message and exit
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
    --purge)
      PURGE=1
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
  echo "uninstall.sh must run as root (use sudo or pkexec)." >&2
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
IW_BIN="$(find_bin iw || true)"

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
USER_HOME=""
if [[ -n "${TARGET_USER}" ]]; then
  USER_HOME="$(getent passwd "${TARGET_USER}" | cut -d: -f6)"
fi

# ---------------------------------------------------------------------------
# Gracefully stop tray UI (allowing native display refresh restoration on exit)
# ---------------------------------------------------------------------------
if [[ -n "${TARGET_USER}" ]]; then
  pkill -TERM -u "${TARGET_USER}" -f "/usr/local/bin/smart-power-tray" 2>/dev/null || true
else
  pkill -TERM -f "/usr/local/bin/smart-power-tray" 2>/dev/null || true
fi
sleep 0.4

if [[ -n "${USER_HOME}" ]]; then
  rm -f "${USER_HOME}/.config/autostart/smart-power-tray.desktop"
fi

# ---------------------------------------------------------------------------
# Stop and disable smart-powerd service
# ---------------------------------------------------------------------------
if command -v systemctl >/dev/null 2>&1; then
  if systemctl list-unit-files smart-powerd.service >/dev/null 2>&1; then
    systemctl disable --now smart-powerd.service 2>/dev/null || true
  fi
fi

# ---------------------------------------------------------------------------
# Restore all captured hardware baseline settings before removing state dir
# (Never wakes suspended discrete GPUs; restores original battery charge
# limit baseline; never touches desktop environment power settings.)
# ---------------------------------------------------------------------------
if [[ -n "${PYTHON_BIN}" && -x "${PYTHON_BIN}" ]]; then
  "${PYTHON_BIN}" - "${IW_BIN}" <<'PYEOF'
import glob
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

iw_bin = sys.argv[1] or shutil.which("iw") or "/usr/bin/iw"

baseline_path = Path("/var/lib/smart-powerd/baseline.json")
baseline: dict = {}
if baseline_path.is_file():
    try:
        parsed = json.loads(baseline_path.read_text(encoding="utf-8", errors="ignore"))
        if isinstance(parsed, dict):
            baseline = parsed
    except ValueError:
        baseline = {}


def read_text(path: str | Path, default: str | None = None) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8", errors="ignore").strip()
    except OSError:
        return default


def write_text(path: str | Path, val: str) -> None:
    try:
        Path(path).write_text(val, encoding="utf-8")
    except OSError:
        pass


# 1. Restore PPD / tuned-ppd to balanced, or ACPI platform_profile fallback
ppd_restored = False
try:
    import dbus

    bus = dbus.SystemBus()
    obj = bus.get_object(
        "org.freedesktop.UPower.PowerProfiles",
        "/org/freedesktop/UPower/PowerProfiles",
    )
    props = dbus.Interface(obj, "org.freedesktop.DBus.Properties")
    props.Set(
        "org.freedesktop.UPower.PowerProfiles",
        "ActiveProfile",
        dbus.String("balanced"),
    )
    ppd_restored = True
    time.sleep(0.08)
except Exception:
    pass

if not ppd_restored:
    plat = baseline.get("platform_profile")
    if isinstance(plat, str) and plat:
        write_text("/sys/firmware/acpi/platform_profile", plat)

# 2. Restore CPU boost/turbo, uncapped scaling_max_freq, and EPP balance_performance
write_text("/sys/devices/system/cpu/cpufreq/boost", "1")
write_text("/sys/devices/system/cpu/intel_pstate/no_turbo", "0")

for cdir in glob.glob("/sys/devices/system/cpu/cpu[0-9]*/cpufreq"):
    write_text(f"{cdir}/boost", "1")
    try:
        max_info = Path(f"{cdir}/cpuinfo_max_freq").read_text(encoding="utf-8").strip()
        scale_max = Path(f"{cdir}/scaling_max_freq").read_text(encoding="utf-8").strip()
        if int(scale_max) < int(max_info):
            write_text(f"{cdir}/scaling_max_freq", max_info)
    except (OSError, ValueError):
        pass

epp_files = glob.glob("/sys/devices/system/cpu/cpu[0-9]*/cpufreq/energy_performance_preference")
if epp_files:
    for epp in epp_files:
        write_text(epp, "balance_performance")
else:
    for cdir in glob.glob("/sys/devices/system/cpu/cpu[0-9]*/cpufreq"):
        avail_raw = read_text(f"{cdir}/scaling_available_governors", "") or ""
        avail = set(avail_raw.split())
        for gov in ("schedutil", "ondemand", "powersave"):
            if gov in avail:
                write_text(f"{cdir}/scaling_governor", gov)
                break

# 3. Restore PCIe ASPM and HD-Audio power save
write_text(
    "/sys/module/pcie_aspm/parameters/policy",
    str(baseline.get("pcie_aspm", "default")),
)
write_text(
    "/sys/module/snd_hda_intel/parameters/power_save",
    str(baseline.get("snd_hda_power_save", "0")),
)

# 4. Restore Wi-Fi power save
wifi_target = str(baseline.get("wifi_power_save", "on"))
net_root = Path("/sys/class/net")
if net_root.is_dir() and iw_bin and Path(iw_bin).exists():
    for net_dev in sorted(net_root.iterdir()):
        if (net_dev / "wireless").is_dir():
            subprocess.run(
                [iw_bin, "dev", net_dev.name, "set", "power_save", wifi_target],
                check=False,
            )

# 5. Restore VM dirty writeback & NMI watchdog
write_text(
    "/proc/sys/vm/dirty_writeback_centisecs",
    str(baseline.get("dirty_writeback_centisecs", "500")),
)
if "nmi_watchdog" in baseline and baseline["nmi_watchdog"] is not None:
    write_text("/proc/sys/kernel/nmi_watchdog", str(baseline["nmi_watchdog"]))

# 6. Restore SATA / AHCI ALPM
sata_alpm = baseline.get("sata_alpm", {})
if isinstance(sata_alpm, dict):
    for host, policy in sata_alpm.items():
        target = (
            Path(host)
            if str(host).startswith("/")
            else Path(f"/sys/class/scsi_host/{host}/link_power_management_policy")
        )
        write_text(target, str(policy))

# 7. Restore AMD Panel Power Savings (ABM)
amd_abm = baseline.get("amd_abm", {})
if isinstance(amd_abm, dict) and amd_abm:
    for conn, level in amd_abm.items():
        target = (
            Path(conn)
            if str(conn).startswith("/")
            else Path(f"/sys/class/drm/{conn}/amdgpu/panel_power_savings")
        )
        write_text(target, str(level))
else:
    for abm_path in glob.glob("/sys/class/drm/card*-eDP-*/amdgpu/panel_power_savings"):
        write_text(abm_path, "0")

# 8. Restore keyboard backlight LEDs
kbd_backlight = baseline.get("kbd_backlight", {})
if isinstance(kbd_backlight, dict):
    for led, val in kbd_backlight.items():
        target = (
            Path(led)
            if str(led).startswith("/")
            else Path(f"/sys/class/leds/{led}/brightness")
        )
        write_text(target, str(val))

# 9. Restore dynamic PCI Runtime PM (Ethernet 0x0200 & SDHCI 0x0805)
pci_controls = baseline.get("pci_controls")
if isinstance(pci_controls, dict) and pci_controls:
    for bdf, ctrl in pci_controls.items():
        write_text(f"/sys/bus/pci/devices/{bdf}/power/control", str(ctrl))
else:
    pci_root = Path("/sys/bus/pci/devices")
    if pci_root.is_dir():
        for dev in sorted(pci_root.iterdir()):
            cls = (read_text(dev / "class", "") or "").lower()
            if cls.startswith("0x0200") or cls.startswith("0x0805"):
                write_text(dev / "power/control", "on")

# 10. Restore USB autosuspend controls
usb_controls = baseline.get("usb_controls", {})
if isinstance(usb_controls, dict):
    for dev_id, ctrl in usb_controls.items():
        write_text(f"/sys/bus/usb/devices/{dev_id}/power/control", str(ctrl))

# 11. Restore original battery charge-limit baseline (end -> start -> end order)
cl_base = baseline.get("charge_limit_baseline")
if isinstance(cl_base, dict):
    c_path = cl_base.get("path")
    c_val = cl_base.get("value")
    s_path = cl_base.get("start_path")
    s_val = cl_base.get("start_value")
    if isinstance(c_path, str) and c_path and c_val is not None:
        write_text(c_path, str(c_val))
    if isinstance(s_path, str) and s_path and s_val is not None:
        write_text(s_path, str(s_val))
        if isinstance(c_path, str) and c_path and c_val is not None:
            write_text(c_path, str(c_val))
PYEOF
fi

# ---------------------------------------------------------------------------
# Remove installed files
# ---------------------------------------------------------------------------
rm -f \
  /etc/systemd/system/smart-powerd.service \
  /etc/sudoers.d/smart-powerd \
  /etc/ananicy.d/99-smart-powerd.types \
  /etc/ananicy.d/99-smart-powerd.rules \
  /usr/local/sbin/smart-powerd \
  /usr/local/sbin/smart-power-uninstall \
  /usr/local/libexec/smart-powerd-helper \
  /usr/local/bin/smart-powerctl \
  /usr/local/bin/smart-power-tray \
  /usr/local/share/applications/smart-power-tray.desktop

rm -rf /usr/local/lib/smart-power /run/smart-powerd /var/lib/smart-powerd

if [[ "${PURGE}" -eq 1 ]]; then
  rm -rf /etc/smart-powerd
  echo "Removed /etc/smart-powerd (--purge)."
elif [[ -d /etc/smart-powerd ]]; then
  echo "Kept /etc/smart-powerd/config.toml (re-run with --purge or remove /etc/smart-powerd manually to delete config)."
fi

if command -v systemctl >/dev/null 2>&1; then
  systemctl daemon-reload
  if systemctl is-enabled --quiet ananicy-cpp 2>/dev/null || systemctl is-active --quiet ananicy-cpp 2>/dev/null; then
    systemctl restart ananicy-cpp || true
  fi
fi

echo "smart-powerd cleanly uninstalled and system defaults restored."
