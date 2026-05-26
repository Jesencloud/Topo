import sys
import tty
import termios
import os
import time
import select
from pathlib import Path
from ..core.constants import GRAY, RESET, WHITE, BOLD, BLUE, CYAN, MAGENTA, YELLOW, GREEN, RED
from ..core.file_ops import bytes_to_human

def get_display_width(s: str) -> int:
    """Calculates visual width of a string, accounting for CJK characters."""
    width = 0
    for char in s:
        if ord(char) > 0x1100: width += 2
        else: width += 1
    return width

def pad_and_truncate(s: str, target_width: int) -> str:
    """Truncates and pads a string to a target visual width."""
    current_width = get_display_width(s)
    if current_width <= target_width:
        return s + " " * (target_width - current_width)
    res = ""; res_width = 0
    for char in s:
        char_width = 2 if ord(char) > 0x1100 else 1
        if res_width + char_width + 3 > target_width: break
        res += char; res_width += char_width
    return res + "..." + " " * (target_width - (res_width + 3))

def draw_bar(percentage: float, width: int = 20, force_color: str = None) -> str:
    if width <= 0: return ""
    safe_percent = max(0.0, min(100.0, percentage))
    filled = int(width * safe_percent / 100)
    empty = width - filled
    if force_color: color = force_color
    else:
        if safe_percent < 50: color = GREEN
        elif safe_percent < 80: color = YELLOW
        else: color = RED
    return f"{color}{'▬' * filled}{RESET}{GRAY}{'▬' * empty}{RESET}"

class Navigator:
    """Handles raw terminal input and basic TUI navigation."""
    UP = '\x1b[A'
    DOWN = '\x1b[B'
    RIGHT = '\x1b[C'
    LEFT = '\x1b[D'
    ENTER = '\r'
    ESC = '\x1b'
    SPACE = ' '
    Q = 'q'

    @staticmethod
    def get_key():
        """The original stable key reader."""
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setraw(sys.stdin.fileno())
            ch = sys.stdin.read(1)
            if ch == '\x1b':
                ch += sys.stdin.read(2)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        return ch

    @staticmethod
    def hide_cursor(): print("\x1b[?25l", end="")
    @staticmethod
    def show_cursor(): print("\x1b[?25h", end="")

class InteractiveMenu:
    def __init__(self, title, options, show_banner=None):
        self.title = title; self.options = options
        self.selected_index = 0; self.show_banner = show_banner

    def render(self):
        os.system('clear')
        if self.show_banner: self.show_banner()
        print(f"\n \033[1m{self.title}\033[0m")
        print("-" * 50)
        for i, (label, desc) in enumerate(self.options):
            prefix = " \033[1;36m>\033[0m " if i == self.selected_index else "   "
            style = "\033[1;36m" if i == self.selected_index else ""
            print(f"{prefix}{style}{label:<15}{RESET} {desc}")
        print("-" * 50)
        print(f"{GRAY} ↑/↓: Navigate | Enter: Select | ESC: Quit{RESET}")

    def run(self):
        Navigator.hide_cursor()
        try:
            while True:
                self.render()
                key = Navigator.get_key()
                if key == Navigator.UP: self.selected_index = (self.selected_index - 1) % len(self.options)
                elif key == Navigator.DOWN: self.selected_index = (self.selected_index + 1) % len(self.options)
                elif key == Navigator.ENTER: return self.selected_index
                elif key.lower() == Navigator.Q or key == Navigator.ESC: return None
        finally: Navigator.show_cursor()

