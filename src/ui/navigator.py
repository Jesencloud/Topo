import os
import select
import sys
import termios
import time
import tty
from contextlib import contextmanager
from pathlib import Path

from ..core.constants import BOLD, GRAY, GREEN, RED, RESET, WHITE, YELLOW
from ..core.file_ops import bytes_to_human


def get_terminal_width():
    try:
        return os.get_terminal_size().columns
    except OSError:
        return 80


def pad_and_truncate(text, width):
    """Pads or truncates text to fit a specific width, accounting for CJK characters."""
    import unicodedata

    actual_w = 0
    for char in text:
        if unicodedata.east_asian_width(char) in ("W", "F"):
            actual_w += 2
        else:
            actual_w += 1

    if actual_w > width:
        curr_w = 0
        res = ""
        for char in text:
            char_w = 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
            if curr_w + char_w + 3 > width:
                res += "..."
                break
            res += char
            curr_w += char_w
        return res + " " * (width - curr_w - 3 if width > curr_w + 3 else 0)
    else:
        return text + " " * (width - actual_w)


def draw_bar(percent, width=20, force_color=None):
    """Draws a sleek progress bar using the '▬' character."""
    if width <= 0:
        return ""
    filled = int((percent / 100) * width)
    empty = width - filled

    if force_color:
        color = force_color
    elif percent > 80:
        color = RED
    elif percent > 50:
        color = YELLOW
    else:
        color = GREEN

    bar = f"{color}{'▬' * filled}{RESET}{GRAY}{'▬' * empty}{RESET}"
    return bar


class Navigator:
    UP = "\x1b[A"
    DOWN = "\x1b[B"
    RIGHT = "\x1b[C"
    LEFT = "\x1b[D"
    PGUP = "\x1b[5~"
    PGDN = "\x1b[6~"
    ENTER = ("\r", "\n")
    ESC = "\x1b"
    SPACE = " "
    DEL = "\x7f"

    @staticmethod
    @contextmanager
    def raw_mode():
        """Context manager to put the terminal into cbreak mode and restore it later."""
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            # Disable mouse reporting
            sys.stdout.write("\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l")
            sys.stdout.flush()
            yield fd
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

    @staticmethod
    def get_key(fd=None):
        """Reads a key or escape sequence. If fd is provided, assumes already in raw mode."""
        if fd is None:
            with Navigator.raw_mode() as raw_fd:
                return Navigator._read_key(raw_fd)
        return Navigator._read_key(fd)

    @staticmethod
    def _read_key(fd):
        try:
            # Read first character using raw FD
            ch = os.read(fd, 1).decode("utf-8", "ignore")
            if ch == "\x1b" and select.select([fd], [], [], 0.05)[0]:
                ch += os.read(fd, 1).decode("utf-8", "ignore")

                if ch == "\x1b[" and select.select([fd], [], [], 0.05)[0]:
                    next_ch = os.read(fd, 1).decode("utf-8", "ignore")
                    ch += next_ch

                    if next_ch == "M":
                        # Standard X11 Mouse tracking
                        for _ in range(3):
                            ch += os.read(fd, 1).decode("utf-8", "ignore")
                        return "MOUSE_EVENT"

                    if next_ch == "<":
                        # SGR Mouse tracking
                        while True:
                            last = os.read(fd, 1).decode("utf-8", "ignore")
                            ch += last
                            if last in ("m", "M") or len(ch) > 25:
                                break
                        return "MOUSE_EVENT"

                    # Other CSI sequences (Arrows, PageUp, etc.)
                    while select.select([fd], [], [], 0.02)[0]:
                        last = os.read(fd, 1).decode("utf-8", "ignore")
                        ch += last
                        if last.isalpha() or last == "~":
                            break

            # Check if it was a fragmented mouse sequence
            if ("M" in ch or "m" in ch) and (ch.startswith("\x1b[M") or ch.startswith("\x1b[<")):
                return "MOUSE_EVENT"
        except Exception:
            return ""
        return ch

    @staticmethod
    def wait_for_return():
        """Standardized non-blocking return/exit prompt."""
        print("\n\033[1;90mPress Enter to return, ESC to exit... \033[0m", end="", flush=True)
        while True:
            key = Navigator.get_key()
            if key in Navigator.ENTER:
                print()
                return True
            if key == Navigator.ESC and len(key) == 1:
                print()
                return False

    @staticmethod
    def hide_cursor():
        sys.stdout.write("\x1b[?25l")
        sys.stdout.flush()

    @staticmethod
    def show_cursor():
        sys.stdout.write("\x1b[?25h")
        sys.stdout.flush()


