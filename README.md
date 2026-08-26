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

## Requirements

Topo requires Linux, Python 3.10 or newer, `curl`, and the Python `packaging` library.
DEB/RPM installations resolve these dependencies through the system package manager. For
script installation or running directly from a source checkout, install them first if needed:

```bash
sudo apt install python3 curl python3-packaging       # Debian / Ubuntu
sudo dnf install python3 curl python3-packaging       # Fedora / RHEL
sudo pacman -S python curl python-packaging           # Arch / Manjaro
```

The script installer checks these runtime requirements before installing Topo. This repository
is not currently distributed as a Python wheel, so `pip install .` is not a supported installation
method.

## Quick Start

**Script Installation (Recommended)**
```bash
curl -fsSL https://raw.githubusercontent.com/Jesencloud/Topo/main/install.sh | bash
```

**Package Manager (.deb / .rpm)**

Download from the [latest release](https://github.com/Jesencloud/Topo/releases/latest), then install via:
```bash
sudo apt install ./topo_xxx-1_amd64.deb   # Debian / Ubuntu
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
  ✓  1. brave-browser                           428.7 MiB
  ✓  2. Thunderbird                             368.7 MB
```

### 3. Disk Usage Explorer (`topo analyze`)
Powered by a high-speed Rust engine, scan hundreds of thousands of files in milliseconds to explore storage hogs.
```bash
$ topo analyze

Exploring: /home/users/.config/Cursor
  ○  1. ▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬   59.9%  📁 WebStorage     |  109.4 MiB
  ○  2. ▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬    6.8%  📁 Cache          |   12.4 MiB
```

### 4. Audit & History (`topo history`)
Track deletion events and audit what changed after a cleanup or uninstall.
```bash
$ topo history --limit 5
2026-06-04T18:17:03+08:00  uninstall wechat  size=820.4 MiB
```

---

## Configuration

Optional. topo does not create `~/.config/topo/config.json` for you — write it by hand to change a default. Every key is optional; anything missing or invalid falls back to the value below.

```json
{
  "config_version": 2,
  "use_trash": true,
  "min_age_days": 0,
  "show_scrollbar": true,
  "theme_color": "purple"
}
```

| Key | Default | Meaning |
| --- | --- | --- |
| `use_trash` | `true` | Where a *recoverable* deletion goes: editor backups, app residue, and the directories `topo analyze` deletes on request are trashed. Set it to `false` to wipe them instead. Caches and stale temp files are always deleted outright — trashing a cache would free nothing. |
| `min_age_days` | `0` | A floor, in days, under which nothing is old enough to be cleaned. Each cleaner keeps its own window (30 days for caches, 7 for editor backups, 3 for `/tmp`); this can only push one further into the past, never closer to now. `0` leaves every threshold where the code put it. |
| `show_scrollbar` | `true` | Draw the scrollbar in the interactive selectors. |
| `theme_color` | `purple` | Title color: `purple`, `cyan`, `blue`, `magenta`, `green`, `yellow` or `red`. `--no-color` and `NO_COLOR` still win. |

Keep `"config_version": 2` in the file. A config without it is read as one written by topo ≤ 1.1.2, where `min_age_days` and `theme_color` had no effect at all: those two values are dropped rather than suddenly honoured, so upgrading never moves an existing install off the thresholds and title color it has always had.

## Highlights

- **Safety First**: Built-in global whitelist protecting system binaries, user credentials, and XDG folders.
- **High-Speed Engine**: Python flexibility paired with Rust raw speed for ultra-fast file scanning.
- **Terminal Friendly**: Uses Alternate Screen Buffer to preserve your shell session history upon exit.

## License

MIT License. Developed with ❤️ for the Linux community.
