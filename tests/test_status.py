import os
import socket
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import MagicMock, mock_open, patch

import pytest

from src.core.text import display_width
from src.status import (
    _ICON_SLOT,
    _LABEL_SLOT,
    _ROW_LABELS,
    TEMP_ELEVATED_C,
    TEMP_HOT_C,
    TEMP_WARN_C,
    _get_default_route_interface,
    _get_interface_ipv4,
    _iter_hwmon,
    _status_row,
    get_battery_info,
    get_cpu_load_summary,
    get_cpu_temp,
    get_disk_rows,
    get_fan_speed,
    get_gpu_info,
    get_ip_info,
    get_mem_info,
    get_network_traffic,
    get_system_health_assessment,
    get_temp_color,
    get_uptime,
    show_status,
)
from src.ui.navigator import ANSI_CSI_RE

# Every row show_status() can print, as (icon, label). Kept here rather than
# imported so that adding a row to the report without checking its alignment
# makes this list -- and the reviewer -- notice.
STATUS_ROWS = [
    ("⏱️", "Uptime:"),
    ("🔲", "CPU Status:"),
    ("\U0001f3ae", "GPU Status:"),
    ("❄️", "Fan Speed:"),
    ("\U0001f4be", "Memory:"),
    ("\U0001f4bf", "Disk:"),
    # The disk row names its filesystem once a machine shows more than one, and
    # "/var" is the longest spec the report can print.
    ("\U0001f4bf", "Disk (/var):"),
    ("\U0001f50b", "Battery:"),
    # The battery row swaps in the low-charge glyph under 20%, so both spellings
    # have to hold the value column.
    ("\U0001faab", "Battery:"),
    ("\U0001f5a7", "Network:"),
    ("\U0001f51d", "Top Processes:"),
    # The verdict row carries the longest label in the report, and each of its
    # three glyphs.
    ("\U0001f33f", "Overall Status:"),
    ("\U0001f7e1", "Overall Status:"),
    ("\U0001f534", "Overall Status:"),
]


def test_status_rows_share_one_value_column():
    """All labels and icons are padded by measurement, so values line up.

    The icons are not all the same width -- the ones carrying U+FE0F take a
    single cell -- which is why the rows used to be hand-spaced and why three of
    them sat one column off from the other eight.
    """
    columns = {display_width(_status_row(icon, label, "")) for icon, label in STATUS_ROWS}
    assert columns == {_ICON_SLOT + 1 + _LABEL_SLOT + 1}, columns


def test_the_label_field_is_sized_for_every_label_the_report_prints():
    """_ROW_LABELS is what sizes the field, so it has to list the real labels.

    "Overall Status:" is a character longer than "Top Processes:" and was missing
    from the table the field was measured against, so the verdict pushed its own
    value one column right of the other ten rows.
    """
    printed = {label for _, label in STATUS_ROWS}
    assert set(_ROW_LABELS) <= printed, set(_ROW_LABELS) - printed
    assert max(len(label) for label in printed) == _LABEL_SLOT, _LABEL_SLOT


def test_status_row_keeps_the_value_untouched():
    row = _status_row("\U0001f4bf", "Disk:", "50.0%  (1 GB / 2 GB)")
    assert row.endswith("50.0%  (1 GB / 2 GB)")
    assert row.startswith("\U0001f4bf Disk:")


# Colors resolve to empty strings whenever stdout is not a TTY, which is always
# the case under pytest -- so a test that compared real escapes would compare ""
# against "" and pass whichever band it actually hit. These sentinels give the
# four temperature bands distinguishable values for the length of one test.
COLOR_MARKERS = {"GREEN": "<green>", "YELLOW": "<yellow>", "RED": "<red>", "WHITE": "<white>"}


def _marked_colors():
    """Patch the color names ``status`` reads with legible sentinels."""
    stack = ExitStack()
    for name, marker in COLOR_MARKERS.items():
        stack.enter_context(patch(f"src.status.{name}", marker))
    return stack


def test_get_mem_info():
    mock_data = """MemTotal:       16000000 kB
MemAvailable:    8000000 kB
"""
    with patch("builtins.open", mock_open(read_data=mock_data)):
        used, total, percent = get_mem_info()
        # used = (16000000 - 8000000) * 1024 = 8192000000 bytes = 7.6GiB
        # total = 16000000 * 1024 = 16384000000 bytes = 15.3GiB
        assert "7." in used
        assert "15." in total
        assert percent == 50.0


def test_get_uptime():
    mock_data = "3660.00 7000.00"  # 3660 seconds = 1h 1m
    with patch("builtins.open", mock_open(read_data=mock_data)):
        uptime = get_uptime()
        assert uptime == "1h 1m"


def test_get_uptime_switches_to_days_past_the_first_one():
    """An hour count is only readable for about a day.

    A server up three months printed "2160h 5m", where neither number tells the
    reader anything. 23h59m is still hours; the day count starts one minute later.
    """
    cases = {
        23 * 3600 + 59 * 60: "23h 59m",
        24 * 3600: "1d 0h",
        26 * 3600 + 5 * 60: "1d 2h",
        90 * 24 * 3600 + 3600: "90d 1h",
    }
    for seconds, expected in cases.items():
        with patch("builtins.open", mock_open(read_data=f"{seconds}.00 0.00")):
            assert get_uptime() == expected, seconds