class InteractiveMenu:
    def __init__(self, title, options, show_banner=None):
        self.title = title
        self.options = options
        self.selected_index = 0
        self.show_banner = show_banner

    def render(self):
        buf = ["\033[H"]
        if self.show_banner:
            import io

            old_stdout = sys.stdout
            sys.stdout = io.StringIO()
            self.show_banner()
            buf.append(sys.stdout.getvalue())
            sys.stdout = old_stdout

        buf.append(f"\n \033[1;35m{self.title}\033[0m\033[K\n")
        buf.append("-" * 50 + "\033[K\n")
        for i, (label, desc) in enumerate(self.options):
            prefix = " \033[1;36m>\033[0m " if i == self.selected_index else "   "
            style = "\033[1;36m" if i == self.selected_index else ""
            buf.append(f"{prefix}{style}{label:<15}{RESET} {desc}\033[K\n")
        buf.append("-" * 50 + "\033[K\n")
        buf.append(f"{GRAY} ↑/↓: Navigate | Enter: Select | ESC: Quit{RESET}\033[K\n")
        buf.append("\033[J")
        sys.stdout.write("".join(buf))
        sys.stdout.flush()

    def run(self):
        Navigator.hide_cursor()
        sys.stdout.write("\033[2J")
        try:
            with Navigator.raw_mode() as fd:
                while True:
                    self.render()
                    key = Navigator.get_key(fd)
                    if key in (Navigator.UP, "\x1bOA"):
                        self.selected_index = (self.selected_index - 1) % len(self.options)
                    elif key in (Navigator.DOWN, "\x1bOB"):
                        self.selected_index = (self.selected_index + 1) % len(self.options)
                    elif key in (Navigator.LEFT, Navigator.RIGHT, "\x1bOC", "\x1bOD"):
                        continue
                    elif key in Navigator.ENTER:
                        return self.selected_index
                    elif key == Navigator.ESC and len(key) == 1:
                        return None
                    elif key == "MOUSE_EVENT":
                        continue
        finally:
            Navigator.show_cursor()


