import sys
import tty
import termios
import os
import time

# Reusable UI Constants
GRAY = "\033[1;90m"
RESET = "\033[0m"
WHITE = "\033[1;37m"
BOLD = "\033[1m"
BLUE = "\033[1;34m"
CYAN = "\033[1;36m"
MAGENTA = "\033[1;35m"
YELLOW = "\033[1;33m"

def draw_bar(percentage: float, width: int = 20, force_color: str = None) -> str:
    safe_percent = max(0.0, min(100.0, percentage))
    filled = int(width * safe_percent / 100)
    empty = width - filled
    
    # Dynamic color logic if no specific color is forced
    if force_color:
        color = force_color
    else:
        if safe_percent < 50:
            color = "\033[1;32m" # GREEN
        elif safe_percent < 80:
            color = "\033[1;33m" # YELLOW
        else:
            color = "\033[1;31m" # RED
            
    return f"{WHITE}[{color}{'■' * filled}{RESET}{GRAY}{'□' * empty}{WHITE}]{RESET}"

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
        self.title = title
        self.options = options
        self.selected_index = 0
        self.show_banner = show_banner

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
        print(f"{GRAY} ↑/↓: Navigate | Enter: Select | Q: Quit{RESET}")

    def run(self):
        Navigator.hide_cursor()
        try:
            while True:
                self.render()
                key = Navigator.get_key()
                if key == Navigator.UP: self.selected_index = (self.selected_index - 1) % len(self.options)
                elif key == Navigator.DOWN: self.selected_index = (self.selected_index + 1) % len(self.options)
                elif key == Navigator.ENTER: return self.selected_index
                elif key.lower() == Navigator.Q: return None
        finally: Navigator.show_cursor()

class AnalyzeSelector:
    def __init__(self, title, items, show_banner=None):
        self.title = title
        self.items = items
        self.selected_index = 0
        self.sort_reverse = True # Default: Largest to Smallest
        self.show_banner = show_banner
        self._sort_items()

    def _sort_items(self):
        self.items.sort(key=lambda x: x['size'], reverse=self.sort_reverse)

    def render(self):
        os.system('clear')
        if self.show_banner: self.show_banner()
        print(f"\n \033[1;35m{self.title}\033[0m")
        print(f"{GRAY}Select a location to explore:{RESET}\n")
        
        from ..core.file_ops import bytes_to_human

        for i, item in enumerate(self.items):
            is_hover = i == self.selected_index
            cursor = "\033[1;36m▶\033[0m" if is_hover else " "
            
            # Pass color only if it's the root view (which defines 'color'), 
            # otherwise let the dynamic coloring take over
            bar = draw_bar(item['percent'], force_color=item.get('color'))
            
            style = "\033[1;37m" if is_hover else ""
            print(f"{cursor} {i+1:2}. {bar}  {item['percent']:>5.1f}%  |  📁 {style}{item['name']:<20}{RESET} {WHITE}{bytes_to_human(item['size']):>10}{RESET}")

        print("\n" + "-" * 75)
        order_icon = "↓" if self.sort_reverse else "↑"
        print(f"\033[1;90m ↑↓→ | Enter | R Refresh | O Open | F Top Files | S Size {order_icon} | Q Exit to Menu\033[0m")

    def run(self):
        if not self.items: return None, None
        Navigator.hide_cursor()
        try:
            while True:
                self.render()
                key = Navigator.get_key()
                if key == Navigator.UP: self.selected_index = (self.selected_index - 1) % len(self.items)
                elif key == Navigator.DOWN: self.selected_index = (self.selected_index + 1) % len(self.items)
                elif key == Navigator.LEFT: return "BACK", None
                elif key in (Navigator.ENTER, Navigator.RIGHT): return "DRILL_DOWN", self.selected_index
                elif key.lower() == 's':
                    self.sort_reverse = not self.sort_reverse
                    self._sort_items()
                elif key.lower() == 'r': return "REFRESH", None
                elif key.lower() == 'o': return "OPEN", self.selected_index
                elif key.lower() == 'f': return "SWITCH_FILES", None
                elif key.lower() == 'q': return "QUIT", None
        finally: Navigator.show_cursor()