def test_get_cpu_load_summary():
    with (
        patch("os.getloadavg", return_value=(1.0, 0.8, 0.5)),
        patch("os.cpu_count", return_value=4),
    ):
        percent, summary = get_cpu_load_summary()

    assert summary == "load 25%"
    # The number is handed over unrounded: the assessment used to parse it back
    # out of the string, which had already lost everything below a whole percent.
    assert percent == 25.0


def test_get_cpu_temp_hwmon_dedicated():
    def mock_hwmon_glob(self, pattern):
        if "hwmon*" in pattern:
            return [Path("/sys/class/hwmon/hwmon5")]
        if "temp*_input" in pattern:
            return [Path("/sys/class/hwmon/hwmon5/temp1_input")]
        return []

    def mock_read_text(self, *args, **kwargs):
        p = str(self)
        if p.endswith("/name"):
            return "k10temp\n"
        if p.endswith("/temp1_label"):
            return "Tctl\n"
        if p.endswith("/temp1_input"):
            return "52000\n"
        return ""

    with (
        patch("pathlib.Path.exists", return_value=True),
        patch("pathlib.Path.glob", mock_hwmon_glob),
        patch("pathlib.Path.read_text", mock_read_text),
    ):
        val, text = get_cpu_temp()
        assert val == 52.0
        assert text == "52°C"


def test_get_cpu_temp_missing():
    with patch("pathlib.Path.exists", return_value=False):
        val, text = get_cpu_temp()
        assert val is None
        assert text == "N/A"


def test_a_die_reading_outranks_a_hotter_control_temperature():
    """k10temp's Tctl carries a deliberate offset; Tdie is the measured silicon.

    Threadripper and the early X-series add +27C to Tctl for the fan curve and
    expose both channels. They used to share one priority, and ties are broken by
    taking the hotter reading, so Tctl won every time and the report ran 27C high
    on exactly the machines that publish the truthful channel next to it.
    """
    hwmon = Path("/sys/class/hwmon/hwmon2")

    def mock_glob(self, pattern):
        if "hwmon*" in pattern:
            return [hwmon]
        if "temp*_input" in pattern:
            return [hwmon / "temp1_input", hwmon / "temp2_input"]
        return []

    def mock_read_text(self, *args, **kwargs):
        return {
            "name": "k10temp\n",
            "temp1_label": "Tctl\n",
            "temp1_input": "70000\n",
            "temp2_label": "Tdie\n",
            "temp2_input": "43000\n",
        }.get(self.name, "")

    with (
        patch("pathlib.Path.exists", return_value=True),
        patch("pathlib.Path.glob", mock_glob),
        patch("pathlib.Path.read_text", mock_read_text),
    ):
        assert get_cpu_temp() == (43.0, "43°C")


def test_both_sensor_probes_accept_one_shared_hwmon_walk():
    """show_status walks /sys/class/hwmon once and hands the list to both probes.

    They read different files out of the same directories, and each probe used to
    list the tree and read every `name` for itself -- twice the traversal for one
    report, on a laptop with a dozen nodes.
    """
    hwmon_dir = Path("/sys/class/hwmon/hwmon0")
    walk = [(hwmon_dir, "coretemp")]

    def mock_glob(self, pattern):
        if "hwmon*" in pattern:
            raise AssertionError("re-listed /sys/class/hwmon instead of using the walk")
        if "temp*_input" in pattern:
            return [hwmon_dir / "temp1_input"]
        if "fan*_input" in pattern:
            return [hwmon_dir / "fan1_input"]
        return []

    def mock_read_text(self, *args, **kwargs):
        if self.name == "name":
            raise AssertionError("re-read a driver name the walk already carried")
        return {
            "temp1_label": "Package id 0\n",
            "temp1_input": "51000\n",
            "fan1_input": "2400\n",
        }.get(self.name, "")

    with (
        patch("pathlib.Path.exists", return_value=True),
        patch("pathlib.Path.glob", mock_glob),
        patch("pathlib.Path.read_text", mock_read_text),
    ):
        assert get_cpu_temp(walk) == (51.0, "51°C")
        assert get_fan_speed(walk) == "coretemp: 2400 RPM"


def test_iter_hwmon_reads_each_driver_name_once():
    reads: list[str] = []

    def mock_glob(self, pattern):
        return [Path("/sys/class/hwmon/hwmon0"), Path("/sys/class/hwmon/hwmon1")]

    def mock_read_text(self, *args, **kwargs):
        reads.append(str(self))
        return "acpitz\n"

    with (
        patch("pathlib.Path.exists", return_value=True),
        patch("pathlib.Path.glob", mock_glob),
        patch("pathlib.Path.read_text", mock_read_text),
    ):
        walk = _iter_hwmon()

    assert walk == [
        (Path("/sys/class/hwmon/hwmon0"), "acpitz"),
        (Path("/sys/class/hwmon/hwmon1"), "acpitz"),
    ]
    assert reads == ["/sys/class/hwmon/hwmon0/name", "/sys/class/hwmon/hwmon1/name"]


