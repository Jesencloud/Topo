import socket
from unittest.mock import MagicMock, mock_open, patch

from src.core.status import (
    _get_default_route_interface,
    _status_row,
    get_battery_info,
    get_cpu_load_summary,
    get_cpu_temp,
    get_gpu_info,
    get_ip_info,
    get_mem_info,
    get_uptime,
)
from src.core.text import display_width

# Every row show_status() can print, as (icon, label). Kept here rather than
# imported so that adding a row to the report without checking its alignment
# makes this list -- and the reviewer -- notice.
STATUS_ROWS = [
    ("⏱️", "Uptime:"),
    ("\U0001f4ca", "CPU Load:"),
    ("\U0001f321️", "CPU Temp:"),
    ("\U0001f3ae", "GPU Status:"),
    ("⚙️", "Fan Speed:"),
    ("\U0001f9e0", "Memory:"),
    ("\U0001f504", "Swap:"),
    ("⚡", "ZRAM RAM:"),
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
    assert columns == {display_width("\U0001f4ca ") + len("Top Processes: ")}, columns


def test_status_row_keeps_the_value_untouched():
    row = _status_row("\U0001f4be", "Disk:", "50.0%  (1 GB / 2 GB)")
    assert row.endswith("50.0%  (1 GB / 2 GB)")
    assert row.startswith("\U0001f4be Disk:")


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


def test_get_cpu_load_summary_is_user_readable():
    with (
        patch("os.getloadavg", return_value=(1.0, 0.8, 0.5)),
        patch("os.cpu_count", return_value=4),
    ):
        summary = get_cpu_load_summary()

    assert summary == "Low (25% of 4 cores; 1m 1.00, 5m 0.80, 15m 0.50)"


def test_get_cpu_load_summary_marks_overloaded():
    with (
        patch("os.getloadavg", return_value=(8.0, 6.0, 4.0)),
        patch("os.cpu_count", return_value=4),
    ):
        summary = get_cpu_load_summary()

    assert summary.startswith("Overloaded (200% of 4 cores")


def test_get_cpu_temp():
    mock_data = "45000"  # 45.0 C
    with (
        patch("pathlib.Path.exists", return_value=True),
        patch("builtins.open", mock_open(read_data=mock_data)),
    ):
        val, text = get_cpu_temp()
        assert val == 45.0
        assert text == "45.0°C"


def test_get_cpu_temp_missing():
    with patch("pathlib.Path.exists", return_value=False):
        val, text = get_cpu_temp()
        assert val == 0
        assert text == "N/A"


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
        patch("src.core.status._get_default_route_interface", return_value="wlan0"),
        patch("src.core.status.fcntl.ioctl", return_value=ioctl_response),
        patch("src.core.status.socket.socket") as mock_socket,
    ):
        sock = mock_socket.return_value.__enter__.return_value
        sock.fileno.return_value = 3
        local_ip = get_ip_info()

    assert local_ip == "192.168.1.10"
    mock_socket.assert_called_once_with(socket.AF_INET, socket.SOCK_DGRAM)
    sock.connect.assert_not_called()


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
    out = "10, 1024, 8192, 55\n20, 2048, 8192, 60\n"
    with (
        patch("src.core.status.shutil.which", return_value="/usr/bin/nvidia-smi"),
        patch("src.core.status.run_command", return_value=MagicMock(ok=True, stdout=out)),
    ):
        result = get_gpu_info()

    assert result is not None
    assert "NVIDIA: 10% util" in result
    assert "1.0GB" in result