class PaginatedSelector:
    def __init__(self, title, items, page_size=10):
        self.title = title
        self.items = items
        self.page_size = page_size
        self.current_page = 0
        self.selected_index = 0
        self.selected_items = set()

    def render(self):
        os.system('clear')
        print(f"\n \033[1;35m{self.title}\033[0m")
        print("-" * 60)
        start = self.current_page * self.page_size
        end = min(start + self.page_size, len(self.items))
        for i in range(start, end):
            item = self.items[i]
            is_hover = i == self.selected_index
            is_checked = i in self.selected_items
            cursor = "\033[1;35m>\033[0m" if is_hover else " "
            checkbox = "[\033[1;32mX\033[0m]" if is_checked else "[ ]"
            style = "\033[1;37m" if is_hover else ""
            print(f"{cursor} {checkbox} {style}{item['human_size']:>8} | {item['project']:<15} | {item['path'].name}{RESET}")
        print("-" * 60)
        total_pages = (len(self.items) + self.page_size - 1) // self.page_size
        print(f" Page {self.current_page + 1}/{total_pages} | Items: {len(self.items)} | Selected: {len(self.selected_items)}")
        print(f"{GRAY} ↑/↓: Move | Space: Select | A: All | P: Purge | M: Edit Paths | Q: Back{RESET}")

    def run(self):
        Navigator.hide_cursor()
        try:
            while True:
                self.render()
                key = Navigator.get_key()
                if key == Navigator.UP:
                    if self.selected_index > 0:
                        self.selected_index -= 1
                        if self.selected_index < self.current_page * self.page_size: self.current_page -= 1
                elif key == Navigator.DOWN:
                    if self.selected_index < len(self.items) - 1:
                        self.selected_index += 1
                        if self.selected_index >= (self.current_page + 1) * self.page_size: self.current_page += 1
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
                elif key.lower() == 'q': return []
        finally: Navigator.show_cursor()

class UninstallSelector:
    def __init__(self, title, items):
        self.title = title
        self.items = items
        self.selected_index = 0
        self.selected_ids = set() # Store unique IDs instead of indices
        self.sort_key = 'size_bytes'
        self.sort_reverse = True
        self.page_size = 10
        self.current_page = 0
        self._sort_items()

    def _format_time_ago(self, timestamp):
        if timestamp == 0: return "Unknown"
        diff = time.time() - timestamp
        if diff < 86400: return "Today"
        if diff < 172800: return "Yesterday"
        if diff < 604800: return f"{int(diff/86400)}d ago"
        if diff < 2592000: return f"{int(diff/604800)}w ago"
        if diff < 31536000: return f"{int(diff/2592000)}m ago"
        return f"{int(diff/31536000)}y ago"

    def _sort_items(self):
        # We no longer clear selections when sorting!
        if self.sort_key == 'name':
            self.items.sort(key=lambda x: x['name'].lower(), reverse=not self.sort_reverse)
        else:
            self.items.sort(key=lambda x: x[self.sort_key], reverse=self.sort_reverse)

    def render(self):
        os.system('clear')
        print(f"\n \033[1;36m{self.title}\033[0m")
        print("-" * 80)
        start = self.current_page * self.page_size
        end = min(start + self.page_size, len(self.items))
        for i in range(start, end):
            item = self.items[i]
            is_hover = i == self.selected_index
            is_selected = item['id'] in self.selected_ids
            
            # Numeric Hotkey UI
            page_idx = (i - start) + 1
            num_key = "0" if page_idx == 10 else str(page_idx)
            
            cursor = "\033[1;36m▶\033[0m" if is_hover else " "
            checkbox = "[\033[1;32mX\033[0m]" if is_selected else f"[{num_key}]"
            
            # Name Styling: Bold Magenta if selected, otherwise Cyan if hovered, else default
            if is_selected:
                name_style = "\033[1;35m" # Bold Magenta
            elif is_hover:
                name_style = "\033[1;36m" # Cyan
            else:
                name_style = ""
            
            time_str = self._format_time_ago(item['install_time'])
            print(f"{cursor} {checkbox} {name_style}{item['name']:<35}{RESET} {item['size_str']:>10} | {time_str}")
        
        print("-" * 80)
        total_pages = (len(self.items) + self.page_size - 1) // self.page_size
        order_icon = "↑" if not self.sort_reverse else "↓"
        print(f" Page {self.current_page + 1}/{total_pages} | {GRAY}Keys 1-0: Select | ↑↓: Move | Space: Select | Enter: Confirm{RESET}")
        print(f"{GRAY} S: Size | N: Name | T: Time | O: {order_icon} | Q: Exit{RESET}")
        
        # Selection summary (persists across sorts/pages)
        if self.selected_ids:
            print(f"\n \033[1;35m☉ Selected Apps to Remove:\033[0m")
            # Show actual items based on their saved IDs
            for item in self.items:
                if item['id'] in self.selected_ids:
                    print(f"   \033[1;35m•\033[0m {item['name']}")

    def run(self):
        if not self.items: return []
        Navigator.hide_cursor()
        try:
            while True:
                self.render()
                key = Navigator.get_key()
                if key == Navigator.UP:
                    if self.selected_index > 0:
                        self.selected_index -= 1
                        if self.selected_index < self.current_page * self.page_size: self.current_page -= 1
                elif key == Navigator.DOWN:
                    if self.selected_index < len(self.items) - 1:
                        self.selected_index += 1
                        if self.selected_index >= (self.current_page + 1) * self.page_size: self.current_page += 1
                elif key == Navigator.SPACE:
                    item_id = self.items[self.selected_index]['id']
                    if item_id in self.selected_ids: self.selected_ids.remove(item_id)
                    else: self.selected_ids.add(item_id)
                elif key.isdigit():
                    num = int(key)
                    page_offset = 9 if num == 0 else num - 1
                    idx = self.current_page * self.page_size + page_offset
                    if idx < len(self.items):
                        item_id = self.items[idx]['id']
                        if item_id in self.selected_ids: self.selected_ids.remove(item_id)
                        else: self.selected_ids.add(item_id)
                elif key.lower() == 's': self.sort_key = 'size_bytes'; self.sort_reverse = not self.sort_reverse; self._sort_items()
                elif key.lower() == 'n': self.sort_key = 'name'; self.sort_reverse = not self.sort_reverse; self._sort_items()
                elif key.lower() == 't': self.sort_key = 'install_time'; self.sort_reverse = not self.sort_reverse; self._sort_items()
                elif key.lower() == 'o': self.sort_reverse = not self.sort_reverse; self._sort_items()
                elif key == Navigator.ENTER:
                    if not self.selected_ids: 
                        return [self.selected_index]
                    # Map IDs back to indices for the manager to process
                    return [i for i, item in enumerate(self.items) if item['id'] in self.selected_ids]
                elif key.lower() == 'q': return []
        finally: Navigator.show_cursor()