def test_get_cpu_temp_does_not_read_a_sensor_its_label_disqualifies():
    """The label decides whether a channel counts, so it is read first.

    An nvme temp*_input costs a controller round-trip (~0.5 ms on this box, 20x
    a label read), and a laptop exposes a dozen channels that lose anyway.
    """
    hwmon = Path("/sys/class/hwmon/hwmon3")
    read = []

    def mock_glob(self, pattern):
        if "hwmon*" in pattern:
            return [hwmon]
        if "temp*_input" in pattern:
            return [hwmon / "temp1_input", hwmon / "temp2_input"]
        return []

    def mock_read_text(self, *args, **kwargs):
        read.append(self.name)
        if self.name == "name":
            return "nvme\n"  # not a CPU driver, so labels decide
        if self.name == "temp1_label":
            return "Composite\n"  # the SSD itself
        if self.name == "temp2_label":
            return "CPU\n"  # a board probe that does count
        if self.name == "temp2_input":
            return "48000\n"
        raise AssertionError(f"read a channel the label already ruled out: {self}")

    with (
        patch("pathlib.Path.exists", return_value=True),
        patch("pathlib.Path.glob", mock_glob),
        patch("pathlib.Path.read_text", mock_read_text),
    ):
        assert get_cpu_temp() == (48.0, "48°C")

    assert "temp1_input" not in read, read


def test_get_temp_color_covers_both_thresholds():
    with _marked_colors():
        assert get_temp_color(0.0) == "<green>"
        assert get_temp_color(TEMP_WARN_C) == "<green>"
        assert get_temp_color(TEMP_WARN_C + 0.1) == "<yellow>"
        assert get_temp_color(TEMP_HOT_C) == "<yellow>"
        assert get_temp_color(TEMP_HOT_C + 0.1) == "<red>"


def test_an_unreadable_sensor_does_not_borrow_green():
    """No reading is not a healthy reading.

    ``get_cpu_temp`` used to answer ``0`` for a missing sensor, which fell into
    the green band and printed "N/A" in the color that means "all good".
    """
    with _marked_colors():
        assert get_temp_color(None) == "<white>"


def test_get_default_route_interface_uses_lowest_metric(tmp_path):
    route_file = tmp_path / "route"
    route_file.write_text(
        "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\tMTU\tWindow\tIRTT\n"
        "wlan0\t00000000\t0101A8C0\t0003\t0\t0\t600\t00000000\t0\t0\t0\n"
        "eth0\t00000000\t0101A8C0\t0003\t0\t0\t100\t00000000\t0\t0\t0\n"
    )

    assert _get_default_route_interface(route_file) == "eth0"


def test_get_ip_info_reads_local_interface_without_external_connect():
    ioctl_response = b"\x00" * 20 + b"\xc0\xa8\x01\x0a" + b"\x00" * 232
    with (
        patch("src.status._get_default_route_interface", return_value="wlan0"),
        patch("src.status.fcntl.ioctl", return_value=ioctl_response),
        patch("src.status.socket.socket") as mock_socket,
    ):
        sock = mock_socket.return_value.__enter__.return_value
        sock.fileno.return_value = 3
        local_ip = get_ip_info()

    assert local_ip == "192.168.1.10"
    mock_socket.assert_called_once_with(socket.AF_INET, socket.SOCK_DGRAM)
    sock.connect.assert_not_called()


def test_get_network_traffic_filters_virtual_interfaces():
    # eth0: physical (1000 bytes rx, 2000 bytes tx)
    # docker0: virtual (5000 bytes rx, 5000 bytes tx) -> ignored
    # veth123: virtual (3000 bytes rx, 3000 bytes tx) -> ignored
    # lo: loopback -> ignored
    net_dev_content = (
        "Inter-|   Receive                                                |  Transmit\n"
        " face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed\n"
        "    lo: 1000000       0    0    0    0     0          0         0  1000000       0    0    0    0     0       0          0\n"
        "  eth0:    1024       1    0    0    0     0          0         0     2048       1    0    0    0     0       0          0\n"
        "docker0: 500000       0    0    0    0     0          0         0   500000       0    0    0    0     0       0          0\n"
        "veth123: 300000       0    0    0    0     0          0         0   300000       0    0    0    0     0       0          0\n"
    )

    with (
        patch("builtins.open", mock_open(read_data=net_dev_content)),
        patch("pathlib.Path.exists", return_value=True),
    ):
        rx, tx = get_network_traffic()

    assert rx == "1.0 KiB"
    assert tx == "2.0 KiB"


def test_get_network_traffic_falls_back_to_container_default_route():
    net_dev_content = (
        "Inter-|   Receive |  Transmit\n"
        " face |bytes packets errs drop fifo frame compressed multicast|bytes packets errs drop fifo colls carrier compressed\n"
        "  eth0:    3072 0 0 0 0 0 0 0     4096 0 0 0 0 0 0 0\n"
    )

    with (
        patch("builtins.open", mock_open(read_data=net_dev_content)),
        patch("pathlib.Path.exists", return_value=False),
        patch("src.status._get_default_route_interface", return_value="eth0"),
    ):
        rx, tx = get_network_traffic()

    assert rx == "3.0 KiB"
    assert tx == "4.0 KiB"


def test_get_network_traffic_keeps_lowpan_and_filters_unbacked_interface():
    net_dev_content = (
        "Inter-|   Receive |  Transmit\n"
        " face |bytes packets errs drop fifo frame compressed multicast|bytes packets errs drop fifo colls carrier compressed\n"
        "lowpan0:    1024 0 0 0 0 0 0 0     2048 0 0 0 0 0 0 0\n"
        "custom0:    8192 0 0 0 0 0 0 0     8192 0 0 0 0 0 0 0\n"
    )

    def mock_exists(path):
        return str(path).endswith("/lowpan0/device")

    with (
        patch("builtins.open", mock_open(read_data=net_dev_content)),
        patch("pathlib.Path.exists", mock_exists),
    ):
        rx, tx = get_network_traffic()

    assert rx == "1.0 KiB"
    assert tx == "2.0 KiB"