class AnalyzeSelector:
    def __init__(self, title, items, show_banner=None, can_select=True):
        self.title = title
        self.items = items
        self.selected_index = 0
        self.selected_items = set()
        self.sort_reverse = True
        self.show_banner = show_banner
        self.can_select = can_select
        self.page_size = 15
        self.current_page = 0
        self._sort_items()

    def _sort_items(self):
        self.selected_items.clear()
        self.items.sort(key=lambda x: x["size"], reverse=self.sort_reverse)

    def render(self):
        buf = ["\033[H"]
        if self.show_banner:
            import io

            old_stdout = sys.stdout
            sys.stdout = io.StringIO()
            self.show_banner()
            buf.append(sys.stdout.getvalue())
            sys.stdout = old_stdout

        buf.append(f"\n \033[1;35m{self.title}\033[0m\033[K\n")
        hint = (
            f"{GRAY}Select a location to explore (Type numbers or Space to select):{RESET}"
            if self.can_select
            else f"{GRAY}Select a category to explore:{RESET}"
        )
        buf.append(f"{hint}\033[K\n\n")

        import shutil

        columns = shutil.get_terminal_size().columns
        available = columns - (2 + 5 + 8 + 3 + 2 + 5 + 12 + 5)
        bar_w = 20 if available > 40 else 10 if available > 20 else 0
        name_w = min(30, available - bar_w) if bar_w > 0 else max(15, available)

        total_len = len(self.items)
        total_pages = (total_len + self.page_size - 1) // self.page_size
        self.current_page = max(0, min(self.current_page, total_pages - 1))
        start = self.current_page * self.page_size
        end = min(start + self.page_size, total_len)

        for i in range(start, end):
            item = self.items[i]
            is_hover = i == self.selected_index
            is_selected = i in self.selected_items
            cursor = "\033[1;36m▶\033[0m" if is_hover else " "
            if self.can_select:
                num = (i - start) + 1
                inner = "\033[1;32m✓\033[0m" if is_selected else str(num)
                checkbox_str = "[\033[1;32m✓\033[0m] " if is_selected else f"[{inner}] "
            else:
                checkbox_str = f" {i + 1:2}. "

            bar = draw_bar(item["percent"], width=bar_w, force_color=item.get("color"))
            bar_str = f"{bar}  " if bar_w > 0 else ""
            style = "\033[1;35m" if is_hover else ""
            name_padded = pad_and_truncate(item["name"], name_w)
            icon = item.get("icon", "📁")
            age_str = f" {GRAY}{item.get('age_hint', '')}{RESET}" if item.get("age_hint") else ""
            buf.append(
                f"{cursor} {checkbox_str}{bar_str}{item['percent']:>5.1f}%  |  {icon} {style}{name_padded}{RESET}  {WHITE}{bytes_to_human(item['size']):>12}{RESET}{age_str}\033[K\n"
            )

        buf.append("\n" + "-" * (columns - 2) + "\033[K\n")
        order_icon = "↓" if self.sort_reverse else "↑"
        page_info = f" Page {self.current_page + 1}/{total_pages} |" if total_pages > 1 else ""

        if self.can_select:
            buf.append(
                f"\033[1;90m {page_info} ↑↓←→ | Num/Space: Select | A: All | ←: Back | Enter: Open | F: Dir | Del | R | S: Sort {order_icon} | ESC\033[0m\033[K\n"
            )
        else:
            buf.append(
                f"\033[1;90m {page_info} ↑↓: Nav | ←: Back | →: Open | F: Dir | R | S: Sort {order_icon} | ESC\033[0m\033[K\n"
            )

        if self.selected_items:
            buf.append("\n \033[1;35m☉ Selected Items to Remove:\033[0m\033[K\n")
            for i in sorted(list(self.selected_items)):
                item = self.items[i]
                buf.append(f"   \033[1;35m•\033[0m {item.get('icon', '📁')} {item['name']}\033[K\n")

        buf.append("\033[J")  # Clear remaining
        sys.stdout.write("".join(buf))
        sys.stdout.flush()

    def run(self):
        if not self.items:
            return None, None
        Navigator.hide_cursor()
        sys.stdout.write("\033[2J")
        try:
            with Navigator.raw_mode() as fd:
                while True:
                    self.render()
                    key = Navigator.get_key(fd)
                    total_len = len(self.items)
                    if key in (Navigator.UP, "\x1bOA"):
                        if total_len > 0:
                            self.selected_index = (self.selected_index - 1) % total_len
                            self.current_page = self.selected_index // self.page_size
                    elif key in (Navigator.DOWN, "\x1bOB"):
                        if total_len > 0:
                            self.selected_index = (self.selected_index + 1) % total_len
                            self.current_page = self.selected_index // self.page_size
                    elif key == Navigator.PGUP:
                        if total_len > 0:
                            self.current_page = (self.current_page - 1) % (
                                (total_len + self.page_size - 1) // self.page_size
                            )
                            self.selected_index = self.current_page * self.page_size
                    elif key == Navigator.PGDN:
                        if total_len > 0:
                            self.current_page = (self.current_page + 1) % (
                                (total_len + self.page_size - 1) // self.page_size
                            )
                            self.selected_index = self.current_page * self.page_size
                    elif key in (Navigator.LEFT, "\x1bOD"):
                        if self.current_page == 0:
                            return "BACK", None
                        total_pages = (total_len + self.page_size - 1) // self.page_size
                        if total_pages > 1:
                            self.current_page = (self.current_page - 1) % total_pages
                            self.selected_index = self.current_page * self.page_size
                        else:
                            return "BACK", None
                    elif key in (Navigator.RIGHT, "\x1bOC"):
                        total_pages = (total_len + self.page_size - 1) // self.page_size
                        if self.items[self.selected_index]["path"].is_dir():
                            return "DRILL_DOWN", self.selected_index
                        elif total_pages > 1:
                            self.current_page = (self.current_page + 1) % total_pages
                            self.selected_index = self.current_page * self.page_size
                    elif key == Navigator.SPACE and self.can_select:
                        if self.selected_index in self.selected_items:
                            self.selected_items.remove(self.selected_index)
                        else:
                            self.selected_items.add(self.selected_index)
                    elif key.isdigit() and self.can_select:
                        num_str = key
                        while True:
                            if select.select([fd], [], [], 0.4)[0]:
                                next_char = os.read(fd, 1).decode("utf-8", "ignore")
                                if next_char.isdigit():
                                    num_str += next_char
                                else:
                                    break
                            else:
                                break
                        try:
                            num = int(num_str)
                            page_offset = 9 if num_str == "0" else num - 1
                            idx = self.current_page * self.page_size + page_offset
                            if idx < total_len:
                                if idx in self.selected_items:
                                    self.selected_items.remove(idx)
                                else:
                                    self.selected_items.add(idx)
                        except Exception:
                            pass
                    elif key in Navigator.ENTER:
                        return "OPEN_ITEM", self.selected_index
                    elif len(key) == 1 and key.lower() == "s":
                        self.sort_reverse = not self.sort_reverse
                        self._sort_items()
                    elif len(key) == 1 and key.lower() == "r":
                        return "REFRESH", None
                    elif len(key) == 1 and key.lower() == "f":
                        if self.can_select:
                            if self.selected_items:
                                return "OPEN_BATCH", list(self.selected_items)
                            return "OPEN", self.selected_index
                        else:
                            return "DRILL_DOWN", self.selected_index
                    elif len(key) == 1 and key.lower() == "a" and self.can_select:
                        start = self.current_page * self.page_size
                        end = min(start + self.page_size, total_len)
                        page_indices = set(range(start, end))
                        if page_indices.issubset(self.selected_items):
                            self.selected_items -= page_indices
                        else:
                            self.selected_items |= page_indices
                    elif key == Navigator.ESC and len(key) == 1:
                        return "QUIT", None
                    elif key == "MOUSE_EVENT":
                        continue
        finally:
            Navigator.show_cursor()


