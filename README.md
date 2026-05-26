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

*   **Rust Engine**: Core scanning logic implemented in `topo-core` (Rust), utilizing parallel I/O to analyze tens of thousands of files in milliseconds.
*   **Modern TUI**: A clean terminal user interface with full keyboard navigation and numeric hotkeys for rapid selection.
*   **Safe by Design**: Built-in global whitelist protection ensures critical system paths are never touched. Enforces strict **Home Directory Isolation** for manual cleanup tasks.
*   **Cloud & AI Ready**: Native support for cleaning Docker resources and massive AI model repositories (Ollama, Hugging Face).

---

## 🛠️ Installation & Usage

### Prerequisites
*   Python 3.10+
*   Linux OS (Fedora, Ubuntu, Debian, Arch, etc.)

### Quick Installation (Recommended)
You can install or update `topo` using our automated installation script:

```bash
curl -fsSL https://raw.githubusercontent.com/Jesencloud/Topo/main/install.sh | bash
```

This script will safely download the tool to `~/.topo` and automatically configure the `topo` command in your terminal.

### Build Rust Engine (Optional)
The project includes pre-compiled binaries for **x86_64**. To support **ARM64** or build manually:

#### Option A: Use GitHub Actions (Easiest)
Our repository is equipped with CI/CD. Simply push any change to the `topo-core/` directory, and GitHub will automatically build both x86 and ARM binaries. You can find them in the "Actions" tab under "Artifacts".

#### Option B: Manual Build
```bash
cd topo-core && cargo build --release
# For x86_64
cp target/release/topo-core ../src/core/bin/topo-core-x86_64
# For ARM64 (on an ARM machine)
cp target/release/topo-core ../src/core/bin/topo-core-aarch64
```

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
*   `topo remove` - Safely remove topo system integration
*   `topo authorize` - Setup passwordless sudo for topo (optional)

---

## 🤝 Contribution & Credits

*   **Inspired by**: [Mole](https://github.com/tw93/mole) (macOS)
*   **Core Logic**: Python 3
*   **Scanning Engine**: Rust (topo-core)

`topo` strives to be the most elegant and powerful optimization tool for the Linux community. Issues and Pull Requests are always welcome!
