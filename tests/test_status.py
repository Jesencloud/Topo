from unittest.mock import mock_open, patch

from src.core.status import get_cpu_temp, get_ip_info, get_mem_info, get_uptime


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


def test_get_cpu_temp():
    mock_data = "45000"  # 45.0 C
    with (
        patch("pathlib.Path.exists", return_value=True),
        patch("builtins.open", mock_open(read_data=mock_data)),
    ):
        temp = get_cpu_temp()
        assert temp == "45.0°C"


def test_get_cpu_temp_missing():
    with patch("pathlib.Path.exists", return_value=False):
        temp = get_cpu_temp()
        assert temp == "N/A"


def test_get_ip_info_does_not_fetch_public_ip_by_default():
    with (
        patch("src.core.status.load_config", return_value={"status_public_ip": False}),
        patch("socket.socket") as mock_socket,
        patch("urllib.request.urlopen") as mock_urlopen,
    ):
        mock_socket.return_value.getsockname.return_value = ("192.168.1.10", 12345)
        local_ip, public_ip = get_ip_info()

    assert local_ip == "192.168.1.10"
    assert public_ip == ""
    mock_urlopen.assert_not_called()


def test_get_ip_info_fetches_public_ip_when_enabled():
    response = mock_open(read_data=b'{"status":"success","query":"203.0.113.1","countryCode":"CN"}')
    response.return_value.__enter__.return_value.read.return_value = (
        b'{"status":"success","query":"203.0.113.1","countryCode":"CN"}'
    )

    with (
        patch("src.core.status.load_config", return_value={"status_public_ip": True}),
        patch("socket.socket") as mock_socket,
        patch("urllib.request.urlopen", response),
    ):
        mock_socket.return_value.getsockname.return_value = ("192.168.1.10", 12345)
        local_ip, public_ip = get_ip_info()

    assert local_ip == "192.168.1.10"
    assert public_ip == "[CN] 203.0.113.1"