@contextmanager
def _fake_batteries(packs):
    """Present /sys/class/power_supply as holding exactly ``packs``.

    ``packs`` maps a battery directory name to ``{attribute: contents}``. An
    attribute the mapping omits raises OSError, the way reading an absent sysfs
    file does -- which is how the "machine has no cycle_count" and "the pack
    reports charge_* instead of energy_*" cases are expressed.
    """

    def glob(self, pattern):
        if str(self) == "/sys/class/power_supply" and pattern == "BAT*":
            return [Path(f"/sys/class/power_supply/{name}") for name in packs]
        return []

    def read_text(self, *args, **kwargs):
        attributes = packs.get(Path(str(self)).parent.name, {})
        if self.name not in attributes:
            raise OSError(f"no such attribute: {self}")
        return attributes[self.name]

    with (
        patch("src.status.Path.glob", glob),
        patch("src.status.Path.read_text", read_text),
    ):
        yield


def test_get_battery_info():
    packs = {
        "BAT0": {
            "capacity": "80\n",
            "energy_full_design": "5000\n",
            "energy_full": "4500\n",  # 90% health
            "cycle_count": "100\n",
        }
    }
    with _fake_batteries(packs):
        val, health, details = get_battery_info()

    assert val == 80
    # Health comes back as a number as well as inside the details text: the
    # assessment needs the value and used to recover it by parsing the string.
    assert health == 90.0
    assert "Health: 90.0%" in details
    assert "Cycles: 100" in details


def test_get_battery_health_capped_at_100():
    packs = {
        "BAT0": {
            "capacity": "95\n",
            "energy_full_design": "5000\n",
            "energy_full": "5200\n",  # full > design -> would be >100%
        }
    }
    with _fake_batteries(packs):
        _, health, details = get_battery_info()

    assert health == 100.0
    assert "Health: 100.0%" in details


def test_battery_details_are_not_pre_parenthesized():
    """show_status() wraps the details in parens, so they must not carry their own.

    Otherwise the row renders as "🔋 Battery: ... ((Health: 100.0%) | Cycles: N)".
    """
    packs = {
        "BAT0": {
            "capacity": "80\n",
            "energy_full_design": "5000\n",
            "energy_full": "4500\n",
            "cycle_count": "244\n",
        }
    }
    with _fake_batteries(packs):
        _, _, details = get_battery_info()

    assert "(" not in details and ")" not in details, details
    assert details == "Health: 90.0% | Cycles: 244"


def test_battery_details_start_with_the_cycle_count_when_health_is_unknown():
    """The details are joined, not concatenated.

    A pack that reports cycles but no design capacity -- some ThinkPads, and
    anything whose EC is behind a firmware update -- used to render its details as
    "| Cycles: 244", with a separator in front of the only field there was.
    """
    packs = {"BAT0": {"capacity": "77\n", "cycle_count": "244\n"}}
    with _fake_batteries(packs):
        assert get_battery_info() == (77, None, "Cycles: 244")


def test_battery_is_found_when_the_pack_is_not_numbered_zero():
    """Docked second packs number BAT0/BAT1, and some machines only expose BAT1."""
    with _fake_batteries({"BAT1": {"capacity": "62\n"}}):
        assert get_battery_info() == (62, None, "")

    # A VM's stub BAT0 answers nothing usable; it must not shadow the real pack.
    with _fake_batteries({"BAT0": {"capacity": "bad"}, "BAT1": {"capacity": "41\n"}}):
        assert get_battery_info() == (41, None, "")


def test_battery_health_reads_a_charge_based_pack():
    """ACPI fills in energy_* or charge_* but never both; health needs either."""
    packs = {
        "BAT0": {
            "capacity": "55\n",
            "charge_full_design": "4000\n",
            "charge_full": "3000\n",
        }
    }
    with _fake_batteries(packs):
        _, health, details = get_battery_info()

    assert health == 75.0
    assert details == "Health: 75.0%"


def test_get_gpu_info_multi_gpu_uses_first_line():
    out = "55, 10\n60, 20\n"
    with (
        patch("src.status.shutil.which", return_value="/usr/bin/nvidia-smi"),
        patch("src.status.run_command", return_value=MagicMock(ok=True, stdout=out)),
    ):
        result = get_gpu_info()

    assert result is not None
    assert "55°C" in result
    assert "load 10%" in result


def test_get_gpu_info_drm_hwmon_junction_priority():
    def mock_drm_glob(self, pattern):
        if pattern.startswith("card"):
            return [Path("/sys/class/drm/card0")]
        if "hwmon*" in pattern:
            return [Path("/sys/class/drm/card0/device/hwmon/hwmon1")]
        if "temp*_input" in pattern:
            return [
                Path("/sys/class/drm/card0/device/hwmon/hwmon1/temp1_input"),
                Path("/sys/class/drm/card0/device/hwmon/hwmon1/temp2_input"),
            ]
        return []

    def mock_read_text(self, *args, **kwargs):
        p = str(self)
        if p.endswith("/temp1_input"):
            return "45000\n"
        if p.endswith("/temp1_label"):
            return "edge\n"
        if p.endswith("/temp2_input"):
            return "58000\n"
        if p.endswith("/temp2_label"):
            return "junction\n"
        if p.endswith("/gpu_busy_percent"):
            return "22\n"
        return ""

    with (
        patch("src.status.shutil.which", return_value=None),
        patch("pathlib.Path.exists", return_value=True),
        patch("pathlib.Path.glob", mock_drm_glob),
        patch("pathlib.Path.read_text", mock_read_text),
        patch("builtins.open", mock_open(read_data="22\n")),
    ):
        result = get_gpu_info()

    assert result is not None
    assert "58°C" in result
    assert "load 22%" in result


