import fcntl
import os
import shutil
import socket
import struct
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from .core.constants import GREEN, PURPLE, RED, RESET, SECONDS_PER_HOUR, WHITE, YELLOW
from .core.file_ops import bytes_to_human
from .core.render import draw_bar, format_percent, get_color_for_percent
from .core.system import run_command
from .core.text import display_width

DEFAULT_ROUTE_PATH = Path("/proc/net/route")
SIOCGIFADDR = 0x8915

_HWMON_ROOT = Path("/sys/class/hwmon")
_THERMAL_ROOT = Path("/sys/class/thermal")

# Every status row renders as "<icon><pad> <label><pad> <value>". Both pads are
# measured rather than hand-typed: the icons are not all the same width (U+1F4DF
# and friends take two cells, while U+23F1 / U+2699 are a narrow base plus
# U+FE0F and take one), and the label field is sized to the longest label so all
# values start in the same column no matter which rows a machine actually shows.
_ICON_SLOT = 2

# Every label the report can print, listed so the field is sized by measurement
# rather than by whichever label someone remembered. "Overall Status:" is one
# character longer than "Top Processes:" and used to push its own value a column
# right of the other ten. The disk row can widen to "Disk (/var):" (12), which
# still fits.
_ROW_LABELS = (
    "Uptime:",
    "CPU Status:",
    "GPU Status:",
    "Fan Speed:",
    "Memory:",
    "Disk:",
    "Battery:",
    "Network:",
    "Top Processes:",
    "Overall Status:",
)
_LABEL_SLOT = max(len(label) for label in _ROW_LABELS)

# Shared by every temperature this module prints -- the CPU row and both GPU
# probes -- so a retune lands on all of them at once. They used to carry three
# copies of the thresholds in two spellings (`<= 60` and `> 60`).
TEMP_WARN_C = 60.0
TEMP_HOT_C = 80.0

# The verdict's own "elevated" line, deliberately above the row's yellow one:
# many Ryzen and mobile parts idle in the low 60s, so calling that a finding
# would leave half the machines in the world permanently at "moderate". It is
# named rather than left as a literal 70.0 so the gap is a decision on record --
# the assessment used to hold the third copy of these thresholds, spelling hot
# as its own 80.0 (equal to TEMP_HOT_C, but not tied to it) and warn as 70.0.
TEMP_ELEVATED_C = 70.0

# One boundary for "the battery is low", shared by the row's colour and its icon
# so the two cannot disagree about what counts as low.
_BATTERY_LOW_PERCENT = 20
_BATTERY_HIGH_PERCENT = 50


def _status_row(icon: str, label: str, value: str) -> str:
    icon_pad = " " * max(0, _ICON_SLOT - display_width(icon))
    label_pad = " " * max(0, _LABEL_SLOT - display_width(label))
    return f"{icon}{icon_pad} {label}{label_pad} {value}"


def get_temp_color(temp_c: float | None) -> str:
    """Color a temperature reading: green to 60C, yellow to 80C, red above.

    ``None`` means the sensor could not be read, which is not a temperature and
    must not borrow green -- green claims "this reading is healthy", and there is
    no reading. It gets WHITE, the same dimming an unknown size gets elsewhere.
    """
    if temp_c is None:
        return WHITE
    if temp_c > TEMP_HOT_C:
        return RED
    if temp_c > TEMP_WARN_C:
        return YELLOW
    return GREEN


def get_mem_info():
    """Read RAM info from /proc/meminfo."""
    try:
        with open("/proc/meminfo", errors="replace") as f:
            lines = f.readlines()
            total = 0
            available = 0
            for line in lines:
                if "MemTotal" in line:
                    total = int(line.split()[1]) * 1024
                if "MemAvailable" in line:
                    available = int(line.split()[1]) * 1024
            used = total - available
            percent = (used / total) * 100 if total > 0 else 0
            return bytes_to_human(used), bytes_to_human(total), percent
    except (OSError, ValueError, IndexError):
        return "Unknown", "Unknown", 0


