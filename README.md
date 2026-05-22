# 🦔 lmole

**lmole** (Linux Mole) is a high-performance system optimization and cleanup tool designed specifically for the Linux platform. Inspired by the minimalist philosophy of the famous `Mole` tool on macOS, `lmole` aims to provide a fast, safe, and intuitive experience for users to maintain their system's health and reclaim disk space.

Built with a hybrid architecture of **Python logic** and a custom **Rust scanning engine**, `lmole` is up to 10x faster than traditional shell-based tools for large-scale filesystem analysis.

---

## ✨ Key Features

1.  **🧽 One-Key Clean**  
    A streamlined, one-action cleanup process. Covers package manager caches (DNF/APT/Pacman), system journal logs, user trash, temporary files, and deep caches for popular apps like Discord, Spotify, VS Code, Zoom, and Teams.

2.  **📦 Deep Uninstall**  
    Completely remove applications beyond the binary. Uses intelligent naming variants to locate and purge hidden configuration residues in `~/.config`, `~/.cache`, and `~/.var/app` (Flatpak).

3.  **⚡ System Optimize**  
    Perform essential system maintenance tasks including SSD Trim, DNS cache flushing, font index rebuilding, and instant PageCache memory relief.

4.  **📊 Intelligence Analyze**  
    An ultra-fast disk usage analyzer powered by a dedicated Rust engine. Features multi-threaded scanning and a built-in `ScanCache` for instant directory navigation without rescanning.

5.  **🛡️ Health Status**  
    Real-time system monitoring dashboard. Displays CPU load, core temperature, memory pressure, disk utilization, and network traffic at a glance.

---

## 🚀 Technical Advantages

*   **Rust Engine**: Core scanning logic is implemented in `lmo-core` (Rust), utilizing parallel I/O to analyze tens of thousands of files in milliseconds.
*   **Modern TUI**: A clean terminal user interface with full keyboard navigation support.
*   **Safe by Design**: Built-in global whitelist protection ensures critical system paths are never touched. Supports `--dry-run` for risk-free previews.
*   **Cloud Native Aware**: Native support for cleaning Docker resources (images, volumes) and various development tool caches (NPM, Go, Pip, JetBrains IDEs).

---

## 🛠️ Installation & Usage

### Prerequisites
*   Python 3.10+
*   Linux OS (Fedora, Ubuntu, Debian, Arch, etc.)

### Quick Installation (Recommended)
`lmole` is designed as a standalone tool. Simply link the launcher to your local bin path:

```bash
# Clone the repository
git clone https://github.com/Jesencloud/lmole.git
cd lmole

# Create a symbolic link to your local bin directory
# (Ensure ~/.local/bin is in your PATH)
ln -s $(pwd)/lmole ~/.local/bin/lmole
```

### Build Rust Engine (Optional)
The project includes pre-compiled binaries. To build the engine manually:
```bash
cd lmo-core && cargo build --release
cp target/release/lmo-core ../src/core/bin/lmo-core
```

---

## 📖 Usage Guide

Type `lmole` to enter the interactive TUI:
```bash
lmole
```

Alternatively, use CLI commands for quick actions:
*   `lmole clean` - Execute one-key cleanup
*   `lmole --dry-run clean` - Preview files to be deleted
*   `lmole status` - View system health overview
*   `lmole analyze` - Enter deep disk analysis mode
*   `lmole uninstall` - Enter application uninstallation mode

 _                      _      
| |                    | |     
| |  _ __ ___    ___   | |  ___
| | | '_ ` _ \  / _ \  | | / _ \
| |____ | | | | | | (_) | | |  __/
|______||_| |_| |_|\___/|_| \___|
      v0.5.0 | lmole - Linux System Optimizer

 Main Menu
--------------------------------------------------
 > 1. Clean        One-key safe disk cleanup
   2. Uninstall    Remove apps completely
   3. Optimize     Check and maintain system
   4. Analyze      Explore disk usage
   5. Status       Monitor system health
--------------------------------------------------
 ↑/↓: Navigate | Enter: Select | Q: Quit


---

## 🤝 Contribution & Credits

*   **Inspired by**: [Mole](https://github.com/tw93/mole) (macOS)
*   **Core Logic**: Python 3
*   **Scanning Engine**: Rust (lmo-core)

`lmole` strives to be the most elegant and powerful optimization tool for the Linux community. Issues and Pull Requests are always welcome!