class AnalyzeSelector:
    def __init__(self, title, items, show_banner=None, can_select=True):
        self.title = title; self.items = items
        self.selected_index = 0; self.selected_items = set()
        self.sort_reverse = True; self.show_banner = show_banner
        self.can_select = can_select; self._sort_items()

    def _sort_items(self):
        self.selected_items.clear()
        self.items.sort(key=lambda x: x['size'], reverse=self.sort_reverse)

    def render(self):
        os.system('clear')
        if self.show_banner: self.show_banner()
        print(f"\n \033[1;35m{self.title}\033[0m")
        hint = f"{GRAY}Select a location to explore (Type numbers or Space to select):{RESET}" if self.can_select else f"{GRAY}Select a category to explore:{RESET}"
        import shutil
        columns = shutil.get_terminal_size().columns
        fixed_overhead = 2 + 5 + 8 + 3 + 2 + 5 + 12 + 5
        available = columns - fixed_overhead
        if available > 40: bar_w = 20; name_w = min(40, available - bar_w)
        elif available > 20: bar_w = 10; name_w = available - bar_w
        else: bar_w = 0; name_w = max(15, available)
        print(f"{hint}\n")
        for i, item in enumerate(self.items):
            is_hover = i == self.selected_index
            is_selected = i in self.selected_items
            cursor = "\033[1;36m▶\033[0m" if is_hover else " "
            if self.can_select:
                num = i + 1
                inner = "\033[1;32m✓\033[0m" if is_selected else str(num)
                raw_box = f"[{inner}]"; checkbox_str = f"{raw_box:<4} "
            else: checkbox_str = f" {i+1:2}. "
            bar = draw_bar(item['percent'], width=bar_w, force_color=item.get('color'))
            bar_str = f"{bar}  " if bar_w > 0 else ""
            style = "\033[1;37m" if is_hover else ""
            name_padded = pad_and_truncate(item['name'], name_w)
            icon = item.get('icon', '📁'); age_hint = item.get('age_hint', '')
            age_str = f" {GRAY}{age_hint}{RESET}" if age_hint else ""
            print(f"{cursor} {checkbox_str}{bar_str}{item['percent']:>5.1f}%  |  {icon} {style}{name_padded}{RESET}     {WHITE}{bytes_to_human(item['size']):>12}{RESET}{age_str}")
        print("\n" + "-" * (columns - 2))
        order_icon = "↓" if self.sort_reverse else "↑"
        if self.selected_items:
            print(f"\n \033[1;35m☉ Selected Items to Remove:\033[0m")
            for i in sorted(list(self.selected_items)):
                item = self.items[i]
                print(f"   \033[1;35m•\033[0m {item.get('icon', '📁')} {item['name']}")
        if self.can_select:
            print(f"\033[1;90m ↑↓←→ | Num Select | Space Select | A All | ← Back | Enter Open/In | D Delete | R Refresh | S Sort {order_icon} | ESC Exit\033[0m")
        else:
            print(f"\033[1;90m ↑↓→ | Enter Explore | R Refresh | S Sort {order_icon} | ESC Exit\033[0m")

    def run(self):
        if not self.items: return None, None
        Navigator.hide_cursor()
        try:
            while True:
                self.render()
                key = Navigator.get_key()
                if key == Navigator.UP: self.selected_index = (self.selected_index - 1) % len(self.items)
                elif key == Navigator.DOWN: self.selected_index = (self.selected_index + 1) % len(self.items)
                elif key in (Navigator.LEFT, 'b', 'B', 'h', 'H'): return "BACK", None
                elif key == Navigator.SPACE and self.can_select:
                    if self.selected_index in self.selected_items: self.selected_items.remove(self.selected_index)
                    else: self.selected_items.add(self.selected_index)
                elif key.isdigit() and self.can_select:
                    num_str = key
                    fd = sys.stdin.fileno(); old_settings = termios.tcgetattr(fd)
                    try:
                        tty.setraw(fd)
                        while True:
                            r, _, _ = select.select([sys.stdin], [], [], 0.4)
                            if r:
                                next_char = sys.stdin.read(1)
                                if next_char.isdigit(): num_str += next_char
                                else: break
                            else: break
                    finally: termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
                    try:
                        num = int(num_str); idx = 9 if num_str == '0' else num - 1
                        if 0 <= idx < len(self.items):
                            if idx in self.selected_items: self.selected_items.remove(idx)
                            else: self.selected_items.add(idx)
                    except: pass
                elif key in (Navigator.ENTER, Navigator.RIGHT): return "DRILL_DOWN", self.selected_index
                elif key.lower() == 'd' and self.can_select:
                    if not self.selected_items: continue
                    return "DELETE_BATCH", list(self.selected_items)
                elif key.lower() == 's': self.sort_reverse = not self.sort_reverse; self._sort_items()
                elif key.lower() == 'r': return "REFRESH", None
                elif key.lower() == 'o':
                    if self.can_select:
                        if self.selected_items: return "OPEN_BATCH", list(self.selected_items)
                        return "OPEN", self.selected_index
                    else: return "DRILL_DOWN", self.selected_index
                elif key.lower() == 'a' and self.can_select:
                    if len(self.selected_items) == len(self.items): self.selected_items.clear()
                    else: self.selected_items = set(range(len(self.items)))
                elif key.lower() == 'f': return "SWITCH_FILES", None
                elif key.lower() == 'q' or key == Navigator.ESC: return "QUIT", None
        finally: Navigator.show_cursor()

