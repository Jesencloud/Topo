<div align="center">
  <h1>🦡 Topo</h1>
  <p><em>专为 Linux 设计的高性能系统优化与垃圾清理工具。</em></p>
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

> 保持 Linux 系统干练与高效的最优雅方式。灵感源自 macOS 上的 [Mole](https://github.com/tw93/mole)。

## 快速开始

**一键脚本安装 (推荐)**
```bash
curl -fsSL https://raw.githubusercontent.com/Jesencloud/Topo/main/install.sh | bash
```

**包管理器安装 (.deb / .rpm)**

从 [最新 Release 页面](https://github.com/Jesencloud/Topo/releases/latest) 下载，然后通过以下命令安装：
```bash
sudo apt install ./topo_xxx_amd64.deb   # Debian / Ubuntu
sudo dnf install ./topo-xxx-1.x86_64.rpm  # Fedora / RHEL
```

> **支持架构**: `amd64`/`x86_64` 与 `arm64`/`aarch64`。Arch Linux 用户请使用脚本安装。

---

## 功能特性

### 主界面 ( 在终端里输入 'Topo' 就可以开始使用 )

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

### 1. 一键深度清理 (`topo clean`)
安全清除包管理器缓存、journal 系统日志、开发构建产物和系统缓存。
```bash
$ topo clean

➤ System & Package Manager
  ✓ Cleaned DNF cache (1.2 GB)
  ✓ Vacuumed journal logs (218 MB)

➤ Developer Tools & AI Models
  ✓ Cargo cache (44.5 MB) cleaned

Total space freed: 1.25 GB | Free space now: 482.2 GB
```

### 2. 智能应用卸载 (`topo uninstall`)
交互式选择需要卸载的应用。Topo 会自动追踪并清除关联的所有配置文件、缓存和残留。
```bash
$ topo uninstall

 Select Application to Remove
 ▶ ✓  1. brave-browser                           428.7 MiB
   ✓  2. Thunderbird                             368.7 MB
```

### 3. 磁盘占用分析 (`topo analyze`)
基于 Rust 极速引擎，毫秒级遍历数十万文件，精准探索空间占用大户。
```bash
$ topo analyze

Exploring: /home/users/.config/Cursor
  ○  1. ▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬   59.9%  🗂️ WebStorage     |  109.4 MiB
  ✓  2. ▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬    6.8%  🗂️ Cache          |   12.4 MiB
```

### 4. 审计与历史 (`topo history`)
跟踪删除事件，审计清理或卸载后的变更记录。
```bash
$ topo history --limit 5
2026-06-04T18:17:03+08:00  uninstall wechat  size=820.4 MiB
```

---

## 项目亮点

- **安全第一**：内置全局白名单，保护系统二进制文件、用户凭据与 XDG 目录。
- **极速引擎**：Python 灵活性结合 Rust 原生极速，实现超高速文件扫描。
- **终端友好**：使用备用屏幕缓冲区（Alternate Screen Buffer），退出时完好保留终端会话历史。

## 开源协议

MIT License. 用心为 Linux 社区打造 ❤️
