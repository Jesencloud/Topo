import pytest
import os
from unittest.mock import patch, mock_open
from src.core.status import get_mem_info, get_uptime, get_cpu_temp

def test_get_mem_info():
    mock_data = """MemTotal:       16000000 kB
MemAvailable:    8000000 kB
"""
    with patch("builtins.open", mock_open(read_data=mock_data)):
        used, total, percent = get_mem_info()
        # used = (16000000 - 8000000) * 1024 = 8192000000 bytes = 8.2GB
        # total = 16000000 * 1024 = 16384000000 bytes = 16.4GB
        assert "8." in used
        assert "16." in total
        assert percent == 50.0

def test_get_uptime():
    mock_data = "3660.00 7000.00" # 3660 seconds = 1h 1m
    with patch("builtins.open", mock_open(read_data=mock_data)):
        uptime = get_uptime()
        assert uptime == "1h 1m"

def test_get_cpu_temp():
    mock_data = "45000" # 45.0 C
    with patch("pathlib.Path.exists", return_value=True):
        with patch("builtins.open", mock_open(read_data=mock_data)):
            temp = get_cpu_temp()
            assert temp == "45.0°C"

def test_get_cpu_temp_missing():
    with patch("pathlib.Path.exists", return_value=False):
        temp = get_cpu_temp()
        assert temp == "N/A"
