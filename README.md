<div align="center">
  <h1>🦡 Topo</h1>
  <p><em>High-performance system optimization and cleanup for Linux.</em></p>
</div>

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

> The most elegant way to keep your Linux system lean and mean. Inspired by the minimalist philosophy of [Mole](https://github.com/tw93/mole) on macOS.

## Features

- **All-in-one toolkit**: Combines package managers (DNF/APT/Pacman), App Uninstaller, Disk Analyzer, and Monitor in a **single tool**.
- **Intelligent cleanup**: Features a proactive detection engine that auto-discovers unknown apps, AppImage remnants, and orphaned data.
- **AI Developer ready**: Reclaims gigabytes with age-aware purging for Hugging Face, Ollama, PyTorch, and CUDA caches.
- **Cross-distro support**: Deep cleans Ubuntu Snaps, Multipass, Flatpaks, and Fedora Podman/Docker environments.
- **Disk insights**: Ultra-fast disk explorer powered by a **Rust scanning engine** with parallel I/O.
- **Live monitoring**: Real-time dashboard showing CPU, GPU, memory, and top resource-consuming processes.

## Quick Start

**Script Installation**

```bash
curl -fsSL https://raw.githubusercontent.com/Jesencloud/Topo/main/install.sh | bash
```

The script installer keeps Topo under `~/.topo`, detects **x86_64** or **ARM64**, provisions the optimized engine, and uses Topo's built-in `topo update` / `topo remove` lifecycle.

**GitHub Release Packages**

Download the matching package from the [latest release](https://github.com/Jesencloud/Topo/releases/latest), then install it with your system package manager:

```bash
sudo apt install ./topo_0.9.3_amd64.deb
sudo dnf install ./topo-0.9.3-1.x86_64.rpm
```

Use `topo_0.9.3_arm64.deb` on Debian/Ubuntu ARM64 systems and `topo-0.9.3-1.aarch64.rpm` on Fedora/RHEL ARM64 systems. Package installs place Topo under `/usr/lib/topo` and expose `/usr/bin/topo`; updates and removal are handled by `apt` or `dnf`.

```bash
  ████████  ██████  ██████   ██████
     ██    ██    ██ ██   ██ ██    ██
     ██    ██    ██ ██████  ██    ██
     ██    ██    ██ ██      ██    ██
     ██     ██████  ██       ██████

  ● Topo is digging deeper 🦡 🦡 🦡

 Main Menu

 > 1. Clean        Free up disk space
   2. Uninstall    Remove apps completely
   3. Optimize     Check and maintain system
   4. Analyze      Explore disk usage
   5. Status       Monitor system health

 ↑/↓ | M: Mute | Enter: Select | ESC: Quit
```
## Help

```bash
options:
  -h, --help   show this help message and exit
  --version    show program's version number and exit

commands:
  COMMAND
    clean      One-key safe disk cleanup
    analyze    Interactive disk usage explorer
    uninstall  Completely remove applications and residues
    optimize   Run system maintenance (fstrim, databases, etc.)
    purge      Interactive project artifact purging
    status     Monitor system health and resource usage
    history    Show recent deletion history
    all        Run all cleanup and purge tasks sequentially
    authorize  Setup passwordless sudo for faster cleanup
    update     Update topo to the latest version
    remove     Uninstall topo from the system
    link       Create a symbolic link for the 'topo' command
    whitelist  Manage manual path protection whitelist

Quick Start:
  topo                     Open the interactive TUI
  topo clean --dry-run     Preview cleanup without deleting
  topo analyze             Explore disk usage
  topo status              Show system health
  topo history --limit 5   Show the last 5 cleanup sessions

Whitelist:
  topo whitelist list         Show manual protection rules.
  topo whitelist add PATH     Protect PATH from cleanup.
  topo whitelist remove PATH  Remove a manual rule.

Notes:
  An empty whitelist is normal before you add a path.
  Built-in protections cover system paths, credentials, and XDG folders.
  Run topo whitelist --help for whitelist details.
  Run topo COMMAND --help for command-specific options.

```

## Security & Safety Design

Topo is built for performance but governed by safety. It uses **Home Directory Isolation** for manual cleanup and a **Global Whitelist** to ensure critical system paths remain untouched. 

It adopts a **Zero-Interruption** policy: administrative tasks are pre-authorized so your "One-Key Clean" runs unattended from start to finish.

### Deep System Cleanup

```bash
Clean Your Linux

● Use 'topo clean --dry-run' to preview, 'topo whitelist --help' for whitelist details.
➔ System caches need sudo. Enter continue, Space skip: 
➔ System cleanup requires admin access
➔ Password: 
 ✓ Authorization successful.


➤ System & Package Manager
  ✓ Cleaned DNF cache (1.2 GB)
  ✓ Vacuumed journal logs (218 MB)

➤ Developer Tools & AI Models
  ✓ Cargo cache (44.5 MB) cleaned

============================================================
Cleanup complete

Breakdown:
  • Package Manager Cache        1.2 GB (1 items)
  • Developer Artifacts         44.5 MB (1 items)

Total space freed: 1.25 GB | Items: 2
Free space now: 482.2 GB
============================================================
```

### Smart App Uninstaller

Select apps to remove and Topo will find all associated residues.

```bash
 Select Application to Remove 2/54 selected

  ○  1. cursor                                  837.1 MiB | 2d ago
  ○  2. wechat                                  715.8 MiB | Just now
▶ ✓  3. brave-browser                           428.7 MiB | 4d ago
  ○  4. google-chrome-stable                    404.0 MiB | 4d ago
  ✓  5. Thunderbird                              368.7 MB | 13d ago
  ○  6. java-25-openjdk-headless                236.8 MiB | 20d ago
  ○  7. clash-verge                             235.8 MiB | 6d ago
  ○  8. glibc-all-langpacks                     227.2 MiB | 20d ago
  ○  9. rust-std-static                         162.7 MiB | 6d ago
  ○ 10. llvm-libs                               140.5 MiB | 4d ago
  ○ 11. cldr-emoji-annotation                   122.4 MiB | 1mo ago
  ○ 12. gcc                                     120.8 MiB | 14d ago
  ○ 13. libreoffice-calc                         27.4 MiB | 11d ago
  ○ 14. orca                                     22.2 MiB | 20d ago
  ○ 15. papers                                   14.6 MiB | 20d ago

 Page 1/4 | ↑↓←→ | A: All | N: Name | S: Size ↓ | T: Time | Space: Select

 ☉ Selected Apps to Remove: Press Enter to Uninstall, ESC to Exit
   • brave-browser                         • Thunderbird           
```

### Cleanup History

Topo records cleanup and uninstall deletion events so you can review what changed after a run.

```bash
$ topo history --limit 5
topo 0.8.0 (Python Edition)
System: fedora
Deletion History

2026-06-04T18:17:03+08:00 -> 2026-06-04T18:17:05+08:00  uninstall wechat
  removed=2  trashed=0  skipped=0  failed=0  size=820.4 MiB
    removed              wechat
    deleted              /home/users/.xwechat

```

### Intelligence Analyze

Powered by a dedicated Rust engine, Topo scans hundreds of thousands of files in milliseconds.

```bash
Exploring: /home/users/.config/Cursor

Select a location to explore (Type numbers or Space to select):

  ○  1. ▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬   59.9%  🗂️ WebStorage                     |  109.4 MiB
  ○  2. ▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬   11.5%  🗂️ CachedExtensionVSIXs           |   21.0 MiB
  ✓  3.  ▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬    6.8%  🗂️ Cache                          |   12.4 MiB
  ○  4. ▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬    5.6%  🗂️ User                           |   10.3 MiB
  ○  5. ▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬    5.3%  🗂️ CachedData                     |    9.7 MiB
  ○  6. ▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬    4.3%  🗂️ GPUCache                       |    7.9 MiB
▶ ✓  7.  ▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬    1.6%  🗂️ logs                           |    2.8 MiB
  ○  8. ▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬    1.1%  🗂️ clp                            |    2.0 MiB
  ○  9. ▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬    0.9%  🗂️ Partitions                     |    1.7 MiB
  ○ 10. ▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬    0.9%  🗂️ process-monitor                |    1.6 MiB
  ○ 11. ▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬    0.6%  🗂️ CachedProfilesData             |    1.1 MiB
  ○ 12. ▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬    0.4%  🗂️ Local Storage                  |  789.5 KiB
  ○ 13. ▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬    0.3%  🗂️ DawnWebGPUCache                |  544.4 KiB
  ○ 14. ▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬    0.3%  🗂️ DawnGraphiteCache              |  544.4 KiB
  ○ 15. ▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬    0.2%  🗂️ Dictionaries                   |  441.4 KiB


  Page 1/3 | ↑↓←→ | A:All | F:Open Folder | R:Reload | S:Sort ↓ | Space:Select

 ☉ Selected Items to Remove: Enter:Delete
   • 🗂️ Cache                                 • 🗂️ logs
```

## Technical Advantages

- **Multi-Arch Native**: Optimized binaries for both **x86_64** and **ARM64** (Apple Silicon, Raspberry Pi).
- **Self-Learning Registry**: Automatically learns your installed apps to provide process-safe, high-precision cleaning without relying solely on hardcoded lists.
- **Terminal History Protection**: Uses the Alternate Screen Buffer to ensure your terminal session history is perfectly preserved upon exit.
- **Intelligent Silence**: "Silent on zero-gain" policy—only shows what actually matters.
- **Zero-Latency UI**: Built-in **ScanCache** for instant directory navigation.
- **Hybrid Power**: High-level flexibility of Python combined with the raw speed of Rust.

## License

MIT License. Developed with ❤️ for the Linux community.