class PaginatedSelector:
    def __init__(self, title, items, page_size=10):
        self.title = title
        self.items = items
        self.page_size = page_size
        self.current_page = 0
        self.selected_index = 0
        self.selected_items = set()

    def render(self):
        buf = ["\033[H"]
        buf.append(f"\n \033[1;35m{self.title}\033[0m\033[K\n")
        buf.append("-" * 60 + "\033[K\n")
        start = self.current_page * self.page_size
        end = min(start + self.page_size, len(self.items))
        for i in range(start, end):
            item = self.items[i]
            is_hover = i == self.selected_index
            is_checked = i in self.selected_items
            cursor = "\033[1;35m>\033[0m" if is_hover else " "
            checkbox = "[\033[1;32m✓\033[0m]" if is_checked else "[ ]"
            style = "\033[1;37m" if is_hover else ""
            name_padded = pad_and_truncate(item["project"], 20)
            size_str = bytes_to_human(item["size"])
            buf.append(f"{cursor} {checkbox} {style}{name_padded}{RESET} | {size_str:>10}\033[K\n")
        buf.append("-" * 60 + "\033[K\n")
        total_pages = (len(self.items) + self.page_size - 1) // self.page_size
        buf.append(
            f" Page {self.current_page + 1}/{total_pages} | {GRAY}Space: Select | A: All | Enter: Confirm | S: Manage Paths | ESC: Exit{RESET}\033[K\n"
        )
        buf.append("\033[J")
        sys.stdout.write("".join(buf))
        sys.stdout.flush()

    def run(self):
        if not self.items:
            return None
        Navigator.hide_cursor()
        sys.stdout.write("\033[2J")
        try:
            with Navigator.raw_mode() as fd:
                while True:
                    self.render()
                    key = Navigator.get_key(fd)
                    total_len = len(self.items)
                    if key in (Navigator.UP, "\x1bOA"):
                        self.selected_index = (self.selected_index - 1) % total_len
                        self.current_page = self.selected_index // self.page_size
                    elif key in (Navigator.DOWN, "\x1bOB"):
                        self.selected_index = (self.selected_index + 1) % total_len
                        self.current_page = self.selected_index // self.page_size
                    elif key in (Navigator.RIGHT, "\x1bOC"):
                        total_pages = (total_len + self.page_size - 1) // self.page_size
                        if total_pages > 1:
                            self.current_page = (self.current_page + 1) % total_pages
                            self.selected_index = self.current_page * self.page_size
                    elif key in (Navigator.LEFT, "\x1bOD"):
                        total_pages = (total_len + self.page_size - 1) // self.page_size
                        if total_pages > 1:
                            self.current_page = (self.current_page - 1) % total_pages
                            self.selected_index = self.current_page * self.page_size
                    elif key == Navigator.SPACE:
                        if self.selected_index in self.selected_items:
                            self.selected_items.remove(self.selected_index)
                        else:
                            self.selected_items.add(self.selected_index)
                    elif len(key) == 1 and key.lower() == "a":
                        start = self.current_page * self.page_size
                        end = min(start + self.page_size, total_len)
                        page_indices = set(range(start, end))
                        if page_indices.issubset(self.selected_items):
                            self.selected_items -= page_indices
                        else:
                            self.selected_items |= page_indices
                    elif key in Navigator.ENTER:
                        if not self.selected_items:
                            self.selected_items.add(self.selected_index)
                        return list(self.selected_items)
                    elif len(key) == 1 and key.lower() == "s":
                        return "MANAGE_PATHS"
                    elif key == Navigator.ESC and len(key) == 1:
                        return None
                    elif key == "MOUSE_EVENT":
                        continue
        finally:
            Navigator.show_cursor()


