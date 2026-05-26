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

> The most elegant way to keep your Linux system lean and mean. Inspired by the minimalist philosophy of [Mole](https://github.com/tw93/mole) on macOS.

## Features

- **All-in-one toolkit**: Combines DNF/APT cleaner, App Uninstaller, Disk Analyzer, and iStat-style Monitor in a **single tool**.
- **Deep cleaning**: Reclaims gigabytes by purging caches, journal logs, browser leftovers, and massive **AI/LLM models**.
- **Smart uninstaller**: Removes applications plus their hidden config remnants in `~/.config` and `~/.local`.
- **Disk insights**: Ultra-fast disk explorer powered by a **Rust scanning engine** with parallel I/O.
- **Live monitoring**: Real-time dashboard showing CPU, GPU, memory, and top resource-consuming processes.

## Quick Start

**Automated Installation (Recommended)**

```bash
curl -fsSL https://raw.githubusercontent.com/Jesencloud/Topo/main/install.sh | bash
```

> Note: The script automatically detects your architecture (**x86_64** or **ARM64**) and provisions the optimized engine.

**Run**

```bash
topo                           # Start interactive TUI (Recommended)
topo clean                     # One-key safe cleanup
topo uninstall                 # Deep application uninstaller
topo optimize                  # Refresh system services & maintenance
topo analyze                   # Ultra-fast disk usage explorer
topo status                    # Live system health dashboard

topo update                    # Upgrade Topo to the latest version
topo link                      # Re-configure system-wide command
topo remove                    # Safely remove Topo from system
topo --help                    # Show help
```

## Security & Safety Design

Topo is built for performance but governed by safety. It uses **Home Directory Isolation** for manual cleanup and a **Global Whitelist** to ensure critical system paths remain untouched. 

It adopts a **Zero-Interruption** policy: administrative tasks are pre-authorized so your "One-Key Clean" runs unattended from start to finish.

## Features in Detail

### Deep System Cleanup

```bash
$ topo clean

[EXECUTING] Starting system cleanup...

  ✓ Package Manager Cache (DNF/APT)                          1.2 GB
  ✓ System Journal Logs                                      450 MB
  ✓ User Trash                                               2.8 GB
  ✓ Browser & App Caches                                     8.4 GB
  ✓ AI/LLM Models (Ollama/HF)                                12.5 GB

============================================================
Space freed: 25.3 GB | Free space now: 184.2 GB
============================================================
```

### Smart App Uninstaller

Select apps to remove and Topo will find all associated residues.

```bash
Select Apps to Remove
═══════════════════════════
▶ ☑ Visual Studio Code        (850M) | Active
  ☐ Discord                   (420M) | Background
  ☐ WeChat (Flatpak)          (1.2G) | Idle

Uninstalling: Visual Studio Code

  ✓ Terminated active processes
  ✓ Removed package binaries
  ✓ Cleaned residues in ~/.config/Code
  ✓ Cleaned residues in ~/.cache/Code
```

### Intelligence Analyze

Powered by a dedicated Rust engine, Topo scans hundreds of thousands of files in milliseconds.

```bash
Analyze Disk  ~  |  Total: 450.8 GB

 ▶  1. ███████████████████  58.2%  |  📁 workspace                   262.4 GB
    2. █████████░░░░░░░░░░  22.1%  |  📁 .cache                       98.6 GB
    3. ███░░░░░░░░░░░░░░░░   8.3%  |  📁 Downloads                    34.2 GB
    4. █░░░░░░░░░░░░░░░░░░   4.2%  |  📁 .local                       18.5 GB

  ↑↓ Navigate  |  Enter Open  |  Space Select  |  D Delete  |  Q Quit
```

## Technical Advantages

- **Multi-Arch Native**: Optimized binaries for both **x86_64** and **ARM64** (Apple Silicon, Raspberry Pi).
- **Intelligent Silence**: "Silent on zero-gain" policy—only shows what actually matters.
- **Zero-Latency UI**: Built-in **ScanCache** for instant directory navigation.
- **Hybrid Power**: High-level flexibility of Python combined with the raw speed of Rust.

## License

MIT License. Developed with ❤️ for the Linux community.