class PaginatedSelector:
    def __init__(self, title, items, page_size=10):
        self.title = title; self.items = items; self.page_size = page_size
        self.current_page = 0; self.selected_index = 0; self.selected_items = set()
    def render(self):
        os.system('clear')
        print(f"\n \033[1;35m{self.title}\033[0m"); print("-" * 60)
        start = self.current_page * self.page_size; end = min(start + self.page_size, len(self.items))
        for i in range(start, end):
            item = self.items[i]; is_hover = i == self.selected_index; is_checked = i in self.selected_items
            cursor = "\033[1;35m>\033[0m" if is_hover else " "
            checkbox = "[\033[1;32m✓\033[0m]" if is_checked else "[ ]"
            style = "\033[1;37m" if is_hover else ""
            print(f"{cursor} {checkbox} {style}{item['human_size']:>8} | {item['project']:<15} | {item['path'].name}{RESET}")
        print("-" * 60); total_pages = (len(self.items) + self.page_size - 1) // self.page_size
        print(f" Page {self.current_page + 1}/{total_pages} | Items: {len(self.items)} | Selected: {len(self.selected_items)}")
        print(f"{GRAY} ↑/↓: Move | Space: Select | A: All | P: Purge | M: Edit Paths | ESC: Back{RESET}")
    def run(self):
        Navigator.hide_cursor()
        try:
            while True:
                self.render(); key = Navigator.get_key()
                if key == Navigator.UP:
                    if len(self.items) > 0:
                        self.selected_index = (self.selected_index - 1) % len(self.items)
                        self.current_page = self.selected_index // self.page_size
                elif key == Navigator.DOWN:
                    if len(self.items) > 0:
                        self.selected_index = (self.selected_index + 1) % len(self.items)
                        self.current_page = self.selected_index // self.page_size
                elif key == Navigator.SPACE:
                    if self.selected_index in self.selected_items: self.selected_items.remove(self.selected_index)
                    else: self.selected_items.add(self.selected_index)
                elif key.lower() == 'a':
                    if len(self.selected_items) == len(self.items): self.selected_items.clear()
                    else: self.selected_items = set(range(len(self.items)))
                elif key.lower() == 'm': return "MANAGE_PATHS"
                elif key.lower() == 'p':
                    if not self.selected_items: continue
                    return list(self.selected_items)
                elif key.lower() == 'q' or key == Navigator.ESC: return []
        finally: Navigator.show_cursor()

