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

## 运行要求

Topo 需要 Linux、Python 3.10 或更高版本、`curl`，以及 Python `packaging` 库。
通过 DEB/RPM 安装时，系统包管理器会自动解析这些依赖。使用脚本安装或直接从源码目录运行前，
如系统尚未安装相关依赖，请先执行对应命令：

```bash
sudo apt install python3 curl python3-packaging       # Debian / Ubuntu
sudo dnf install python3 curl python3-packaging       # Fedora / RHEL
sudo pacman -S python curl python-packaging           # Arch / Manjaro
```

脚本安装器会在安装 Topo 前检查这些运行要求。本项目目前不以 Python wheel 形式发布，
因此不支持使用 `pip install .` 安装。

## 快速开始

**一键脚本安装 (推荐)**
```bash
curl -fsSL https://raw.githubusercontent.com/Jesencloud/Topo/main/install.sh | bash
```

**包管理器安装 (.deb / .rpm)**

从 [最新 Release 页面](https://github.com/Jesencloud/Topo/releases/latest) 下载，然后通过以下命令安装：
```bash
sudo apt install ./topo_xxx-1_amd64.deb   # Debian / Ubuntu
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
  ✓  1. brave-browser                           428.7 MiB
  ✓  2. Thunderbird                             368.7 MB
```

### 3. 磁盘占用分析 (`topo analyze`)
基于 Rust 极速引擎，毫秒级遍历数十万文件，精准探索空间占用大户。
```bash
$ topo analyze

Exploring: /home/users/.config/Cursor
  ○  1. ▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬   59.9%  📁 WebStorage     |  109.4 MiB
  ○  2. ▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬▬    6.8%  📁 Cache          |   12.4 MiB
```

### 4. 审计与历史 (`topo history`)
跟踪删除事件，审计清理或卸载后的变更记录。
```bash
$ topo history --limit 5
2026-06-04T18:17:03+08:00  uninstall wechat  size=820.4 MiB
```

---

## 配置

可选。topo 不会替你创建 `~/.config/topo/config.json`，需要改默认值时手写这个文件即可。所有键都是可选的，缺失或非法的值一律回落到下面的默认值。

```json
{
  "config_version": 2,
  "use_trash": true,
  "min_age_days": 0,
  "show_scrollbar": true,
  "theme_color": "purple"
}
```

| 键 | 默认值 | 含义 |
| --- | --- | --- |
| `use_trash` | `true` | 一次**可恢复**的删除去哪儿：编辑器备份、应用残留、`topo analyze` 里手动删除的目录会进回收站。改成 `false` 则直接抹掉。缓存与过期临时文件一律直接删除——把缓存搬进回收站不会释放任何空间。 |
| `min_age_days` | `0` | 清理的年龄下限（天）。各清理器自带窗口（缓存 30 天、编辑器备份 7 天、`/tmp` 3 天），这个值只能把窗口往过去推，不能往现在拉。`0` 表示不加下限，完全按代码里的阈值执行。 |
| `show_scrollbar` | `true` | 交互选择器里是否绘制滚动条。 |
| `theme_color` | `purple` | 标题颜色：`purple`、`cyan`、`blue`、`magenta`、`green`、`yellow`、`red`。`--no-color` 与 `NO_COLOR` 仍然优先。 |

文件里请保留 `"config_version": 2`。没有这个键的配置会被当成 topo ≤ 1.1.2 写下的旧文件——那时 `min_age_days` 与 `theme_color` 根本无人读取，所以其中这两个值会被丢弃而不是突然生效，升级才不会把现有安装从一直以来的阈值和标题色上挪走。

## 项目亮点

- **安全第一**：内置全局白名单，保护系统二进制文件、用户凭据与 XDG 目录。
- **极速引擎**：Python 灵活性结合 Rust 原生极速，实现超高速文件扫描。
- **终端友好**：使用备用屏幕缓冲区（Alternate Screen Buffer），退出时完好保留终端会话历史。

## 开源协议

MIT License. 用心为 Linux 社区打造 ❤️