def test_get_gpu_info_drm_reads_temperature_without_utilization_node():
    def mock_drm_glob(self, pattern):
        if pattern.startswith("card"):
            return [Path("/sys/class/drm/card0")]
        if "hwmon*" in pattern:
            return [Path("/sys/class/drm/card0/device/hwmon/hwmon1")]
        if "temp*_input" in pattern:
            return [Path("/sys/class/drm/card0/device/hwmon/hwmon1/temp1_input")]
        return []

    def mock_exists(self):
        return not str(self).endswith("/gpu_busy_percent")

    def mock_read_text(self, *args, **kwargs):
        if str(self).endswith("/temp1_input"):
            return "61000\n"
        if str(self).endswith("/temp1_label"):
            return "hotspot\n"
        return ""

    with (
        patch("src.status.shutil.which", return_value=None),
        patch("pathlib.Path.exists", mock_exists),
        patch("pathlib.Path.glob", mock_drm_glob),
        patch("pathlib.Path.read_text", mock_read_text),
    ):
        result = get_gpu_info()

    assert result is not None
    assert "61°C" in result
    assert "load" not in result


def test_gpu_probe_skips_the_connector_directories_beside_the_card():
    """/sys/class/drm holds one card<N>-<CONNECTOR> directory per display output.

    Those match a plain "card*" and carry a `device` link of their own, pointing
    at the card rather than at the GPU's PCI device -- so the existing check
    cannot tell them apart. A laptop with four connectors ran the whole probe
    body five times, four of them re-reading one card's hwmon.
    """
    names = ("card1", "card1-DP-1", "card1-DP-2", "card1-eDP-1", "card1-HDMI-A-1")
    touched: list[str] = []

    def note(path):
        if "card1-" in str(path):
            touched.append(str(path))

    def mock_drm_glob(self, pattern):
        if pattern.startswith("card"):
            return [Path(f"/sys/class/drm/{name}") for name in names]
        if "hwmon*" in pattern:
            return [Path("/sys/class/drm/card1/device/hwmon/hwmon2")]
        if "temp*_input" in pattern:
            return [Path("/sys/class/drm/card1/device/hwmon/hwmon2/temp1_input")]
        return []

    def mock_exists(self):
        note(self)
        return not str(self).endswith("/gpu_busy_percent")

    def mock_read_text(self, *args, **kwargs):
        note(self)
        if self.name == "temp1_label":
            return "edge\n"
        if self.name == "temp1_input":
            return "49000\n"
        return ""

    with (
        patch("src.status.shutil.which", return_value=None),
        patch("pathlib.Path.exists", mock_exists),
        patch("pathlib.Path.glob", mock_drm_glob),
        patch("pathlib.Path.read_text", mock_read_text),
    ):
        result = get_gpu_info()

    assert result is not None and "49°C" in result
    assert touched == [], touched


# --- percent rows ---
# Every probe show_status() reads, pinned to something uninteresting. A test
# overrides just the one row it is about, so a report always renders even on a
# machine with no battery, no discrete GPU and no fan sensor.
QUIET_PROBES = {
    "get_uptime": "1h",
    "get_cpu_load_summary": (5.0, "load 5%"),
    "get_cpu_temp": (40.0, "40°C"),
    "get_fan_speed": None,
    "get_mem_info": ("1.0 GiB", "8.0 GiB", 12.5),
    "get_battery_info": None,
    "get_network_traffic": ("0 B", "0 B"),
    "get_ip_info": "127.0.0.1",
    "get_gpu_info": None,
    "get_top_processes": [],
}


def _render_report(capsys, disk=(1, 2), **overrides):
    """Render one whole report with every probe pinned, and return its text."""
    probes = {**QUIET_PROBES, **overrides}
    probes.setdefault("get_disk_rows", [("/", *disk)])
    with ExitStack() as stack:
        for name, value in probes.items():
            stack.enter_context(patch(f"src.status.{name}", return_value=value))
        show_status()

    return capsys.readouterr().out


def _render_status_raw(capsys, label, disk=(1, 2), **overrides):
    """Render one report and return the row carrying ``label``, colors intact."""
    out = _render_report(capsys, disk=disk, **overrides)
    return next(line for line in out.splitlines() if label in ANSI_CSI_RE.sub("", line))


def _render_status(capsys, label, disk=(1, 2), **overrides):
    """Render one report and return the row carrying ``label``, colors stripped."""
    return ANSI_CSI_RE.sub("", _render_status_raw(capsys, label, disk=disk, **overrides))


def test_the_cpu_row_dims_an_unreadable_temperature(capsys):
    with _marked_colors():
        row = _render_status_raw(capsys, "CPU Status:", get_cpu_temp=(None, "N/A"))

    assert "<white>N/A" in row, row
    assert "<green>" not in row, row


def test_the_cpu_row_still_colors_a_real_temperature(capsys):
    with _marked_colors():
        hot = _render_status_raw(capsys, "CPU Status:", get_cpu_temp=(95.0, "95°C"))
        warm = _render_status_raw(capsys, "CPU Status:", get_cpu_temp=(70.0, "70°C"))
        cool = _render_status_raw(capsys, "CPU Status:", get_cpu_temp=(40.0, "40°C"))

    assert "<red>95°C" in hot, hot
    assert "<yellow>70°C" in warm, warm
    assert "<green>40°C" in cool, cool


