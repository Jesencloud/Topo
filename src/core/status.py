import os
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from .file_ops import bytes_to_human

def get_mem_info():
    """Read RAM info from /proc/meminfo."""
    try:
        with open('/proc/meminfo', 'r') as f:
            lines = f.readlines()
            total = 0
            available = 0
            for line in lines:
                if 'MemTotal' in line:
                    total = int(line.split()[1]) * 1024
                if 'MemAvailable' in line:
                    available = int(line.split()[1]) * 1024
            used = total - available
            return bytes_to_human(used), bytes_to_human(total)
    except:
        return "Unknown", "Unknown"

def get_uptime():
    try:
        with open('/proc/uptime', 'r') as f:
            uptime_seconds = float(f.readline().split()[0])
            hours = int(uptime_seconds // 3600)
            minutes = int((uptime_seconds % 3600) // 60)
            return f"{hours}h {minutes}m"
    except:
        return "Unknown"

def get_battery_info():
    """Get battery capacity, health, and cycle count."""
    try:
        bat_path = Path("/sys/class/power_supply/BAT0")
        if not bat_path.exists():
            return None
            
        with open(bat_path / "capacity", "r") as f:
            capacity = f.read().strip()
        
        # Health calculation
        try:
            with open(bat_path / "energy_full_design", "r") as f:
                design = int(f.read().strip())
            with open(bat_path / "energy_full", "r") as f:
                full = int(f.read().strip())
            health = (full / design) * 100
            health_str = f" (Health: {health:.1f}%)"
        except:
            health_str = ""
            
        # Cycle count
        cycles_str = ""
        try:
            with open(bat_path / "cycle_count", "r") as f:
                cycles = f.read().strip()
                if cycles and cycles != "0":
                    cycles_str = f" | Cycles: {cycles}"
        except:
            pass
            
        return f"{capacity}%{health_str}{cycles_str}"
    except:
        return "N/A"

def get_network_traffic():
    """Get total network traffic since boot."""
    try:
        with open("/proc/net/dev", "r") as f:
            lines = f.readlines()[2:] # Skip headers
            rx = 0
            tx = 0
            for line in lines:
                parts = line.split()
                if parts[0] == "lo:": continue
                rx += int(parts[1])
                tx += int(parts[9])
            return bytes_to_human(rx), bytes_to_human(tx)
    except:
        return "N/A", "N/A"

def get_ssd_info():
    """Try to find SSD health info from /sys (limited availability without root/smartctl)."""
    try:
        # NVMe specific health (some kernels expose this)
        nvme_path = Path("/sys/class/nvme/nvme0/device/smart_log")
        if nvme_path.exists():
            # This is binary data, hard to parse without root. 
            # Fallback to model name
            pass
        
        # Check for model and temperature
        drive_path = Path("/sys/class/block/nvme0n1/device")
        if not drive_path.exists():
            drive_path = Path("/sys/class/block/sda/device")
            
        if drive_path.exists():
            with open(drive_path / "model", "r") as f:
                model = f.read().strip()
            return model
    except:
        pass
    return "Generic Drive"

def get_cpu_temp():
    """Try to read CPU temperature from /sys/class/thermal."""
    try:
        temp_path = Path("/sys/class/thermal/thermal_zone0/temp")
        if temp_path.exists():
            with open(temp_path, "r") as f:
                temp_mc = int(f.read().strip())
                return f"{temp_mc / 1000:.1f}°C"
    except:
        pass
    return "N/A"

def get_gpu_info():
    """Detect and get GPU status (NVIDIA/AMD/Intel)."""
    # 1. Check NVIDIA (Most common for AI)
    if shutil.which("nvidia-smi"):
        try:
            res = subprocess.run(["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu", "--format=csv,noheader,nounits"], 
                                capture_output=True, text=True)
            if res.returncode == 0:
                util, used, total, temp = res.stdout.strip().split(', ')
                return f"NVIDIA: {util}% util | Mem: {int(used)/1024:.1f}GB / {int(total)/1024:.1f}GB | {temp}°C"
        except: pass

    # 2. Check AMD/Intel (via sysfs)
    # Search for card0, card1, etc.
    try:
        drm_path = Path("/sys/class/drm")
        if drm_path.exists():
            for card_dir in drm_path.glob("card*"):
                # Avoid symlinks that don't lead to devices
                if not (card_dir / "device").exists(): continue
                
                # Check for AMD utilization
                util_path = card_dir / "device/gpu_busy_percent"
                if util_path.exists():
                    with open(util_path, "r") as f: util = f.read().strip()
                    # Find temperature in hwmon subdirectories
                    temp_str = ""
                    hwmon_root = card_dir / "device/hwmon"
                    if hwmon_root.exists():
                        for hw_dir in hwmon_root.glob("hwmon*"):
                            t_file = hw_dir / "temp1_input"
                            if t_file.exists():
                                with open(t_file, "r") as f: 
                                    temp_str = f" | {int(f.read().strip())/1000:.0f}°C"
                                break
                    return f"AMD: {util}% utilization{temp_str}"
                
                # Check for Intel utilization (i915 driver)
                # Note: Intel utilization is harder via sysfs, but presence check works
                if (card_dir / "device/vendor").exists():
                    with open(card_dir / "device/vendor", "r") as f:
                        vendor = f.read().strip()
                        if "0x8086" in vendor: # Intel Vendor ID
                            return "Intel HD/UHD Graphics (Active)"
    except: pass
    
    return None

def get_top_processes():
    """Get top 3 applications by aggregated memory usage."""
    try:
        # Use ps to get command and resident memory (rss)
        cmd = ["ps", "-eo", "comm,rss", "--no-headers"]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode == 0:
            lines = res.stdout.strip().split('\n')
            agg_mem = {}
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
    except: pass
    return []

def show_status():
    """Main status display logic."""
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"\n\033[1;36m🛡️  System Health Status ({now})\033[0m")
    print("-" * 65)
    
    uptime = get_uptime()
    cpu_load = os.getloadavg()
    cpu_temp = get_cpu_temp()
    used_mem, total_mem = get_mem_info()
    battery = get_battery_info()
    rx, tx = get_network_traffic()
    gpu = get_gpu_info()
    top_procs = get_top_processes()
    
    home_stats = shutil.disk_usage(os.path.expanduser("~"))
    
    print(f"⏱️  Uptime:       {uptime}")
    print(f"📊 CPU Load:     {cpu_load[0]:.2f}, {cpu_load[1]:.2f}, {cpu_load[2]:.2f} (1m, 5m, 15m)")
    print(f"🌡️  CPU Temp:     {cpu_temp}")
    print(f"🧠 Memory:       {used_mem} / {total_mem}")
    
    if gpu:
        print(f"🎮 GPU Status:   {gpu}")
    
    disk_size_str = f"{bytes_to_human(home_stats.used)} / {bytes_to_human(home_stats.total)} used"
    print(f"💾 Disk:         {disk_size_str}")
    
    if battery:
        print(f"🔋 Battery:      {battery}")
    
    print(f"🌐 Network:      ↓ {rx} / ↑ {tx} (Total)")
    
    if top_procs:
        print(f"🚀 Top Processes: {', '.join(top_procs)}")
    
    print("-" * 65)