class UninstallSelector:
    def __init__(self, title, items):
        self.title = title
        self.items = items
        self.selected_index = 0
        self.selected_ids = set()
        self.sort_key = "size_bytes"
        self.sort_reverse = True
        self.page_size = 15
        self.current_page = 0
        self._sort_items()

    def _sort_items(self):
        if self.sort_key == "name":
            self.items.sort(key=lambda x: x["name"].lower(), reverse=not self.sort_reverse)
        else:
            self.items.sort(key=lambda x: x[self.sort_key], reverse=self.sort_reverse)

    def _format_time_ago(self, timestamp):
        if timestamp == 0:
            return "Unknown"
        diff = time.time() - timestamp
        if diff < 3600:
            return "Just now"
        if diff < 86400:
            return f"{int(diff / 3600)}h ago"
        if diff < 172800:
            return "Yesterday"
        if diff < 2592000:
            return f"{int(diff / 86400)}d ago"
        if diff < 31536000:
            return f"{int(diff / 2592000)}mo ago"
        return f"{int(diff / 31536000)}y ago"

    def render(self):
        buf = ["\033[H"]
        buf.append(f"\n \033[1;36m{self.title}\033[0m\033[K\n")
        buf.append("-" * 80 + "\033[K\n")
        total_len = len(self.items)
        if total_len == 0:
            buf.append(f"\n   {GRAY}No applications found{RESET}\033[K\n")
        else:
            total_pages = (total_len + self.page_size - 1) // self.page_size
            self.current_page = max(0, min(self.current_page, total_pages - 1))
            start = self.current_page * self.page_size
            end = min(start + self.page_size, total_len)
            for i in range(start, end):
                item = self.items[i]
                is_hover = i == self.selected_index
                is_selected = item["id"] in self.selected_ids
                num_key = str((i - start) + 1)
                cursor = "\033[1;36m▶\033[0m" if is_hover else " "
                check_inner = "\033[1;32m✓ \033[0m" if is_selected else f"{num_key:<2}"
                checkbox = f"[{check_inner}]"
                name_style = "\033[1;35m" if is_selected else "\033[1;36m" if is_hover else ""
                name_padded = pad_and_truncate(item["name"], 35)
                buf.append(
                    f"{cursor} {checkbox} {name_style}{name_padded}{RESET}     {item['size_str']:>12} | {self._format_time_ago(item['install_time'])}\033[K\n"
                )
            buf.append("-" * 80 + "\033[K\n")
            order_icon = "↓" if self.sort_reverse else "↑"
            buf.append(
                f" Page {self.current_page + 1}/{total_pages} | {GRAY}Spc: Select | A: All | ←: Back | Enter: Confirm | S/N/T: Sort {order_icon} | ESC{RESET}\033[K\n"
            )

        if self.selected_ids:
            buf.append("\n \033[1;35m☉ Selected Apps to Remove:\033[0m\033[K\n")
            selected_names = [i["name"] for i in self.items if i["id"] in self.selected_ids]
            for i in range(0, len(selected_names), 2):
                pair = selected_names[i : i + 2]
                line = ""
                for name in pair:
                    line += f"   \033[1;35m•\033[0m {pad_and_truncate(name, 35)}"
                buf.append(line + "\033[K\n")

        buf.append("\033[J")
        sys.stdout.write("".join(buf))
        sys.stdout.flush()

    def run(self):
        if not self.items:
            return []
        Navigator.hide_cursor()
        sys.stdout.write("\033[2J")
        try:
            with Navigator.raw_mode() as fd:
                while True:
                    self.render()
                    key = Navigator.get_key(fd)
                    total_len = len(self.items)
                    if key in (Navigator.UP, "\x1bOA"):
                        if total_len > 0:
                            self.selected_index = (self.selected_index - 1) % total_len
                            self.current_page = self.selected_index // self.page_size
                    elif key in (Navigator.DOWN, "\x1bOB"):
                        if total_len > 0:
                            self.selected_index = (self.selected_index + 1) % total_len
                            self.current_page = self.selected_index // self.page_size
                    elif key == Navigator.PGUP:
                        if total_len > 0:
                            self.current_page = (self.current_page - 1) % (
                                (total_len + self.page_size - 1) // self.page_size
                            )
                            self.selected_index = self.current_page * self.page_size
                    elif key == Navigator.PGDN:
                        if total_len > 0:
                            self.current_page = (self.current_page + 1) % (
                                (total_len + self.page_size - 1) // self.page_size
                            )
                            self.selected_index = self.current_page * self.page_size
                    elif key in (Navigator.LEFT, "\x1bOD"):
                        if self.current_page == 0:
                            return []
                        total_pages = (total_len + self.page_size - 1) // self.page_size
                        if total_pages > 1:
                            self.current_page = (self.current_page - 1) % total_pages
                            self.selected_index = self.current_page * self.page_size
                        else:
                            return []
                    elif key in (Navigator.RIGHT, "\x1bOC"):
                        total_pages = (total_len + self.page_size - 1) // self.page_size
                        if total_pages > 1:
                            self.current_page = (self.current_page + 1) % total_pages
                            self.selected_index = self.current_page * self.page_size
                    elif key == Navigator.SPACE and total_len > 0:
                        item_id = self.items[self.selected_index]["id"]
                        if item_id in self.selected_ids:
                            self.selected_ids.remove(item_id)
                        else:
                            self.selected_ids.add(item_id)
                    elif key.isdigit() and total_len > 0:
                        num_str = key
                        while select.select([fd], [], [], 0.4)[0]:
                            num_str += os.read(fd, 1).decode("utf-8", "ignore")
                        try:
                            num = int(num_str)
                            page_offset = 9 if num_str == "0" else num - 1
                            idx = self.current_page * self.page_size + page_offset
                            if idx < total_len:
                                item_id = self.items[idx]["id"]
                                if item_id in self.selected_ids:
                                    self.selected_ids.remove(item_id)
                                else:
                                    self.selected_ids.add(item_id)
                        except Exception:
                            pass
                    elif len(key) == 1 and key.lower() in ("s", "n", "t"):
                        self.sort_key = (
                            "size_bytes"
                            if key.lower() == "s"
                            else "name"
                            if key.lower() == "n"
                            else "install_time"
                        )
                        self.sort_reverse = not self.sort_reverse
                        self._sort_items()
                    elif len(key) == 1 and key.lower() == "a":
                        start = self.current_page * self.page_size
                        end = min(start + self.page_size, total_len)
                        page_ids = {self.items[i]["id"] for i in range(start, end)}
                        if page_ids.issubset(self.selected_ids):
                            self.selected_ids -= page_ids
                        else:
                            self.selected_ids |= page_ids
                    elif key in Navigator.ENTER or key == "\x1b[3~":  # Enter or Del
                        if not self.selected_ids:
                            return [self.selected_index] if total_len > 0 else []
                        return [
                            i
                            for i, item in enumerate(self.items)
                            if item["id"] in self.selected_ids
                        ]
                    elif key == Navigator.ESC and len(key) == 1:
                        return []
                    elif key == "MOUSE_EVENT":
                        continue
        finally:
            Navigator.show_cursor()


