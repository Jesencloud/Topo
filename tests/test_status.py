import socket
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, mock_open, patch

import pytest

from src.core.text import display_width
from src.status import (
    TEMP_HOT_C,
    TEMP_WARN_C,
    _get_default_route_interface,
    _status_row,
    get_battery_info,
    get_cpu_load_summary,
    get_cpu_temp,
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
    ("📟", "CPU Status:"),
    ("\U0001f3ae", "GPU Status:"),
    ("⚙️", "Fan Speed:"),
    ("\U0001f9e0", "Memory:"),
    ("\U0001f4be", "Disk:"),
    ("\U0001f50b", "Battery:"),
    ("\U0001f310", "Network:"),
    ("\U0001f51d", "Top Processes:"),
]


def test_status_rows_share_one_value_column():
    """All labels and icons are padded by measurement, so values line up.

    The icons are not all the same width -- the ones carrying U+FE0F take a
    single cell -- which is why the rows used to be hand-spaced and why three of
    them sat one column off from the other eight.
    """
    columns = {display_width(_status_row(icon, label, "")) for icon, label in STATUS_ROWS}
    assert columns == {display_width("\U0001f4df ") + len("Top Processes: ")}, columns


def test_status_row_keeps_the_value_untouched():
    row = _status_row("\U0001f4be", "Disk:", "50.0%  (1 GB / 2 GB)")
    assert row.endswith("50.0%  (1 GB / 2 GB)")
    assert row.startswith("\U0001f4be Disk:")


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


def test_get_cpu_load_summary():
    with (
        patch("os.getloadavg", return_value=(1.0, 0.8, 0.5)),
        patch("os.cpu_count", return_value=4),
    ):
        summary = get_cpu_load_summary()

    assert summary == "load 25%"


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


def test_get_battery_info():
    # Mock battery data: capacity=80%, design=5000, full=4500 (90% health), cycles=100
    def battery_mock_open(path):
        if "capacity" in str(path):
            return mock_open(read_data="80\n")()
        if "energy_full_design" in str(path):
            return mock_open(read_data="5000\n")()
        if "energy_full" in str(path):
            return mock_open(read_data="4500\n")()
        if "cycle_count" in str(path):
            return mock_open(read_data="100\n")()
        return mock_open()()

    with (
        patch("pathlib.Path.exists", return_value=True),
        patch("builtins.open", side_effect=battery_mock_open),
    ):
        val, pct, details = get_battery_info()
        assert val == 80
        assert pct == "80%"
        assert "Health: 90.0%" in details
        assert "Cycles: 100" in details


def test_get_battery_health_capped_at_100():
    def battery_mock_open(path):
        if "capacity" in str(path):
            return mock_open(read_data="95\n")()
        if "energy_full_design" in str(path):
            return mock_open(read_data="5000\n")()
        if "energy_full" in str(path):
            return mock_open(read_data="5200\n")()  # full > design -> would be >100%
        return mock_open()()

    with (
        patch("pathlib.Path.exists", return_value=True),
        patch("builtins.open", side_effect=battery_mock_open),
    ):
        _, _, details = get_battery_info()

    assert "Health: 100.0%" in details


def test_battery_details_are_not_pre_parenthesized():
    """show_status() wraps the details in parens, so they must not carry their own.

    Otherwise the row renders as "🔋 Battery: ... ((Health: 100.0%) | Cycles: N)".
    """

    def battery_mock_open(path):
        if "capacity" in str(path):
            return mock_open(read_data="80\n")()
        if "energy_full_design" in str(path):
            return mock_open(read_data="5000\n")()
        if "energy_full" in str(path):
            return mock_open(read_data="4500\n")()
        if "cycle_count" in str(path):
            return mock_open(read_data="244\n")()
        return mock_open()()

    with (
        patch("pathlib.Path.exists", return_value=True),
        patch("builtins.open", side_effect=battery_mock_open),
    ):
        _, _, details = get_battery_info()

    assert "(" not in details and ")" not in details, details
    assert details.strip() == "Health: 90.0% | Cycles: 244"


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
        if "card*" in pattern:
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
        if "card*" in pattern:
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


# --- percent rows ---
# Every probe show_status() reads, pinned to something uninteresting. A test
# overrides just the one row it is about, so a report always renders even on a
# machine with no battery, no discrete GPU and no fan sensor.
QUIET_PROBES = {
    "get_uptime": "1h",
    "get_cpu_load_summary": "idle",
    "get_cpu_temp": (40.0, "40°C"),
    "get_fan_speed": None,
    "get_mem_info": ("1.0 GiB", "8.0 GiB", 12.5),
    "get_battery_info": None,
    "get_network_traffic": ("0 B", "0 B"),
    "get_ip_info": "127.0.0.1",
    "get_gpu_info": None,
    "get_top_processes": [],
}


def _render_status_raw(capsys, label, disk=(1, 2), **overrides):
    """Render one report and return the row carrying ``label``, colors intact."""
    probes = {**QUIET_PROBES, **overrides}
    used, total = disk
    with ExitStack() as stack:
        for name, value in probes.items():
            stack.enter_context(patch(f"src.status.{name}", return_value=value))
        stack.enter_context(
            patch(
                "src.status.shutil.disk_usage",
                return_value=MagicMock(used=used, total=total),
            )
        )
        show_status()

    out = capsys.readouterr().out
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
    ("Battery:", {"get_battery_info": (TINY, "0%", "")}),
]

# The same rows at a share that needs no bound, for the column comparison below.
HALF_SHARE_ROWS = {
    "Memory:": {"get_mem_info": ("4.0 GiB", "8.0 GiB", 50.0)},
    "Disk:": {},
    "Battery:": {"get_battery_info": (50.0, "50%", "")},
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


def test_system_health_assessment_optimal():
    icon, color, verdict = get_system_health_assessment(
        cpu_temp_c=45.0,
        cpu_load_str="load 5%",
        mem_percent=40.0,
        disk_percent=20.0,
        battery_data=(100, "100%", "Health: 100.0% | Cycles: 10"),
    )
    assert icon == "🌿"
    assert "optimal" in verdict


def test_system_health_assessment_moderate():
    icon, color, verdict = get_system_health_assessment(
        cpu_temp_c=72.0,
        cpu_load_str="load 50%",
        mem_percent=82.0,
        disk_percent=60.0,
        battery_data=None,
    )
    assert icon == "🟡"
    assert "moderate" in verdict


def test_system_health_assessment_warning_heavy_load():
    icon, color, verdict = get_system_health_assessment(
        cpu_temp_c=85.0,
        cpu_load_str="load 95%",
        mem_percent=92.0,
        disk_percent=94.0,
        battery_data=None,
    )
    assert icon == "🔴"
    assert "Disk space low" in verdict
    assert "Memory load critical" in verdict
    assert "CPU temperature hot" in verdict