class UninstallSelector:
    def __init__(self, title, items):
        self.title = title; self.items = items; self.selected_index = 0
        self.selected_ids = set(); self.sort_key = 'size_bytes'
        self.sort_reverse = True; self.page_size = 10; self.current_page = 0
        self._sort_items()
    def _format_time_ago(self, timestamp):
        if timestamp == 0: return "Unknown"
        diff = time.time() - timestamp
        if diff < 86400: return "Today"
        if diff < 172800: return "Yesterday"
        if diff < 604800: return f"{int(diff/86400)}d ago"
        return f"{int(diff/31536000)}y ago"
    def _sort_items(self):
        if self.sort_key == 'name': self.items.sort(key=lambda x: x['name'].lower(), reverse=not self.sort_reverse)
        else: self.items.sort(key=lambda x: x[self.sort_key], reverse=self.sort_reverse)
    def render(self):
        os.system('clear'); print(f"\n \033[1;36m{self.title}\033[0m"); print("-" * 80)
        total_len = len(self.items)
        if total_len == 0: print(f"\n   {GRAY}No applications found{RESET}\n"); print("-" * 80)
        else:
            total_pages = (total_len + self.page_size - 1) // self.page_size
            self.current_page = max(0, min(self.current_page, total_pages - 1))
            start = self.current_page * self.page_size; end = min(start + self.page_size, total_len)
            for i in range(start, end):
                item = self.items[i]; is_hover = i == self.selected_index; is_selected = item['id'] in self.selected_ids
                num_key = "0" if (i - start) + 1 == 10 else str((i - start) + 1)
                cursor = "\033[1;36m▶\033[0m" if is_hover else " "
                checkbox = "[\033[1;32m✓\033[0m]" if is_selected else f"[{num_key}]"
                name_style = "\033[1;35m" if is_selected else "\033[1;36m" if is_hover else ""
                name_padded = pad_and_truncate(item['name'], 35)
                time_str = self._format_time_ago(item['install_time'])
                print(f"{cursor} {checkbox} {name_style}{name_padded}{RESET}     {item['size_str']:>12} | {time_str}")
            print("-" * 80); order_icon = "↑" if not self.sort_reverse else "↓"
            print(f" Page {self.current_page + 1}/{total_pages} | {GRAY}Keys 1-0: Select | ↑↓: Move | Space: Select | Enter: Confirm{RESET}")
            print(f"{GRAY} S: Size | N: Name | T: Time | O: {order_icon} | ESC: Exit{RESET}")
        if self.selected_ids:
            print(f"\n \033[1;35m☉ Selected Apps to Remove:\033[0m")
            count = 0
            for item in self.items:
                if item['id'] in self.selected_ids:
                    print(f"   \033[1;35m•\033[0m {item['name']}"); count += 1
                    if count >= 8: print(f"   ... and {len(self.selected_ids) - 8} more"); break
    def run(self):
        if not self.items: return []
        Navigator.hide_cursor()
        try:
            while True:
                self.render(); key = Navigator.get_key(); total_len = len(self.items)
                if key == Navigator.UP:
                    if total_len > 0:
                        self.selected_index = (self.selected_index - 1) % total_len
                        self.current_page = self.selected_index // self.page_size
                elif key == Navigator.DOWN:
                    if total_len > 0:
                        self.selected_index = (self.selected_index + 1) % total_len
                        self.current_page = self.selected_index // self.page_size
                elif key == Navigator.SPACE and total_len > 0:
                    item_id = self.items[self.selected_index]['id']
                    if item_id in self.selected_ids: self.selected_ids.remove(item_id)
                    else: self.selected_ids.add(item_id)
                elif key.isdigit() and total_len > 0:
                    num_str = key
                    fd = sys.stdin.fileno(); old_settings = termios.tcgetattr(fd)
                    try:
                        tty.setraw(fd)
                        while True:
                            r, _, _ = select.select([sys.stdin], [], [], 0.4)
                            if r:
                                next_char = sys.stdin.read(1)
                                if next_char.isdigit(): num_str += next_char
                                else: break
                            else: break
                    finally: termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
                    try:
                        num = int(num_str); page_offset = 9 if num_str == '0' else num - 1
                        idx = self.current_page * self.page_size + page_offset
                        if idx < total_len:
                            item_id = self.items[idx]['id']
                            if item_id in self.selected_ids: self.selected_ids.remove(item_id)
                            else: self.selected_ids.add(item_id)
                    except: pass
                elif key.lower() == 's': self.sort_key = 'size_bytes'; self.sort_reverse = not self.sort_reverse; self._sort_items()
                elif key.lower() == 'n': self.sort_key = 'name'; self.sort_reverse = not self.sort_reverse; self._sort_items()
                elif key.lower() == 't': self.sort_key = 'install_time'; self.sort_reverse = not self.sort_reverse; self._sort_items()
                elif key.lower() == 'o': self.sort_reverse = not self.sort_reverse; self._sort_items()
                elif key == Navigator.ENTER:
                    if not self.selected_ids:
                        if total_len > 0: return [self.selected_index]
                        continue
                    return [i for i, item in enumerate(self.items) if item['id'] in self.selected_ids]
                elif key.lower() == 'q' or key == Navigator.ESC: return []
        finally: Navigator.show_cursor()