class TopFilesSelector:
    def __init__(self, title, items):
        self.title, self.items, self.selected_index, self.selected_items = (
            title,
            items,
            0,
            set(),
        )

    def render(self):
        buf = ["\033[H"]
        import shutil

        columns = shutil.get_terminal_size().columns
        buf.append(f"\n \033[1;33m{self.title}\033[0m\033[K\n")
        buf.append("-" * (columns - 2) + "\033[K\n")
        viewport = 20
        start = max(0, self.selected_index - viewport // 2)
        end = min(len(self.items), start + viewport)
        for i in range(start, end):
            item = self.items[i]
            cursor = "\033[1;36m▶\033[0m" if i == self.selected_index else " "
            checkbox = "[\033[1;32m✓\033[0m]" if i in self.selected_items else "[ ]"
            buf.append(
                f"{cursor} {checkbox} {WHITE}{bytes_to_human(item.get('size', item.get('size_bytes', 0))):>12}{RESET} | {str(item['path'])}\033[K\n"
            )
        buf.append("-" * (columns - 2) + "\033[K\n")
        buf.append(f"{GRAY} ↑/↓: Move | Space: Toggle | Enter: Delete | ESC: Back{RESET}\033[K\n")
        if self.selected_items:
            buf.append("\n \033[1;35m☉ Selected Large Files to Remove:\033[0m\033[K\n")
            for i in sorted(list(self.selected_items)):
                buf.append(f"   \033[1;35m•\033[0m 📄 {Path(self.items[i]['path']).name}\033[K\n")
        buf.append("\033[J")
        sys.stdout.write("".join(buf))
        sys.stdout.flush()

    def run(self):
        if not self.items:
            return []
        Navigator.hide_cursor()
        sys.stdout.write("\033[2J")
        try:
            with Navigator.raw_mode() as fd:
                while True:
                    self.render()
                    key = Navigator.get_key(fd)
                    if key in (Navigator.UP, "\x1bOA"):
                        self.selected_index = (self.selected_index - 1) % len(self.items)
                    elif key in (Navigator.DOWN, "\x1bOB"):
                        self.selected_index = (self.selected_index + 1) % len(self.items)
                    elif key == Navigator.SPACE:
                        if self.selected_index in self.selected_items:
                            self.selected_items.remove(self.selected_index)
                        else:
                            self.selected_items.add(self.selected_index)
                    elif key in Navigator.ENTER:
                        return (
                            list(self.selected_items)
                            if self.selected_items
                            else [self.selected_index]
                        )
                    elif key == Navigator.ESC and len(key) == 1:
                        return []
                    elif key == "MOUSE_EVENT":
                        continue
        finally:
            Navigator.show_cursor()


class ConfirmSelector:
    def __init__(self, message):
        self.message, self.selected_index = message, 1

    def render(self):
        buf = ["\033[H"]
        buf.append(f"\n  {BOLD}{self.message}{RESET}\033[K\n")
        y = (
            "\033[1;37m\033[45m Yes \033[0m"
            if self.selected_index == 0
            else f"  {GRAY}Yes{RESET}  "
        )
        n = "\033[1;37m\033[45m No \033[0m" if self.selected_index == 1 else f"  {GRAY}No{RESET}  "
        buf.append(f"  {y}   {n}\033[K\n\n")
        buf.append("\033[J")
        sys.stdout.write("".join(buf))
        sys.stdout.flush()

    def run(self):
        Navigator.hide_cursor()
        sys.stdout.write("\033[2J")
        try:
            with Navigator.raw_mode() as fd:
                while True:
                    self.render()
                    key = Navigator.get_key(fd)
                    if key in (
                        Navigator.LEFT,
                        Navigator.RIGHT,
                        Navigator.UP,
                        Navigator.DOWN,
                        "\x1bOA",
                        "\x1bOB",
                        "\x1bOC",
                        "\x1bOD",
                    ):
                        self.selected_index = 1 - self.selected_index
                    elif len(key) == 1 and key.lower() == "y":
                        return True
                    elif len(key) == 1 and key.lower() == "n":
                        return False
                    elif key in Navigator.ENTER:
                        return self.selected_index == 0
                    elif key == Navigator.ESC and len(key) == 1:
                        return False
                    elif key == "MOUSE_EVENT":
                        continue
        finally:
            Navigator.show_cursor()


class CleanSelector:
    def __init__(self, title, items):
        self.title = title
        self.items = items
        self.selected_index = 0
        self.selected_items = set(range(len(items)))

    def render(self):
        buf = ["\033[H"]
        buf.append(f"\n \033[1;36m{self.title}\033[0m\033[K\n")
        buf.append("-" * 65 + "\033[K\n")
        total_freed = 0
        for i, item in enumerate(self.items):
            is_hover = i == self.selected_index
            is_checked = i in self.selected_items
            if is_checked:
                total_freed += item["size"]
            cursor = "\033[1;36m▶\033[0m" if is_hover else " "
            checkbox = "[\033[1;32m✓\033[0m]" if is_checked else "[ ]"
            name_padded = pad_and_truncate(item["name"], 25)
            size_str = bytes_to_human(item["size"]) if item["size"] > 0 else "Scan Result"
            buf.append(
                f"{cursor} {checkbox} \033[1;36m{name_padded}{RESET} |     {size_str:>12} | {GRAY}{item['desc']}{RESET}\033[K\n"
            )
        buf.append("-" * 65 + "\033[K\n")
        buf.append(f" Total Selected: \033[1;32m{bytes_to_human(total_freed)}\033[0m\033[K\n")
        buf.append(
            f"\n{GRAY} ↑/↓: Move | Space: Toggle | Enter: Clean Selected | ESC: Cancel{RESET}\033[K\n"
        )
        buf.append("\033[J")
        sys.stdout.write("".join(buf))
        sys.stdout.flush()

    def run(self):
        Navigator.hide_cursor()
        sys.stdout.write("\033[2J")
        try:
            with Navigator.raw_mode() as fd:
                while True:
                    self.render()
                    key = Navigator.get_key(fd)
                    if key in (Navigator.UP, "\x1bOA"):
                        self.selected_index = (self.selected_index - 1) % len(self.items)
                    elif key in (Navigator.DOWN, "\x1bOB"):
                        self.selected_index = (self.selected_index + 1) % len(self.items)
                    elif key == Navigator.SPACE:
                        if self.selected_index in self.selected_items:
                            self.selected_items.remove(self.selected_index)
                        else:
                            self.selected_items.add(self.selected_index)
                    elif key in Navigator.ENTER or key == Navigator.DEL:
                        if not self.selected_items:
                            continue
                        return list(self.selected_items)
                    elif key == Navigator.ESC and len(key) == 1:
                        return []
                    elif key == "MOUSE_EVENT":
                        continue
        finally:
            Navigator.show_cursor()
