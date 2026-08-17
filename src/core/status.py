import contextlib
import fcntl
import os
import shutil
import socket
import struct
from datetime import datetime
from pathlib import Path

from ..ui.navigator import draw_bar, format_percent, get_color_for_percent
from .constants import GREEN, PURPLE, RED, RESET, WHITE, YELLOW
from .file_ops import bytes_to_human
from .system import run_command
from .text import display_width

DEFAULT_ROUTE_PATH = Path("/proc/net/route")
SIOCGIFADDR = 0x8915

# Every status row renders as "<icon><pad> <label><pad> <value>". Both pads are
# measured rather than hand-typed: the icons are not all the same width (U+1F4DF
# and friends take two cells, while U+23F1 / U+2699 are a narrow base plus
# U+FE0F and take one), and the label field is sized to the longest label so all
# values start in the same column no matter which rows a machine actually shows.
_ICON_SLOT = 2
_LABEL_SLOT = len("Top Processes:")

# Shared by every temperature this module prints -- the CPU row and both GPU
# probes -- so a retune lands on all of them at once. They used to carry three
# copies of the thresholds in two spellings (`<= 60` and `> 60`).
TEMP_WARN_C = 60.0
TEMP_HOT_C = 80.0


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
        with open("/proc/meminfo") as f:
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
        with open("/proc/uptime") as f:
            uptime_seconds = float(f.readline().split()[0])
            hours = int(uptime_seconds // 3600)
            minutes = int((uptime_seconds % 3600) // 60)
            return f"{hours}h {minutes}m"
    except (OSError, ValueError, IndexError):
        return "Unknown"


def get_cpu_load_summary() -> str:
    """Return CPU load percentage formatted with unit e.g. 'load 3%'."""
    load_1m, *_ = os.getloadavg()
    cores = os.cpu_count() or 1
    load_percent = (load_1m / cores) * 100
    return f"load {load_percent:.0f}%"


def get_battery_info():
    """Get battery capacity, health, and cycle count."""
    try:
        bat_path = Path("/sys/class/power_supply/BAT0")
        if not bat_path.exists():
            return None

        with open(bat_path / "capacity") as f:
            capacity_str = f.read().strip()
            capacity = int(capacity_str)

        # Health calculation
        try:
            with open(bat_path / "energy_full_design") as f:
                design = int(f.read().strip())
            with open(bat_path / "energy_full") as f:
                full = int(f.read().strip())
            health = min(100.0, (full / design) * 100)
            # No parens here: show_status() already wraps the whole details string
            # in one pair, so adding our own produced "((Health: ...) | Cycles: N)".
            health_str = f" Health: {health:.1f}%"
        except (OSError, ValueError, ZeroDivisionError):
            health_str = ""

        # Cycle count
        cycles_str = ""
        try:
            with open(bat_path / "cycle_count") as f:
                cycles = f.read().strip()
                if cycles and cycles != "0":
                    cycles_str = f" | Cycles: {cycles}"
        except OSError:
            pass

        return capacity, f"{capacity}%", f"{health_str}{cycles_str}"
    except (OSError, ValueError):
        return 0, "N/A", ""


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
        with open("/proc/net/dev") as f:
            lines = f.readlines()[2:]  # Skip headers
            eligible = {}
            physical = {}
            for line in lines:
                parts = line.split()
                if not parts:
                    continue
                iface = parts[0].rstrip(":")

                # 1. Skip virtual prefixes
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
        with route_path.open() as f:
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


def get_cpu_temp() -> tuple[float | None, str]:
    """Read authentic CPU core temperature from /sys/class/hwmon or /sys/class/thermal.

    Priority:
    1. Dedicated CPU hardware monitor (coretemp on Intel, k10temp/zenpower on AMD, cpu_thermal on ARM)
    2. Other hwmon sensors with CPU label or acpi thermal zone
    3. Standard fallback: /sys/class/thermal/thermal_zone*/temp
    """
    # 1. Check dedicated CPU hwmon drivers
    try:
        hwmon_root = Path("/sys/class/hwmon")
        if hwmon_root.exists():
            # Known CPU core sensor driver names
            cpu_drivers = {"coretemp", "k10temp", "zenpower", "cpu_thermal", "soc_thermal"}
            candidates = []

            for hw_dir in hwmon_root.glob("hwmon*"):
                name_file = hw_dir / "name"
                drv_name = ""
                if name_file.exists():
                    with contextlib.suppress(OSError):
                        drv_name = name_file.read_text().strip().lower()

                is_cpu_drv = any(k in drv_name for k in cpu_drivers)

                for t_input in hw_dir.glob("temp*_input"):
                    try:
                        raw_str = t_input.read_text().strip()
                        raw_val = int(raw_str)
                        temp_c = raw_val / 1000.0
                        # Ignore disconnected or broken probes. Linux hwmon
                        # occasionally exposes sentinel readings such as 127C
                        # or 255C for sensors that are not physically present.
                        if not 0 < temp_c < 125:
                            continue

                        # Check label (e.g. Tctl, Tdie, Package id 0, CPU)
                        label_file = hw_dir / t_input.name.replace("_input", "_label")
                        label = ""
                        if label_file.exists():
                            label = label_file.read_text().strip().lower()

                        priority = 0
                        if is_cpu_drv:
                            # A dedicated CPU driver always outranks a board/EC
                            # probe. Within it, prefer package/die readings,
                            # then a labelled core, then an unlabelled channel.
                            if any(k in label for k in ("tctl", "tdie", "package")):
                                priority = 5
                            elif "core" in label or "cpu" in label:
                                priority = 4
                            else:
                                priority = 3
                        elif "cpu" in label:
                            priority = 2
                        elif "acpitz" in drv_name:
                            priority = 1

                        if priority > 0:
                            candidates.append((priority, temp_c))
                    except (OSError, ValueError):
                        continue

            if candidates:
                # Pick highest priority, then highest temperature among them
                candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
                best_temp = candidates[0][1]
                return best_temp, f"{best_temp:.0f}°C"
    except OSError:
        pass

    # 2. Fallback to CPU-related /sys/class/thermal/thermal_zone* entries.
    try:
        thermal_root = Path("/sys/class/thermal")
        if thermal_root.exists():
            thermal_candidates = []
            for tz in sorted(thermal_root.glob("thermal_zone*")):
                temp_file = tz / "temp"
                type_file = tz / "type"
                if not temp_file.exists() or not type_file.exists():
                    continue
                try:
                    zone_type = type_file.read_text().strip().lower()
                    raw_val = int(temp_file.read_text().strip())
                    temp_c = raw_val / 1000.0
                    if not 0 < temp_c < 125:
                        continue

                    if any(k in zone_type for k in ("x86_pkg", "cpu", "package")):
                        priority = 3
                    elif any(k in zone_type for k in ("soc", "acpitz")):
                        priority = 2
                    else:
                        # Do not report a GPU, battery, wireless, or other
                        # unrelated thermal zone as the CPU temperature.
                        continue
                    thermal_candidates.append((priority, temp_c))
                except (OSError, ValueError):
                    continue

            if thermal_candidates:
                thermal_candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
                best_temp = thermal_candidates[0][1]
                return best_temp, f"{best_temp:.0f}°C"
    except OSError:
        pass

    return None, "N/A"


def get_fan_speed():
    """Read fan speeds from /sys/class/hwmon."""
    fans = []
    try:
        hwmon_root = Path("/sys/class/hwmon")
        if hwmon_root.exists():
            for hw_dir in hwmon_root.glob("hwmon*"):
                for fan_input in hw_dir.glob("fan*_input"):
                    try:
                        with open(fan_input) as f:
                            rpm = f.read().strip()
                            if rpm and rpm != "0":
                                label_path = hw_dir / fan_input.name.replace("_input", "_label")
                                name = ""
                                if label_path.exists():
                                    with open(label_path) as lf:
                                        name = lf.read().strip()
                                if not name:
                                    name_path = hw_dir / "name"
                                    if name_path.exists():
                                        with open(name_path) as nf:
                                            name = nf.read().strip()
                                entry = f"{name}: {rpm} RPM" if name else f"{rpm} RPM"
                                if entry not in fans:
                                    fans.append(entry)
                    except OSError:
                        continue
    except OSError:
        pass
    return ", ".join(fans) if fans else None


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
                timeout=10,
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
            for card_dir in drm_path.glob("card*"):
                device_dir = card_dir / "device"
                if not device_dir.exists():
                    continue

                util_path = device_dir / "gpu_busy_percent"
                util = None
                if util_path.exists():
                    try:
                        util_value = int(util_path.read_text().strip())
                        if 0 <= util_value <= 100:
                            util = util_value
                    except (OSError, ValueError):
                        pass

                gpu_temps = []
                hwmon_root = device_dir / "hwmon"
                if hwmon_root.exists():
                    for hw_dir in hwmon_root.glob("hwmon*"):
                        for t_input in hw_dir.glob("temp*_input"):
                            try:
                                raw_val = int(t_input.read_text().strip())
                                temp_c = raw_val / 1000.0
                                if not 0 < temp_c < 125:
                                    continue

                                # Check sensor label (junction/hotspot/edge/gpu)
                                label_file = hw_dir / t_input.name.replace("_input", "_label")
                                label = ""
                                if label_file.exists():
                                    label = label_file.read_text().strip().lower()

                                priority = 1
                                if any(k in label for k in ("junction", "hotspot")):
                                    priority = 3
                                elif any(k in label for k in ("edge", "gpu", "core")):
                                    priority = 2

                                gpu_temps.append((priority, temp_c))
                            except (OSError, ValueError):
                                continue

                parts = []
                if gpu_temps:
                    # Pick highest priority, then highest temperature
                    gpu_temps.sort(key=lambda x: (x[0], x[1]), reverse=True)
                    best_gpu_temp = gpu_temps[0][1]
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


def get_system_health_assessment(
    cpu_temp_c: float | None,
    cpu_load_str: str,
    mem_percent: float,
    disk_percent: float,
    battery_data: tuple[int, str, str] | None,
) -> tuple[str, str, str]:
    """Evaluate overall system health based on hardware metrics.

    Returns:
        (icon, color, message)
    """
    issues = []

    # Parse numeric CPU load percent
    cpu_load_val = 0.0
    if "%" in cpu_load_str:
        try:
            num_part = cpu_load_str.replace("load", "").replace("%", "").strip()
            cpu_load_val = float(num_part)
        except ValueError:
            pass

    # 1. Critical & Warning Checks
    if disk_percent >= 90.0:
        issues.append(f"Disk space low ({disk_percent:.0f}%)")
    elif disk_percent >= 80.0:
        issues.append(f"Disk usage high ({disk_percent:.0f}%)")

    if mem_percent >= 90.0:
        issues.append(f"Memory load critical ({mem_percent:.0f}%)")
    elif mem_percent >= 80.0:
        issues.append(f"Memory usage high ({mem_percent:.0f}%)")

    if cpu_temp_c is not None:
        if cpu_temp_c > 80.0:
            issues.append(f"CPU temperature hot ({cpu_temp_c:.0f}°C)")
        elif cpu_temp_c > 70.0:
            issues.append(f"CPU temperature elevated ({cpu_temp_c:.0f}°C)")

    if cpu_load_val >= 90.0:
        issues.append(f"CPU workload heavy ({cpu_load_val:.0f}%)")

    if battery_data:
        try:
            health_str = battery_data[2]
            if "Health:" in health_str:
                health_val = float(health_str.split("Health:")[1].split("%")[0].strip())
                if health_val < 50.0:
                    issues.append(f"Battery degraded ({health_val:.0f}%)")
        except (ValueError, IndexError):
            pass

    critical_issues = [
        iss for iss in issues if any(k in iss for k in ("critical", "low", "hot", "degraded"))
    ]

    if not issues:
        return "🌿", GREEN, "System is running in optimal condition."
    if not critical_issues:
        return "🟡", YELLOW, f"System status is moderate ({', '.join(issues)})."
    return "🔴", RED, f"System under heavy load: {', '.join(issues)}."


def show_status():
    """Main status display logic."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n{PURPLE}System Health Status ({now}){RESET}")
    print()

    uptime = get_uptime()
    cpu_load = get_cpu_load_summary()
    temp_val, cpu_temp_str = get_cpu_temp()
    fans = get_fan_speed()
    used_mem_str, total_mem_str, mem_percent = get_mem_info()
    battery_data = get_battery_info()
    rx, tx = get_network_traffic()
    local_ip = get_ip_info()
    gpu = get_gpu_info()
    top_procs = get_top_processes()

    home_stats = shutil.disk_usage(os.path.expanduser("~"))
    disk_percent = (home_stats.used / home_stats.total) * 100

    # 1. Overview & Compute (Uptime, CPU, GPU, Fans)
    uptime_str = f"{uptime} (since boot)" if uptime != "Unknown" else uptime
    print(_status_row("⏱️", "Uptime:", uptime_str))
    cpu_status_str = f"{get_temp_color(temp_val)}{cpu_temp_str}{RESET} | {cpu_load}"
    print(_status_row("📟", "CPU Status:", cpu_status_str))

    if gpu:
        print(_status_row("🎮", "GPU Status:", gpu))

    if fans:
        print(_status_row("⚙️", "Fan Speed:", fans))

    # 2. Memory & Storage (RAM, Disk)
    mem_bar = draw_bar(mem_percent, width=20)
    mem_color = get_color_for_percent(mem_percent)
    print(
        _status_row(
            "🧠",
            "Memory:",
            f"{mem_bar}  {mem_color}{format_percent(mem_percent)}{RESET}  "
            f"({used_mem_str} / {total_mem_str})",
        )
    )

    disk_bar = draw_bar(disk_percent, width=20)
    disk_color = get_color_for_percent(disk_percent)
    print(
        _status_row(
            "💾",
            "Disk:",
            f"{disk_bar}  {disk_color}{format_percent(disk_percent)}{RESET}  "
            f"({bytes_to_human(home_stats.used)} / {bytes_to_human(home_stats.total)})",
        )
    )

    # 3. Hardware & Network (Battery, Network)
    if battery_data:
        bat_val, _bat_pct_str, bat_details = battery_data
        bat_color = GREEN if bat_val >= 50 else (YELLOW if bat_val >= 20 else RED)
        bat_bar = draw_bar(bat_val, width=20, force_color=bat_color)
        details_fmt = f"  ({bat_details.strip()})" if bat_details.strip() else ""
        print(
            _status_row(
                "🔋",
                "Battery:",
                f"{bat_bar}  {bat_color}{format_percent(float(bat_val))}{RESET}{details_fmt}",
            )
        )

    print(_status_row("🌐", "Network:", f"↓ {rx} / ↑ {tx} | {local_ip}"))

    # 4. Workload (Top Processes)
    if top_procs:
        print(_status_row("🔝", "Top Processes:", ", ".join(top_procs)))

    # 5. Overall Assessment Verdict
    icon, color, verdict = get_system_health_assessment(
        temp_val, cpu_load, mem_percent, disk_percent, battery_data
    )
    print(f"\n{color}{icon}{RESET} {verdict}\n")