class TopFilesSelector:
    def __init__(self, title, items):
        self.title = title; self.items = items; self.selected_index = 0; self.selected_items = set()
    def render(self):
        os.system('clear'); import shutil; columns = shutil.get_terminal_size().columns
        print(f"\n \033[1;33m{self.title}\033[0m"); print("-" * (columns - 2))
        viewport_size = 20; start_idx = max(0, self.selected_index - viewport_size // 2)
        end_idx = min(len(self.items), start_idx + viewport_size)
        if end_idx - start_idx < viewport_size: start_idx = max(0, end_idx - viewport_size)
        fixed_overhead = 2 + 4 + 12 + 5 + 3; name_w = columns - fixed_overhead
        for i in range(start_idx, end_idx):
            item = self.items[i]; is_hover = i == self.selected_index; is_checked = i in self.selected_items
            cursor = "\033[1;36m▶\033[0m" if is_hover else " "
            checkbox = "[\033[1;32m✓\033[0m]" if is_checked else "[ ]"
            style = "\033[1;37m" if is_hover else ""
            display_path = pad_and_truncate(str(item['path']), name_w)
            age_str = f" {GRAY}{item.get('age_hint', '')}{RESET}" if item.get('age_hint') else ""
            print(f"{cursor} {checkbox} {WHITE}{bytes_to_human(item['size_bytes']):>12}{RESET}{age_str} | {style}{display_path}{RESET}")
        print("-" * (columns - 2)); print(f" Total: {len(self.items)} files | Selected: {len(self.selected_items)}")
        if self.selected_items:
            print(f"\n \033[1;35m☉ Selected Large Files to Remove:\033[0m")
            for i in sorted(list(self.selected_items)):
                item = self.items[i]; print(f"   \033[1;35m•\033[0m 📄 {Path(item['path']).name}")
        print(f"{GRAY} ↑/↓: Move | Space: Toggle | Enter: Delete Selected | ESC: Back{RESET}")
    def run(self):
        if not self.items: return []
        Navigator.hide_cursor()
        try:
            while True:
                self.render(); key = Navigator.get_key()
                if key == Navigator.UP:
                    if len(self.items) > 0: self.selected_index = (self.selected_index - 1) % len(self.items)
                elif key == Navigator.DOWN:
                    if len(self.items) > 0: self.selected_index = (self.selected_index + 1) % len(self.items)
                elif key == Navigator.SPACE:
                    if self.selected_index in self.selected_items: self.selected_items.remove(self.selected_index)
                    else: self.selected_items.add(self.selected_index)
                elif key == Navigator.ENTER:
                    if not self.selected_items: continue
                    return list(self.selected_items)
                elif key.lower() == 'q' or key == Navigator.ESC: return []
        finally: Navigator.show_cursor()

class ConfirmSelector:
    def __init__(self, message):
        self.message = message; self.options = ["Yes", "No"]; self.selected_index = 1
    def render(self):
        print(f"\n  {BOLD}{self.message}{RESET}")
        btns = []
        for i, opt in enumerate(self.options):
            if i == self.selected_index: btns.append(f"\033[1;37m\033[45m {opt} \033[0m")
            else: btns.append(f"  {GRAY}{opt}{RESET}  ")
        print("  " + "   ".join(btns)); print(f"{GRAY}   ←/→: Select | Enter: Confirm | Y/N: Quick Keys{RESET}")
    def run(self):
        Navigator.hide_cursor()
        try:
            while True:
                self.render(); key = Navigator.get_key()
                if key in (Navigator.LEFT, Navigator.RIGHT): self.selected_index = 1 - self.selected_index
                elif key.lower() == 'y': return True
                elif key.lower() == 'n': return False
                elif key == Navigator.ENTER: return self.selected_index == 0
                print("\033[4A\033[J", end="")
        finally: Navigator.show_cursor(); print()

class CleanSelector:
    def __init__(self, title, items):
        self.title = title; self.items = items; self.selected_index = 0
        self.selected_items = set(range(len(items)))
    def render(self):
        os.system('clear'); print(f"\n \033[1;36m{self.title}\033[0m"); print("-" * 65)
        total_freed = 0
        for i, item in enumerate(self.items):
            is_hover = i == self.selected_index; is_checked = i in self.selected_items
            if is_checked: total_freed += item['size']
            cursor = "\033[1;36m▶\033[0m" if is_hover else " "
            checkbox = "[\033[1;32m✓\033[0m]" if is_checked else "[ ]"
            name_padded = pad_and_truncate(item['name'], 25)
            size_str = bytes_to_human(item['size']) if item['size'] > 0 else "Scan Result"
            print(f"{cursor} {checkbox} \033[1;36m{name_padded}{RESET} |     {size_str:>12} | {GRAY}{item['desc']}{RESET}")
        print("-" * 65); print(f" Total Selected: \033[1;32m{bytes_to_human(total_freed)}\033[0m")
        print(f"\n{GRAY} ↑/↓: Move | Space: Toggle | Enter: Clean Selected | ESC: Cancel{RESET}")
    def run(self):
        Navigator.hide_cursor()
        try:
            while True:
                self.render(); key = Navigator.get_key()
                if key == Navigator.UP: self.selected_index = (self.selected_index - 1) % len(self.items)
                elif key == Navigator.DOWN: self.selected_index = (self.selected_index + 1) % len(self.items)
                elif key == Navigator.SPACE:
                    if self.selected_index in self.selected_items: self.selected_items.remove(self.selected_index)
                    else: self.selected_items.add(self.selected_index)
                elif key == Navigator.ENTER:
                    if not self.selected_items: continue
                    return list(self.selected_items)
                elif key.lower() == 'q' or key == Navigator.ESC: return []
        finally: Navigator.show_cursor()
