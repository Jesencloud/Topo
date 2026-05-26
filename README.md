<div align="center">
  <h1>🦡 topo</h1>
  <p><em>High-performance system optimization and cleanup for Linux.</em></p>
</div>

<p align="center">
  <a href="https://github.com/Jesencloud/Topo/stargazers"><img src="https://img.shields.io/github/stars/Jesencloud/Topo?style=flat-square" alt="Stars"></a>
  <a href="https://github.com/Jesencloud/Topo/releases"><img src="https://img.shields.io/github/v/tag/Jesencloud/Topo?label=version&style=flat-square" alt="Version"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg?style=flat-square" alt="License"></a>
  <a href="https://github.com/Jesencloud/Topo/commits"><img src="https://img.shields.io/github/commit-activity/m/Jesencloud/Topo?style=flat-square" alt="Commits"></a>
</p>

**topo** (Topo) is inspired by the minimalist philosophy of the famous `Mole` tool on macOS, providing a fast, safe, and intuitive experience for maintaining system health and reclaiming disk space.

Built with a hybrid architecture of **Python logic** and a custom **Rust scanning engine**, `topo` is up to 10x faster than traditional shell-based tools for large-scale filesystem analysis.

---

## ✨ Key Features

1.  **🧽 One-Key Clean**  
    A streamlined, one-action cleanup process. Covers package manager caches (DNF/APT/Pacman), system journal logs, user trash, and developer caches (NPM, Pip, Go). Now features **AI/LLM Model Cleanup** (Ollama, Hugging Face) and Docker system pruning.

2.  **📦 Deep Uninstall**  
    Completely remove applications including their hidden residues. Uses **intelligent keyword extraction** from `.desktop` files and fuzzy matching to locate configurations in `~/.config`, `~/.cache`, and `~/.local`. Automatically **terminates running processes** before removal to prevent "ghost" apps.

3.  **⚡ System Optimize**  
    Advanced maintenance beyond simple cleaning. Includes **SQLite Database Vacuuming** for browsers (Firefox, Chrome, Brave) to improve startup speed, **Zombie Autostart Cleanup**, SSD Trim, and **Intelligent Memory/Swap Management**.

4.  **📊 Intelligence Analyze**  
    An ultra-fast disk usage analyzer powered by a dedicated Rust engine. Features multi-threaded scanning and a built-in **ScanCache** for instant directory navigation without rescanning.

5.  **🛡️ Health Status**  
    Comprehensive system monitoring dashboard. Displays CPU load, core temperature, memory pressure, disk utilization, network traffic, **GPU Status (NVIDIA/AMD)**, and a list of **Top Resource-Consuming Processes**.

---

## 🚀 Technical Advantages

*   **Multi-Arch Rust Engine**: Core scanning logic implemented in Rust, utilizing parallel I/O for extreme performance. Supports both **x86_64** and **ARM64** (Apple Silicon, Raspberry Pi, Cloud ARM) with automatic binary provisioning.
*   **Zero-Interruption Experience**: Smart `sudo` pre-authorization allows the cleaning process to run completely unattended once started—no more mid-task password prompts.
*   **Intelligent Silence**: Topo follows a "silent on zero-gain" philosophy. If a task reclaims 0 B, it stays hidden, keeping your terminal output clean and high-signal.
*   **Production-Ready Installer**: The `install.sh` script handles everything: architecture detection, dependency checks, and a "lean" installation that excludes development artifacts.
*   **Safe by Design**: Features global whitelist protection and strict **Home Directory Isolation** to prevent accidental deletion of critical system files.

---

## 🛠️ Installation & Usage

### Quick Installation
Deploy the latest stable version of `topo` with a single command:

```bash
curl -fsSL https://raw.githubusercontent.com/Jesencloud/Topo/main/install.sh | bash
```

This script will automatically detect your hardware, provision the correct optimized engine to `~/.topo`, and set up the `topo` command in your terminal.

---

## 📖 Usage Guide

Type `topo` to enter the interactive TUI:
```bash
topo
```

Alternatively, use CLI commands:
*   `topo clean` - Execute one-key cleanup
*   `topo uninstall` - Enter application uninstallation mode
*   `topo optimize` - Run system maintenance tasks
*   `topo analyze` - Enter deep disk analysis mode
*   `topo status` - View real-time system health
*   `topo update` - Update topo to the latest version
*   `topo remove` - Safely remove topo system integration
*   `topo authorize` - Setup passwordless sudo for topo (optional)

---

## 🤝 Contribution & Credits

*   **Inspired by**: [Mole](https://github.com/tw93/mole) (macOS)
*   **Core Logic**: Python 3
*   **Scanning Engine**: Rust (topo-core)

`topo` strives to be the most elegant and powerful optimization tool for the Linux community. Issues and Pull Requests are always welcome!