class ConfirmSelector:
    def __init__(self, message):
        self.message = message
        self.options = ["Yes", "No"]
        self.selected_index = 1 # Default to No for safety

    def render(self):
        # Print message and the two buttons
        print(f"\n  {BOLD}{self.message}{RESET}")
        
        btns = []
        for i, opt in enumerate(self.options):
            if i == self.selected_index:
                # Highlighted selection: White text on Magenta background
                btns.append(f"\033[1;37m\033[45m {opt} \033[0m")
            else:
                btns.append(f"  {GRAY}{opt}{RESET}  ")
        
        print("  " + "   ".join(btns))
        print(f"{GRAY}   ←/→: Select | Enter: Confirm | Y/N: Quick Keys{RESET}")

    def run(self):
        Navigator.hide_cursor()
        try:
            while True:
                self.render()
                key = Navigator.get_key()
                if key in (Navigator.LEFT, Navigator.RIGHT):
                    self.selected_index = 1 - self.selected_index
                elif key.lower() == 'y': return True
                elif key.lower() == 'n': return False
                elif key == Navigator.ENTER:
                    return self.selected_index == 0
                
                # ANSI move up 4 lines and clear to re-render local prompt without flickering
                print("\033[4A\033[J", end="")
        finally: 
            Navigator.show_cursor()
            print()

class CleanSelector:
    def __init__(self, title, items):
        self.title = title
        self.items = items
        self.selected_index = 0
        self.selected_items = set(range(len(items))) # Default: Select all

    def render(self):
        os.system('clear')
        print(f"\n \033[1;36m{self.title}\033[0m")
        print("-" * 65)
        
        from ..core.file_ops import bytes_to_human
        
        total_freed = 0
        for i, item in enumerate(self.items):
            is_hover = i == self.selected_index
            is_checked = i in self.selected_items
            if is_checked: total_freed += item['size']
            
            cursor = "\033[1;36m▶\033[0m" if is_hover else " "
            checkbox = "[\033[1;32m✓\033[0m]" if is_checked else "[ ]"
            style = "\033[1;36m" if is_hover else ""
            
            size_str = bytes_to_human(item['size']) if item['size'] > 0 else "Scan Result"
            print(f"{cursor} {checkbox} {style}{item['name']:<25}{RESET} | {size_str:>10} | {GRAY}{item['desc']}{RESET}")
        
        print("-" * 65)
        print(f" Total Selected: \033[1;32m{bytes_to_human(total_freed)}\033[0m")
        print(f"\n{GRAY} ↑/↓: Move | Space: Toggle | Enter: Clean Selected | Q: Cancel{RESET}")

    def run(self):
        Navigator.hide_cursor()
        try:
            while True:
                self.render()
                key = Navigator.get_key()
                if key == Navigator.UP: self.selected_index = (self.selected_index - 1) % len(self.items)
                elif key == Navigator.DOWN: self.selected_index = (self.selected_index + 1) % len(self.items)
                elif key == Navigator.SPACE:
                    if self.selected_index in self.selected_items: self.selected_items.remove(self.selected_index)
                    else: self.selected_items.add(self.selected_index)
                elif key == Navigator.ENTER:
                    if not self.selected_items: continue
                    return list(self.selected_items)
                elif key.lower() == 'q': return []
        finally: Navigator.show_cursor()