def get_uptime():
    try:
        with open("/proc/uptime", errors="replace") as f:
            uptime_seconds = float(f.readline().split()[0])
    except (OSError, ValueError, IndexError):
        return "Unknown"

    hours = int(uptime_seconds // SECONDS_PER_HOUR)
    if hours >= 24:
        # Past a day the minutes carry no information and the hour count stops
        # being readable -- a NAS up for three months printed "2160h 5m". Same
        # units uptime(1) switches to.
        return f"{hours // 24}d {hours % 24}h"
    return f"{hours}h {int((uptime_seconds % SECONDS_PER_HOUR) // 60)}m"


def get_cpu_load_summary() -> tuple[float | None, str]:
    """Return the 1-minute load per core as ``(percent, "load 3%")``.

    Shaped like ``get_cpu_temp``: the number is for the health assessment, the
    string for the row. The assessment used to parse the string back into a
    float, which handed it the value already rounded by ``.0f`` and made an
    unreadable load indistinguishable from a load of zero.

    ``percent`` is ``None`` when the load average is unavailable. Every other
    probe here degrades to "Unknown"/"N/A"; this one used to raise, and nothing
    above ``show_status`` catches OSError, so a kernel without ``/proc/loadavg``
    turned the whole status screen into a traceback.
    """
    try:
        load_1m, *_ = os.getloadavg()
    except OSError:
        return None, "load N/A"
    cores = os.cpu_count() or 1
    load_percent = (load_1m / cores) * 100
    return load_percent, f"load {load_percent:.0f}%"


# The filesystems worth a row of their own. Debian's guided partitioning offers
# separate /home and /var, and a full /var breaks apt long before $HOME notices.
DISK_ROW_PATHS = ("/", "~", "/var")


def get_disk_rows() -> list[tuple[str, int, int]]:
    """Return ``(spec, used, total)`` for each distinct filesystem behind the specs.

    Deduplicated by the measured numbers rather than by ``st_dev``: btrfs gives
    every subvolume its own device id, so a single-pool Fedora/openSUSE box has
    ``/`` and ``$HOME`` on different st_dev while reporting identical usage, and
    keying on the device would print the same row twice. Two genuinely separate
    filesystems agreeing to the byte would collapse into one row, which costs
    nothing: they carry the same percentage either way.

    Paths that cannot be measured are dropped rather than reported as empty -- a
    failing statfs is not a disk with zero bytes used.
    """
    rows: list[tuple[str, int, int]] = []
    seen: set[tuple[int, int]] = set()
    for spec in DISK_ROW_PATHS:
        path = os.path.expanduser(spec)
        try:
            usage = shutil.disk_usage(path)
        except OSError:
            continue
        if usage.total <= 0 or (usage.used, usage.total) in seen:
            continue
        seen.add((usage.used, usage.total))
        rows.append((spec, usage.used, usage.total))
    return rows


def _read_sysfs(path: Path) -> str:
    """Return a stripped sysfs value, or "" when it cannot be read."""
    try:
        # hwmon names, thermal zone types and temperature labels come from ACPI
        # and DMI tables the board vendor wrote, so they are not reliably UTF-8;
        # they are only ever displayed, which is what errors="replace" is for.
        return path.read_text(errors="replace").strip()
    except OSError:
        return ""


def _battery_pack() -> tuple[Path, int] | None:
    """Return the first battery with a parseable capacity, and that capacity.

    Globs ``BAT*`` instead of assuming ``BAT0``: laptops that dock a second pack
    number them BAT0/BAT1, and a few (some ThinkPads, most Chromebooks with an
    ACPI shim) expose only BAT1. A pack whose capacity does not parse -- the stub
    nodes a VM leaves behind -- is skipped rather than taken as the answer.
    """
    try:
        candidates = sorted(Path("/sys/class/power_supply").glob("BAT*"))
    except OSError:
        return None
    for candidate in candidates:
        try:
            return candidate, int(_read_sysfs(candidate / "capacity"))
        except ValueError:
            continue
    return None


# ACPI reports charge in one of two unit families and only ever populates one of
# them: energy (uWh) or charge (uAh). Health is a ratio, so either works.
_BATTERY_FULL_PAIRS = (
    ("energy_full", "energy_full_design"),
    ("charge_full", "charge_full_design"),
)


def _battery_health(bat_path: Path) -> float | None:
    """Return remaining capacity as a percent of the design capacity, or None."""
    for full_name, design_name in _BATTERY_FULL_PAIRS:
        try:
            design = int(_read_sysfs(bat_path / design_name))
            full = int(_read_sysfs(bat_path / full_name))
        except ValueError:
            continue
        if design <= 0:
            continue
        return min(100.0, (full / design) * 100)
    return None


def get_battery_info() -> tuple[int, float | None, str] | None:
    """Return ``(charge percent, health percent, details)``, or None when absent.

    ``None`` covers both "this machine has no battery" and "the battery is there
    but unreadable". The old code answered ``(0, "N/A", "")`` for the second
    case, which show_status drew as an empty bar at 0.0% -- indistinguishable
    from a pack about to die. Same principle as get_temp_color: a failed read is
    not a measurement.

    Health is handed back as a number as well as inside ``details``: the health
    assessment needs the value, and it used to recover it by parsing the display
    string. ``details`` carries no parentheses of its own -- show_status wraps the
    whole string in one pair, so adding another produced
    "((Health: ...) | Cycles: N)".
    """
    pack = _battery_pack()
    if pack is None:
        return None
    bat_path, capacity = pack

    health = _battery_health(bat_path)
    cycles = _read_sysfs(bat_path / "cycle_count")

    details = [] if health is None else [f"Health: {health:.1f}%"]
    if cycles and cycles != "0":
        details.append(f"Cycles: {cycles}")
    return capacity, health, " | ".join(details)


def get_network_traffic():
    """Get traffic since boot, preferring hardware-backed network interfaces."""
    # Virtual interface prefix blacklist for environments where /sys/class/net is restricted
    virtual_prefixes = (
        "docker",
        "br-",
        "veth",
        "virbr",
        "vnet",
        "vmnet",
        "vboxnet",
        "tailscale",
        "wg",
        "tun",
        "tap",
        "flannel",
        "cni",
        "cali",
        "dummy",
    )
    try:
        with open("/proc/net/dev", errors="replace") as f:
            lines = f.readlines()[2:]  # Skip headers
            eligible = {}
            physical = {}
            for line in lines:
                parts = line.split()
                if not parts:
                    continue
                iface = parts[0].rstrip(":")

                if iface == "lo" or iface.startswith(virtual_prefixes):
                    continue

                counters = (int(parts[1]), int(parts[9]))
                eligible[iface] = counters

                # A direct device link identifies a PCI, USB, or platform
                # network device. Software-only interfaces do not have one.
                if Path(f"/sys/class/net/{iface}/device").exists():
                    physical[iface] = counters

            selected = physical
            if not selected and eligible:
                # Containers and PPP-style uplinks may expose only a logical
                # default-route interface with no device link. Use that single
                # interface rather than reporting a misleading zero total.
                default_iface = _get_default_route_interface()
                if default_iface in eligible:
                    selected = {default_iface: eligible[default_iface]}

            if not selected and eligible:
                return "N/A", "N/A"

            rx = sum(counters[0] for counters in selected.values())
            tx = sum(counters[1] for counters in selected.values())
            return bytes_to_human(rx), bytes_to_human(tx)
    except (OSError, ValueError, IndexError):
        return "N/A", "N/A"


def _get_default_route_interface(route_path: Path = DEFAULT_ROUTE_PATH) -> str | None:
    """Return the interface used by the default IPv4 route without network I/O."""
    try:
        with route_path.open(errors="replace") as f:
            routes = f.readlines()[1:]
    except OSError:
        return None

    best_iface = None
    best_metric = None
    for route in routes:
        parts = route.split()
        if len(parts) < 4 or parts[1] != "00000000":
            continue
        try:
            flags = int(parts[3], 16)
            metric = int(parts[6]) if len(parts) > 6 else 0
        except ValueError:
            continue
        if not flags & 0x1:
            continue
        if best_metric is None or metric < best_metric:
            best_iface = parts[0]
            best_metric = metric
    return best_iface


def _get_interface_ipv4(interface: str) -> str | None:
    """Return an interface IPv4 address using local kernel interface metadata."""
    if not interface:
        return None
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            request = struct.pack("256s", interface[:15].encode("utf-8"))
            response = fcntl.ioctl(sock.fileno(), SIOCGIFADDR, request)
            return socket.inet_ntoa(response[20:24])
    except OSError:
        return None


def get_ip_info():
    """Get local IP address without connecting to an external host."""
    iface = _get_default_route_interface()
    local_ip = _get_interface_ipv4(iface) if iface else None

    return local_ip or "N/A"


def _read_temp_c(temp_input: Path) -> float | None:
    """Convert a hwmon/thermal millidegree file to Celsius, or None if unusable.

    Ignores disconnected or broken probes: Linux hwmon occasionally exposes
    sentinel readings such as 127C or 255C for sensors that are not physically
    present, and a channel reading exactly 0 is almost always unpopulated.
    """
    try:
        temp_c = int(_read_sysfs(temp_input)) / 1000.0
    except ValueError:
        return None
    return temp_c if 0 < temp_c < 125 else None


def _hottest(candidates: list[tuple[int, float]]) -> float | None:
    """Return the hottest temperature among the highest-priority candidates.

    Tuple order does the ranking: priority first, then temperature, so several
    channels tied at one priority answer with the hottest -- which is what a
    multi-core package reading wants.
    """
    return max(candidates)[1] if candidates else None


def _iter_hwmon(root: Path = _HWMON_ROOT) -> list[tuple[Path, str]]:
    """List ``(directory, driver name)`` for every hwmon node under ``root``.

    The driver name is read here because the temperature and fan probes both
    want it, and the list is sorted so a machine carrying two sensors of one kind
    answers the same way across runs. ``show_status`` walks the tree once and
    hands the result to both probes; called with no argument, either probe still
    stands on its own.
    """
    if not root.exists():
        return []
    try:
        return [(hw_dir, _read_sysfs(hw_dir / "name")) for hw_dir in sorted(root.glob("hwmon*"))]
    except OSError:
        return []


def _best_hwmon_temp(
    hwmon: list[tuple[Path, str]],
    rank: Callable[[str, str], int],
) -> float | None:
    """Pick a temperature from hwmon channels ranked by ``rank(driver, label)``.

    Reads ``temp*_label`` before ``temp*_input`` because the label decides whether
    the channel counts at all and costs about a twentieth as much: an nvme
    ``temp*_input`` is a round-trip to the SSD controller. ``rank`` answering 0
    drops the channel.

    Both arguments reach ``rank`` lower-cased, so a rank function only ever
    matches lowercase keywords. This is the one ranking loop in the module -- the
    CPU hwmon sweep, the thermal zones and the GPU card each had their own copy,
    which is how the temperature thresholds ended up in three spellings.
    """
    candidates: list[tuple[int, float]] = []
    for hw_dir, drv_name in hwmon:
        try:
            # Listed eagerly: Path.glob is lazy on 3.10-3.12, where a directory
            # that vanished after the walk (an unplugged USB sensor controller)
            # raises FileNotFoundError from the loop below rather than from here,
            # which is outside this guard. get_fan_speed already reads its own
            # glob eagerly for the same reason.
            temp_inputs = sorted(hw_dir.glob("temp*_input"))
        except OSError:
            continue
        for t_input in temp_inputs:
            label = _read_sysfs(hw_dir / t_input.name.replace("_input", "_label"))
            priority = rank(drv_name.lower(), label.lower())
            if priority == 0:
                continue
            temp_c = _read_temp_c(t_input)
            if temp_c is None:
                continue
            candidates.append((priority, temp_c))
    return _hottest(candidates)


# Drivers that expose a real CPU die sensor: Intel, AMD, and the ARM SoC ones.
_CPU_SENSOR_DRIVERS = ("coretemp", "k10temp", "zenpower", "cpu_thermal", "soc_thermal")


def _cpu_sensor_priority(drv_name: str, label: str) -> int:
    """Rank one hwmon channel as a CPU temperature source; 0 means "not one"."""
    if any(k in drv_name for k in _CPU_SENSOR_DRIVERS):
        # A dedicated CPU driver always outranks a board or EC probe. Inside one,
        # a die reading beats Tctl: k10temp exposes both on Threadripper and the
        # early X-series, where Tctl is the control-loop value carrying a
        # deliberate offset (+27C on those parts) and Tdie is the measured
        # silicon. They used to share one priority, and the tie-break takes the
        # hotter channel, so Tctl won every time and the report ran high.
        if "tdie" in label or "package" in label:
            return 6
        if "tctl" in label:
            return 5
        if "core" in label or "cpu" in label:
            return 4
        return 3
    if "cpu" in label:
        return 2
    if "acpitz" in drv_name:
        return 1
    return 0


def _best_thermal_zone_temp(root: Path = _THERMAL_ROOT) -> float | None:
    """Pick a CPU temperature from /sys/class/thermal, the last-resort source."""
    if not root.exists():
        return None
    try:
        zones = sorted(root.glob("thermal_zone*"))
    except OSError:
        return None

    candidates: list[tuple[int, float]] = []
    for zone in zones:
        zone_type = _read_sysfs(zone / "type").lower()
        if any(k in zone_type for k in ("x86_pkg", "cpu", "package")):
            priority = 3
        elif any(k in zone_type for k in ("soc", "acpitz")):
            priority = 2
        else:
            # Do not report a GPU, battery, wireless, or other unrelated thermal
            # zone as the CPU temperature.
            continue
        temp_c = _read_temp_c(zone / "temp")
        if temp_c is None:
            continue
        candidates.append((priority, temp_c))
    return _hottest(candidates)


def get_cpu_temp(hwmon: list[tuple[Path, str]] | None = None) -> tuple[float | None, str]:
    """Read authentic CPU core temperature from /sys/class/hwmon or /sys/class/thermal.

    Priority:
    1. Dedicated CPU hardware monitor (coretemp on Intel, k10temp/zenpower on AMD, cpu_thermal on ARM)
    2. Other hwmon sensors with CPU label or acpi thermal zone
    3. Standard fallback: /sys/class/thermal/thermal_zone*/temp
    """
    best = _best_hwmon_temp(_iter_hwmon() if hwmon is None else hwmon, _cpu_sensor_priority)
    if best is None:
        best = _best_thermal_zone_temp()
    if best is None:
        return None, "N/A"
    return best, f"{best:.0f}°C"


def get_fan_speed(hwmon: list[tuple[Path, str]] | None = None) -> str | None:
    """Read fan speeds from /sys/class/hwmon."""
    fans: list[str] = []
    for hw_dir, drv_name in _iter_hwmon() if hwmon is None else hwmon:
        try:
            fan_inputs = sorted(hw_dir.glob("fan*_input"))
        except OSError:
            continue
        for fan_input in fan_inputs:
            rpm = _read_sysfs(fan_input)
            if not rpm or rpm == "0":
                continue
            # A channel label ("CPU Fan") beats the driver name, which is all a
            # board controller offers.
            name = _read_sysfs(hw_dir / fan_input.name.replace("_input", "_label")) or drv_name
            entry = f"{name}: {rpm} RPM" if name else f"{rpm} RPM"
            if entry not in fans:
                fans.append(entry)
    return ", ".join(fans) if fans else None


def _gpu_sensor_priority(_drv_name: str, label: str) -> int:
    """Rank one GPU hwmon channel. Every channel on the card is a GPU reading.

    Unlike the CPU ranking this never answers 0: the channels live under the
    card's own device directory, so even an unlabelled one belongs to the GPU.
    """
    if "junction" in label or "hotspot" in label:
        return 3
    if any(k in label for k in ("edge", "gpu", "core")):
        return 2
    return 1


def get_gpu_info() -> str | None:
    """Detect and get GPU status (NVIDIA/AMD/Intel). Returns formatted 'Temp | Util' string."""
    # 1. Check NVIDIA (Most common for AI / Dedicated)
    if shutil.which("nvidia-smi"):
        try:
            res = run_command(
                [
                    "nvidia-smi",
                    "--query-gpu=temperature.gpu,utilization.gpu",
                    "--format=csv,noheader,nounits",
                ],
                capture=True,
                # It either answers in milliseconds or it is wedged (a card in a
                # deep power state, a driver mid-reset). The report already has
                # its header printed by now, so a long timeout just shows the
                # user a stalled screen.
                timeout=3,
            )
            if res.ok and res.stdout.strip():
                first = res.stdout.strip().splitlines()[0]
                temp_str, util_str = [p.strip() for p in first.split(",")]
                temp_val = float(temp_str)
                if 0 < temp_val < 125:
                    return f"{get_temp_color(temp_val)}{temp_val:.0f}°C{RESET} | load {util_str}%"
                return f"load {util_str}%"
        except (ValueError, IndexError):
            pass

    # 2. Check AMD/Intel/Other (via sysfs DRM + hwmon)
    try:
        drm_path = Path("/sys/class/drm")
        if drm_path.exists():
            # /sys/class/drm also holds one directory per connector, named
            # card<N>-<CONNECTOR>. Those match a plain "card*" and carry a
            # `device` link of their own (pointing at the card, not the GPU's PCI
            # device), so the check below cannot tell them apart -- a laptop with
            # four connectors ran this whole body five times. sorted() also makes
            # the choice stable on a machine with two cards.
            for card_dir in sorted(drm_path.glob("card[0-9]*")):
                if "-" in card_dir.name:
                    continue
                device_dir = card_dir / "device"
                if not device_dir.exists():
                    continue

                util_path = device_dir / "gpu_busy_percent"
                util = None
                if util_path.exists():
                    try:
                        util_value = int(util_path.read_text(errors="replace").strip())
                        if 0 <= util_value <= 100:
                            util = util_value
                    except (OSError, ValueError):
                        pass

                best_gpu_temp = _best_hwmon_temp(
                    _iter_hwmon(device_dir / "hwmon"), _gpu_sensor_priority
                )

                parts = []
                if best_gpu_temp is not None:
                    color = get_temp_color(best_gpu_temp)
                    parts.append(f"{color}{best_gpu_temp:.0f}°C{RESET}")
                if util is not None:
                    parts.append(f"load {util}%")
                if parts:
                    return " | ".join(parts)
    except (OSError, ValueError):
        pass

    return None


def get_top_processes():
    """Get top 3 applications by aggregated memory usage."""
    try:
        # Use ps to get command and resident memory (rss)
        cmd = ["ps", "-eo", "comm,rss", "--no-headers"]
        res = run_command(cmd, capture=True, timeout=10)
        if res.ok:
            lines = res.stdout.strip().split("\n")
            agg_mem: dict[str, int] = {}
            for line in lines:
                parts = line.split()
                if len(parts) >= 2:
                    name = parts[0]
                    try:
                        rss = int(parts[1])
                        agg_mem[name] = agg_mem.get(name, 0) + rss
                    except ValueError:
                        continue

            # Sort by aggregated memory usage and take top 3
            sorted_procs = sorted(agg_mem.items(), key=lambda x: x[1], reverse=True)[:3]

            procs = []
            for name, total_rss in sorted_procs:
                mem_gb = total_rss / (1024 * 1024)
                if mem_gb >= 0.1:
                    procs.append(f"{name} ({mem_gb:.1f}GB)")
                else:
                    mem_mb = total_rss / 1024
                    procs.append(f"{name} ({int(mem_mb)}MB)")
            return procs
    except (OSError, ValueError):
        pass
    return []


# A finding either forces the red verdict or only tints it yellow. This used to
# be decided by searching the human-readable sentence for "critical", "low",
# "hot" or "degraded", so the classification depended on nobody ever writing a
# message that happened to contain one of those words -- a future "Fan speed low"
# would have silently promoted itself to red.
_CRITICAL = "critical"
_WARNING = "warning"


def get_system_health_assessment(
    cpu_temp_c: float | None,
    cpu_load_percent: float | None,
    mem_percent: float,
    disk_percent: float,
    battery_health: float | None,
) -> tuple[str, str, str]:
    """Evaluate overall system health based on hardware metrics.

    Returns:
        (icon, color, message)
    """
    issues: list[tuple[str, str]] = []

    if disk_percent >= 90.0:
        issues.append((_CRITICAL, f"Disk space low ({disk_percent:.0f}%)"))
    elif disk_percent >= 80.0:
        issues.append((_WARNING, f"Disk usage high ({disk_percent:.0f}%)"))

    if mem_percent >= 90.0:
        issues.append((_CRITICAL, f"Memory load critical ({mem_percent:.0f}%)"))
    elif mem_percent >= 80.0:
        issues.append((_WARNING, f"Memory usage high ({mem_percent:.0f}%)"))

    if cpu_temp_c is not None:
        if cpu_temp_c > TEMP_HOT_C:
            issues.append((_CRITICAL, f"CPU temperature hot ({cpu_temp_c:.0f}°C)"))
        elif cpu_temp_c > TEMP_ELEVATED_C:
            issues.append((_WARNING, f"CPU temperature elevated ({cpu_temp_c:.0f}°C)"))

    # A pegged CPU is what a compile or a render looks like, not a fault, so it
    # stays a warning -- which is also what the old keyword matching worked out
    # to, none of its four words appearing in this sentence.
    if cpu_load_percent is not None and cpu_load_percent >= 90.0:
        issues.append((_WARNING, f"CPU workload heavy ({cpu_load_percent:.0f}%)"))

    if battery_health is not None and battery_health < 50.0:
        issues.append((_CRITICAL, f"Battery degraded ({battery_health:.0f}%)"))

    if not issues:
        return "🌿", GREEN, "System is running in optimal condition."
    findings = ", ".join(text for _, text in issues)
    # A colon, not parentheses: every finding already carries its own "(85%)",
    # so wrapping the joined list would nest them and end the line on "))".
    if not any(severity == _CRITICAL for severity, _ in issues):
        return "🟡", YELLOW, f"System status is moderate: {findings}."
    # Deliberately not "under heavy load": a full disk or a worn-out battery is
    # neither load nor heavy, and those reach this line too.
    return "🔴", RED, f"System needs attention: {findings}."


def show_status():
    """Main status display logic."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n{PURPLE}System Health Status ({now}){RESET}")
    print()

    # Each probe runs immediately before the row that needs it, so a slow one
    # holds up only the rest of the report instead of all of it. Two of them fork
    # (nvidia-smi, ps) and can stall for seconds on a wedged driver or a process
    # in uninterruptible sleep; the header was already on screen while every
    # probe ran, so that time used to read as a hang. The sections below keep
    # that property -- each one prints as it probes, rather than collecting
    # figures for a caller to print later.
    temp_val, load_percent = _show_compute_rows()
    mem_percent, disk_percent = _show_memory_and_storage_rows()
    bat_health = _show_battery_and_network_rows()
    _show_workload_row()
    _show_verdict_row(temp_val, load_percent, mem_percent, disk_percent, bat_health)


def _show_compute_rows() -> tuple[float | None, float | None]:
    """Uptime, CPU temperature and load, GPU, fans.

    Hands back the two figures the verdict needs from this group: the CPU
    temperature in °C and the load as a percentage, each None when the machine
    would not say.
    """
    uptime = get_uptime()
    uptime_str = f"{uptime} (since boot)" if uptime != "Unknown" else uptime
    print(_status_row("⏱️", "Uptime:", uptime_str))

    # One walk of /sys/class/hwmon for both the temperature and the fan probe:
    # they read different files out of the same directories, and each `name` is
    # read once here rather than once per probe.
    hwmon = _iter_hwmon()
    temp_val, cpu_temp_str = get_cpu_temp(hwmon)
    load_percent, cpu_load = get_cpu_load_summary()
    cpu_status_str = f"{get_temp_color(temp_val)}{cpu_temp_str}{RESET} | {cpu_load}"
    print(_status_row("🔲", "CPU Status:", cpu_status_str))

    gpu = get_gpu_info()
    if gpu:
        print(_status_row("🎮", "GPU Status:", gpu))

    fans = get_fan_speed(hwmon)
    if fans:
        print(_status_row("❄️", "Fan Speed:", fans))

    return temp_val, load_percent


def _show_memory_and_storage_rows() -> tuple[float, float]:
    """RAM, then one row per filesystem.

    Hands back the two percentages the verdict needs: memory used, and the
    fullest filesystem.
    """
    used_mem_str, total_mem_str, mem_percent = get_mem_info()
    mem_bar = draw_bar(mem_percent, width=20)
    mem_color = get_color_for_percent(mem_percent)
    print(
        _status_row(
            "💾",
            "Memory:",
            f"{mem_bar}  {mem_color}{format_percent(mem_percent)}{RESET}  "
            f"({used_mem_str} / {total_mem_str})",
        )
    )

    disk_rows = get_disk_rows()
    # The verdict tracks the fullest filesystem: on a split layout a full /var
    # breaks apt while $HOME still looks roomy.
    disk_percent = max((used / total * 100 for _, used, total in disk_rows), default=0.0)

    # One row per filesystem, labelled with the path only when there is more
    # than one -- a single-partition machine keeps the plain "Disk:" row.
    for spec, used, total in disk_rows:
        row_percent = (used / total) * 100
        disk_bar = draw_bar(row_percent, width=20)
        disk_color = get_color_for_percent(row_percent)
        label = "Disk:" if len(disk_rows) == 1 else f"Disk ({spec}):"
        print(
            _status_row(
                "💿",
                label,
                f"{disk_bar}  {disk_color}{format_percent(row_percent)}{RESET}  "
                f"({bytes_to_human(used)} / {bytes_to_human(total)})",
            )
        )

    return mem_percent, disk_percent


def _show_battery_and_network_rows() -> float | None:
    """Battery, then network throughput and address.

    Hands back the pack's health as a percentage, or None on a machine with no
    battery or a pack that would not answer -- neither of which is a battery at
    0%, so neither prints a row.
    """
    battery_data = get_battery_info()
    bat_health: float | None = None
    if battery_data:
        bat_val, bat_health, bat_details = battery_data
        bat_color = (
            GREEN
            if bat_val >= _BATTERY_HIGH_PERCENT
            else (YELLOW if bat_val >= _BATTERY_LOW_PERCENT else RED)
        )
        bat_bar = draw_bar(bat_val, width=20, force_color=bat_color)
        details_fmt = f"  ({bat_details})" if bat_details else ""
        # Same 20% boundary the colour uses, so the icon and the red bar agree.
        # Both glyphs are two cells wide, so the row alignment is unchanged.
        bat_icon = "🔋" if bat_val >= _BATTERY_LOW_PERCENT else "🪫"
        print(
            _status_row(
                bat_icon,
                "Battery:",
                f"{bat_bar}  {bat_color}{format_percent(float(bat_val))}{RESET}{details_fmt}",
            )
        )

    rx, tx = get_network_traffic()
    local_ip = get_ip_info()
    print(_status_row("🖧", "Network:", f"↓ {rx} / ↑ {tx} | {local_ip}"))

    return bat_health


def _show_workload_row() -> None:
    """The heaviest processes, when `ps` named any."""
    top_procs = get_top_processes()
    if top_procs:
        print(_status_row("🔝", "Top Processes:", ", ".join(top_procs)))


def _show_verdict_row(
    temp_val: float | None,
    load_percent: float | None,
    mem_percent: float,
    disk_percent: float,
    bat_health: float | None,
) -> None:
    """The one-line verdict, from the figures the rows above already printed.

    Last, and the only row that reads no probe of its own: everything it judges
    was measured for a row further up, so the verdict cannot disagree with the
    report above it.
    """
    icon, color, verdict = get_system_health_assessment(
        temp_val, load_percent, mem_percent, disk_percent, bat_health
    )
    status_row = _status_row(icon, "Overall Status:", verdict)
    print(status_row.replace(icon, f"{color}{icon}{RESET}", 1))