# One tiny-but-nonzero share per percent row. 4096 against 8 GiB is 4.77e-05%,
# which '.1f' prints as "0.0" -- a freshly-formatted disk or a battery down to
# its last flicker really does sit here, and reading it as "nothing" contradicts
# the byte counts printed on the same line.
TINY = 4.76837158203125e-05
TINY_SHARE_ROWS = [
    ("Memory:", {"get_mem_info": ("4.0 KiB", "8.0 GiB", TINY)}),
    ("Disk:", {"disk": (4096, 8 * 1024**3)}),
    ("Battery:", {"get_battery_info": (TINY, None, "")}),
]

# The same rows at a share that needs no bound, for the column comparison below.
HALF_SHARE_ROWS = {
    "Memory:": {"get_mem_info": ("4.0 GiB", "8.0 GiB", 50.0)},
    "Disk:": {},
    "Battery:": {"get_battery_info": (50.0, None, "")},
}

ROW_IDS = [label.rstrip(":") for label, _ in TINY_SHARE_ROWS]


@pytest.mark.parametrize(("label", "overrides"), TINY_SHARE_ROWS, ids=ROW_IDS)
def test_status_rows_mark_a_share_that_rounds_away(capsys, label, overrides):
    row = _render_status(capsys, label, **overrides)

    assert "<0.1%" in row, row
    assert "0.0%" not in row, row


@pytest.mark.parametrize(("label", "overrides"), TINY_SHARE_ROWS, ids=ROW_IDS)
def test_status_rows_hold_the_value_column_across_the_boundary(capsys, label, overrides):
    # The rows are built by concatenation, so a percent field that changed width
    # on small shares would step the text behind it out of line with the report.
    # The first '%' in a row closes that field, whatever the detail text says.
    tiny = _render_status(capsys, label, **overrides)
    plain = _render_status(capsys, label, **HALF_SHARE_ROWS[label])

    assert display_width(tiny[: tiny.index("%") + 1]) == display_width(
        plain[: plain.index("%") + 1]
    ), (tiny, plain)


def test_status_row_keeps_a_share_that_rounds_to_a_tenth(capsys):
    # From 0.05 up the rounded number is a real reading of the share, so the
    # bound must not swallow it.
    row = _render_status(capsys, "Memory:", get_mem_info=("64 MiB", "128 MiB", 0.06))

    assert "0.1%" in row
    assert "<" not in row


def test_status_row_keeps_the_number_at_exactly_zero(capsys):
    row = _render_status(capsys, "Disk:", disk=(0, 8 * 1024**3))

    assert "0.0%" in row
    assert "<" not in row


def test_the_battery_icon_switches_at_the_low_charge_boundary(capsys):
    """The icon marks low charge on the same 20% line the row's color uses.

    20 itself is still the yellow band, so it keeps the full glyph -- the swap
    belongs to the red band below it.
    """
    at_boundary = _render_status(capsys, "Battery:", get_battery_info=(20, None, ""))
    below = _render_status(capsys, "Battery:", get_battery_info=(19, None, ""))

    assert at_boundary.startswith("\U0001f50b"), at_boundary
    assert below.startswith("\U0001faab"), below


def test_system_health_assessment_optimal():
    icon, color, verdict = get_system_health_assessment(
        cpu_temp_c=45.0,
        cpu_load_percent=5.0,
        mem_percent=40.0,
        disk_percent=20.0,
        battery_health=100.0,
    )
    assert icon == "🌿"
    assert "optimal" in verdict


def test_system_health_assessment_moderate():
    icon, color, verdict = get_system_health_assessment(
        cpu_temp_c=72.0,
        cpu_load_percent=50.0,
        mem_percent=82.0,
        disk_percent=60.0,
        battery_health=None,
    )
    assert icon == "🟡"
    assert "moderate" in verdict


def test_system_health_assessment_warning_heavy_load():
    icon, color, verdict = get_system_health_assessment(
        cpu_temp_c=85.0,
        cpu_load_percent=95.0,
        mem_percent=92.0,
        disk_percent=94.0,
        battery_health=None,
    )
    assert icon == "🔴"
    assert "Disk space low" in verdict
    assert "Memory load critical" in verdict
    assert "CPU temperature hot" in verdict


def test_the_verdict_reads_the_thresholds_the_rows_do():
    """The assessment held its own copies of the temperature limits.

    Its "hot" line spelled 80.0 a second time -- equal to TEMP_HOT_C, but not tied
    to it, so retuning the row colour left the verdict behind. Its warning line is
    deliberately above the row's yellow one and now says so by name.
    """
    quiet = {"cpu_load_percent": 5.0, "mem_percent": 10.0, "disk_percent": 10.0}

    def verdict_at(temp_c):
        return get_system_health_assessment(temp_c, battery_health=None, **quiet)[2]

    assert "temperature" not in verdict_at(TEMP_ELEVATED_C)
    assert "elevated" in verdict_at(TEMP_ELEVATED_C + 0.1)
    assert "elevated" in verdict_at(TEMP_HOT_C)
    assert "hot" in verdict_at(TEMP_HOT_C + 0.1)
    # The row turns yellow at TEMP_WARN_C; the verdict deliberately stays quiet
    # there, because plenty of mobile and Ryzen parts idle in the low 60s.
    assert "temperature" not in verdict_at(TEMP_WARN_C + 0.1)


