from unittest.mock import MagicMock, mock_open, patch

from src.core.status import (
    get_battery_info,
    get_cpu_load_summary,
    get_cpu_temp,
    get_gpu_info,
    get_ip_info,
    get_mem_info,
    get_uptime,
)


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


def test_get_ip_info_returns_local_ip():
    with patch("socket.socket") as mock_socket:
        mock_socket.return_value.getsockname.return_value = ("192.168.1.10", 12345)
        local_ip = get_ip_info()

    assert local_ip == "192.168.1.10"


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
