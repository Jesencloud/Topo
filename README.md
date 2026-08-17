<div align="center">
  <h1>🦡 Topo</h1>
  <p><em>High-performance system optimization and cleanup for Linux.</em></p>
</div>

<p align="center">
  <a href="README.md"><img src="https://img.shields.io/badge/Language-English-blue.svg?style=flat-square" alt="English"></a>
  <a href="README.zh-CN.md"><img src="https://img.shields.io/badge/语言-中文-red.svg?style=flat-square" alt="中文"></a>
</p>

<p align="center">
  <a href="https://github.com/Jesencloud/Topo/stargazers"><img src="https://img.shields.io/github/stars/Jesencloud/Topo?style=flat-square" alt="Stars"></a>
  <a href="https://github.com/Jesencloud/Topo/releases"><img src="https://img.shields.io/github/v/tag/Jesencloud/Topo?label=version&style=flat-square" alt="Version"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg?style=flat-square" alt="License"></a>
  <a href="https://github.com/Jesencloud/Topo/commits"><img src="https://img.shields.io/github/commit-activity/m/Jesencloud/Topo?style=flat-square" alt="Commits"></a>
  <a href="https://github.com/Jesencloud/Topo"><img src="https://img.shields.io/badge/platform-linux-lightgrey?style=flat-square&logo=linux" alt="Linux"></a>
</p>

<p align="center">
  <img src="assets/topo.png" alt="Topo - Clean Your Linux" width="800" />
</p>

> The most elegant way to keep your Linux system lean and mean. Inspired by [Mole](https://github.com/tw93/mole) on macOS.

## Quick Start

**Script Installation (Recommended)**
```bash
curl -fsSL https://raw.githubusercontent.com/Jesencloud/Topo/main/install.sh | bash
```

**Package Manager (.deb / .rpm)**

Download from the [latest release](https://github.com/Jesencloud/Topo/releases/latest), then install via:
```bash
sudo apt install ./topo_xxx_amd64.deb   # Debian / Ubuntu
sudo dnf install ./topo-xxx-1.x86_64.rpm  # Fedora / RHEL
```

> **Supported Architectures**: `amd64`/`x86_64` & `arm64`/`aarch64`. Arch Linux users please use the script installer.

---

## Features

### Main UI ( Type 'Topo' in your terminal to get started )

```bash
 ⠶⣶⠶  ⢰⠶⡆ ⢰⠶⡆ ⢰⠶⡆
  ⠿   ⠸⠤⠇ ⢸⠉⠁ ⠸⠤⠇   ● v1.x.x is digging deeper 🦡

 Main Menu

 > 1. Clean        Free up disk space
   2. Uninstall    Remove apps completely
   3. Optimize     Check and maintain system
   4. Analyze      Explore disk usage
   5. Status       Monitor system health

 ↑/↓ | M: Mute | Enter: Select | ESC: Quit
```

### 1. One-Key Deep Cleanup (`topo clean`)
Safely remove package manager caches, journal logs, developer build artifacts, and system caches.
```bash
$ topo clean

➤ System & Package Manager
  ✓ Cleaned DNF cache (1.2 GB)
  ✓ Vacuumed journal logs (218 MB)

➤ Developer Tools & AI Models
  ✓ Cargo cache (44.5 MB) cleaned

Total space freed: 1.25 GB | Free space now: 482.2 GB
```

### 2. Smart App Uninstaller (`topo uninstall`)
Interactively select applications to remove. Topo automatically traces and purges all associated config files, caches, and residues.
```bash
$ topo uninstall

 Select Application to Remove
 ▶ ✓  1. brave-browser                           428.7 MiB
   ✓  2. Thunderbird                             368.7 MB
```

### 3. Disk Usage Explorer (`topo analyze`)
Powered by a high-speed Rust engine, scan hundreds of thousands of files in milliseconds to explore storage hogs.
```bash
$ topo analyze

Exploring: /home/users/.config/Cursor
  ○  1. ▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬   59.9%  🗂️ WebStorage     |  109.4 MiB
  ✓  2. ▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬    6.8%  🗂️ Cache          |   12.4 MiB
```

### 4. Audit & History (`topo history`)
Track deletion events and audit what changed after a cleanup or uninstall.
```bash
$ topo history --limit 5
2026-06-04T18:17:03+08:00  uninstall wechat  size=820.4 MiB
```

---

## Highlights

- **Safety First**: Built-in global whitelist protecting system binaries, user credentials, and XDG folders.
- **High-Speed Engine**: Python flexibility paired with Rust raw speed for ultra-fast file scanning.
- **Terminal Friendly**: Uses Alternate Screen Buffer to preserve your shell session history upon exit.

## License

MIT License. Developed with ❤️ for the Linux community.