def test_a_finding_is_ranked_by_its_own_severity_not_by_its_wording():
    """Severity used to be decided by searching the message for four keywords.

    "Battery degraded (40%)" was red because it happened to contain "degraded",
    and a pegged CPU was yellow because "CPU workload heavy (95%)" happened to
    contain none of them. Both verdicts are kept -- they are the right ones -- but
    they now come from the finding rather than from its prose.
    """
    quiet = {"mem_percent": 10.0, "disk_percent": 10.0}

    icon, _, verdict = get_system_health_assessment(
        45.0, cpu_load_percent=95.0, battery_health=None, **quiet
    )
    # A compile or a render pegs every core; that is a workload, not a fault.
    assert icon == "🟡", verdict
    assert "CPU workload heavy (95%)" in verdict

    icon, _, verdict = get_system_health_assessment(
        45.0, cpu_load_percent=5.0, battery_health=40.0, **quiet
    )
    assert icon == "🔴", verdict
    assert "Battery degraded (40%)" in verdict


def test_proc_readers_and_cpu_load_errors():
    with patch("builtins.open", side_effect=OSError):
        assert get_mem_info() == ("Unknown", "Unknown", 0)
        assert get_uptime() == "Unknown"
    with patch("os.getloadavg", side_effect=OSError):
        assert get_cpu_load_summary() == (None, "load N/A")


def test_battery_missing_and_malformed_optional_fields():
    with _fake_batteries({}):
        assert get_battery_info() is None

    # A pack whose capacity does not parse is not a pack at 0%: the old code
    # answered (0, "N/A", "") and show_status drew an empty bar at 0.0%, which
    # reads as "about to die" rather than "unreadable".
    with _fake_batteries({"BAT0": {"capacity": "bad"}}):
        assert get_battery_info() is None

    # A design capacity of 0 makes health meaningless -- the row keeps the
    # charge and drops the health text rather than dividing by zero.
    with _fake_batteries({"BAT0": {"capacity": "70", "energy_full_design": "0"}}):
        assert get_battery_info() == (70, None, "")


def test_an_empty_power_supply_directory_is_not_a_battery():
    """A desktop has no BAT* at all; the glob failing is the same answer."""
    with patch("src.status.Path.glob", side_effect=OSError):
        assert get_battery_info() is None


def test_network_and_route_error_branches(tmp_path):
    with patch("builtins.open", side_effect=OSError):
        assert get_network_traffic() == ("N/A", "N/A")
    route = tmp_path / "route"
    route.write_text("header\nmalformed\neth0 00000000 x bad 0 0 x\neth1 00000000 x 0000 0 0 20\n")
    assert _get_default_route_interface(route) is None
    with patch("src.status.socket.socket", side_effect=OSError):
        assert _get_interface_ipv4("eth0") is None
    assert _get_interface_ipv4("") is None


def test_network_unselected_eligible_interface():
    content = "h\nh\neth0: 10 0 0 0 0 0 0 0 20 0 0 0 0 0 0 0\n"
    with (
        patch("builtins.open", mock_open(read_data=content)),
        patch("src.status.Path.exists", return_value=False),
        patch("src.status._get_default_route_interface", return_value="wlan0"),
    ):
        assert get_network_traffic() == ("N/A", "N/A")


def test_cpu_temp_fallback_thermal_and_invalid_hwmon():
    def glob(self, pattern):
        if str(self) == "/sys/class/hwmon":
            return []
        if "thermal_zone" in pattern:
            return [Path("/sys/class/thermal/thermal_zone0")]
        return []

    def read(self, *args, **kwargs):
        return "x86_pkg_temp\n" if str(self).endswith("/type") else "65000\n"

    with (
        patch("src.status.Path.exists", return_value=True),
        patch("src.status.Path.glob", glob),
        patch("src.status.Path.read_text", read),
    ):
        assert get_cpu_temp() == (65.0, "65°C")


def test_fan_speed_reads_labels_deduplicates_and_handles_empty():
    dirs = [Path("/sys/class/hwmon/hwmon0"), Path("/sys/class/hwmon/hwmon1")]

    def glob(self, pattern):
        if "hwmon*" in pattern:
            return dirs
        if "fan*_input" in pattern:
            return [self / "fan1_input", self / "fan2_input"]
        return []

    def read(self, *args, **kwargs):
        if self.name == "name":
            return "thinkpad\n"
        if self.name == "fan1_label":
            return "CPU Fan\n"
        if self.name == "fan1_input":
            return "1200\n"
        # fan2 is a header the board wires up but nothing is plugged into.
        return "0\n"

    with (
        patch("src.status.Path.exists", return_value=True),
        patch("src.status.Path.glob", glob),
        patch("src.status.Path.read_text", read),
    ):
        # Two controllers, one physical fan: the same entry twice collapses to one.
        assert get_fan_speed() == "CPU Fan: 1200 RPM"

    with patch("src.status.Path.exists", return_value=False):
        assert get_fan_speed() is None


def test_gpu_nvidia_invalid_and_drm_empty():
    with (
        patch("src.status.shutil.which", return_value="/usr/bin/nvidia-smi"),
        patch("src.status.run_command", return_value=MagicMock(ok=True, stdout="bad")),
        patch("src.status.Path.exists", return_value=False),
    ):
        assert get_gpu_info() is None
    with (
        patch("src.status.shutil.which", return_value=None),
        patch("src.status.Path.exists", return_value=False),
    ):
        assert get_gpu_info() is None


def test_top_processes_success_sorting_and_failure():
    output = "chrome 204800\nchrome 1024\nvim bad\npython 100\n"
    with patch("src.status.run_command", return_value=MagicMock(ok=True, stdout=output)):
        result = __import__("src.status", fromlist=["get_top_processes"]).get_top_processes()
        assert result[0].startswith("chrome")
    with patch("src.status.run_command", return_value=MagicMock(ok=False, stdout="")):
        assert __import__("src.status", fromlist=["get_top_processes"]).get_top_processes() == []


def test_an_unreadable_load_and_a_healthy_pack_are_not_findings():
    """An unreadable load and a healthy pack are both non-findings.

    ``None`` for the load used to arrive as the string "load N/A", which the
    assessment parsed with a bare float() -- so a kernel without /proc/loadavg
    replaced the verdict with a ValueError traceback.
    """
    icon, _, message = get_system_health_assessment(50, None, 40, 81, 80.0)
    assert icon == "🟡" and "Disk usage high" in message
    assert "workload" not in message, message


def test_show_status_optional_rows(capsys):
    overrides = {
        **QUIET_PROBES,
        "get_gpu_info": "55°C | load 10%",
        "get_fan_speed": "1200 RPM",
        "get_battery_info": (80, 90.0, "Health: 90.0%"),
        "get_top_processes": ["chrome (200MB)"],
        "get_disk_rows": [("/", 1, 2)],
    }
    with ExitStack() as stack:
        for name, value in overrides.items():
            stack.enter_context(patch(f"src.status.{name}", return_value=value))
        show_status()
    output = capsys.readouterr().out
    assert "GPU Status:" in output and "Fan Speed:" in output and "Top Processes:" in output


# --- disk rows ---


def _usage(used, total):
    return MagicMock(used=used, total=total)


def test_disk_rows_report_each_distinct_filesystem():
    """Debian's guided partitioning offers separate /home and /var, and a full
    /var breaks apt long before $HOME notices."""
    sizes = {"/": (90, 100), os.path.expanduser("~"): (10, 100), "/var": (50, 60)}

    with patch("src.status.shutil.disk_usage", lambda path: _usage(*sizes[path])):
        assert get_disk_rows() == [("/", 90, 100), ("~", 10, 100), ("/var", 50, 60)]


def test_disk_rows_collapse_paths_that_measure_the_same_filesystem():
    """btrfs gives every subvolume its own st_dev, so / and $HOME can differ by
    device while reporting one pool -- the row must not print three times."""
    with patch("src.status.shutil.disk_usage", return_value=_usage(42, 100)):
        assert get_disk_rows() == [("/", 42, 100)]


def test_disk_rows_drop_a_filesystem_they_cannot_measure():
    """A failing statfs is not a disk with zero bytes used."""

    def usage(path):
        if path == "/":
            raise OSError("statfs failed")
        return _usage(7, 0) if path == "/var" else _usage(3, 100)

    with patch("src.status.shutil.disk_usage", usage):
        assert get_disk_rows() == [("~", 3, 100)]


def test_the_report_labels_every_disk_row_once_a_second_one_shows(capsys):
    out = _render_report(capsys, get_disk_rows=[("/", 95, 100), ("~", 10, 100)])

    assert "Disk (/):" in out and "Disk (~):" in out, out
    assert "Disk:" not in out, out


def test_the_verdict_follows_the_fullest_filesystem(capsys):
    """The old report measured only $HOME's filesystem, so a full / went unsaid."""
    out = _render_report(capsys, get_disk_rows=[("/", 95, 100), ("~", 10, 100)])

    assert "Disk space low (95%)" in out, out


def test_the_report_drops_the_battery_row_when_the_pack_is_unreadable(capsys):
    """An unreadable pack used to arrive as (0, "N/A", "").

    That drew "🪫 Battery: ──── 0.0%" -- which is what a pack about to die looks
    like. This drives the real probe, so it covers both halves of the fix.
    """
    probes = {k: v for k, v in QUIET_PROBES.items() if k != "get_battery_info"}
    probes["get_disk_rows"] = [("/", 1, 2)]
    with ExitStack() as stack:
        for name, value in probes.items():
            stack.enter_context(patch(f"src.status.{name}", return_value=value))
        stack.enter_context(_fake_batteries({"BAT0": {"capacity": "bad"}}))
        show_status()

    out = capsys.readouterr().out
    assert "Battery:" not in out, out


def test_the_report_survives_a_kernel_that_answers_neither_load_nor_disk(capsys):
    """Nothing above show_status catches OSError.

    main() only handles KeyboardInterrupt, so a probe that raised replaced the
    whole report -- and, from the TUI, the session -- with a traceback.
    """
    probes = {k: v for k, v in QUIET_PROBES.items() if k != "get_cpu_load_summary"}
    with ExitStack() as stack:
        for name, value in probes.items():
            stack.enter_context(patch(f"src.status.{name}", return_value=value))
        stack.enter_context(patch("os.getloadavg", side_effect=OSError))
        stack.enter_context(patch("src.status.shutil.disk_usage", side_effect=OSError))
        show_status()

    out = capsys.readouterr().out
    assert "load N/A" in out, out
    assert "Disk" not in out, out
    assert "Overall Status:" in out, out
