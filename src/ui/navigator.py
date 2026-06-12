import os
import re
import select
import shutil
import subprocess
import sys
import termios
import time
import tty
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from ..core import terminal_state
from ..core.config import get_show_scrollbar
from ..core.constants import BOLD, GRAY, GREEN, PURPLE, RED, RESET, THEME_TITLE, WHITE, YELLOW
from ..core.file_ops import bytes_to_human

ANSI_CSI_RE = re.compile("\x1b\\[[0-?]*[ -/]*[@-~]")
SGR_MOUSE_RE = re.compile("\x1b\\[<(?P<button>\\d+);(?P<x>\\d+);(?P<y>\\d+)(?P<final>[mM])")


@dataclass(frozen=True)
class MouseEvent:
    action: str
    button: int
    x: int
    y: int


@dataclass(frozen=True)
class FrameState:
    width: int
    height: int
    total_lines: int
    top: int
    scrollable: bool
    scrollbar_visible: bool
    thumb_top: int = 0
    thumb_height: int = 0


def get_terminal_width():
    try:
        return os.get_terminal_size().columns
    except OSError:
        return 80


def _char_width(char):
    if unicodedata.combining(char):
        return 0
    if unicodedata.east_asian_width(char) in ("W", "F"):
        return 2
    return 1


def _strip_frame_controls(text):
    return (
        text.replace("\033[2J", "")
        .replace("\033[H", "")
        .replace("\033[J", "")
        .replace("\033[K", "")
    )


def _frame_line_count(parts):
    text = _strip_frame_controls("".join(parts))
    if not text:
        return 0
    lines = text.count("\n")
    return lines if text.endswith("\n") else lines + 1


def _frame_lines(parts):
    text = _strip_frame_controls("".join(parts))
    lines = text.split("\n")
    while lines and lines[-1] == "":
        lines.pop()
    return lines or [""]


def _fit_ansi_line(line, width):
    if width <= 0:
        return ""

    out = []
    used = 0
    i = 0
    truncated = False
    while i < len(line):
        match = ANSI_CSI_RE.match(line, i)
        if match:
            out.append(match.group(0))
            i = match.end()
            continue

        char = line[i]
        char_w = _char_width(char)
        if used + char_w > width:
            truncated = True
            break

        out.append(char)
        used += char_w
        i += 1

    if truncated:
        out.append(RESET)
    return "".join(out)


def _viewport_top_for_focus(focus_line, total_lines, height):
    if total_lines <= height:
        return 0

    max_top = total_lines - height
    focus_line = max(0, min(focus_line, total_lines - 1))
    return max(0, min(focus_line - height // 2, max_top))


def _write_scrollable_frame(parts, focus_line=None, scroll_top=None):
    """Render a virtual full-screen frame with a right-edge scrollbar."""
    size = shutil.get_terminal_size(fallback=(80, 24))
    width = max(1, size.columns)
    height = max(1, size.lines)

    lines = _frame_lines(parts)
    total_lines = len(lines)
    scrollable = total_lines > height
    scrollbar_visible = scrollable and width > 1 and get_show_scrollbar()
    content_width = max(0, width - 1) if scrollbar_visible else width
    focus = 0 if focus_line is None else focus_line
    max_top = max(0, total_lines - height)
    if scroll_top is None:
        top = _viewport_top_for_focus(focus, total_lines, height)
    else:
        top = max(0, min(scroll_top, max_top))

    out = ["\033[H"]
    for row in range(height):
        idx = top + row
        line = _fit_ansi_line(lines[idx], content_width) if idx < total_lines else ""
        out.append(f"\033[{row + 1};1H{line}\033[K")

    thumb_top = 0
    thumb_height = 0
    if scrollbar_visible:
        thumb_height = max(1, min(height, round(height * height / total_lines)))
        thumb_top = round(top * (height - thumb_height) / max_top) if max_top else 0
        for row in range(height):
            is_thumb = thumb_top <= row < thumb_top + thumb_height
            char = "▐" if is_thumb else " "
            color = GRAY if is_thumb else ""
            out.append(f"\033[{row + 1};{width}H{RESET}{color}{char}{RESET}")

    # Clear any potential artifacts below the current frame (important on resize)
    out.append(f"\033[{height};{width}H\033[J")
    out.append(RESET)
    sys.stdout.write("".join(out))
    sys.stdout.flush()
    return FrameState(
        width, height, total_lines, top, scrollable, scrollbar_visible, thumb_top, thumb_height
    )


def _render_scrollable_frame(owner, parts, focus_line=None):
    scroll_top = getattr(owner, "_scroll_top", None)
    state = _write_scrollable_frame(parts, focus_line, scroll_top)
    owner._frame_state = state
    if not state.scrollable:
        owner._scroll_top = None
        owner._scrollbar_dragging = False
    elif scroll_top is not None:
        owner._scroll_top = state.top
    if not state.scrollbar_visible:
        owner._scrollbar_dragging = False
    return state


def _clear_manual_scroll(owner):
    owner._scroll_top = None
    owner._scrollbar_dragging = False
    owner._scrollbar_drag_offset = 0


def _scroll_top_from_mouse_y(state, y, offset=0):
    max_top = state.total_lines - state.height
    track_range = state.height - state.thumb_height
    if max_top <= 0 or track_range <= 0:
        return 0

    thumb_row = max(0, min((y - 1) - offset, track_range))
    return round(thumb_row * max_top / track_range)


def _handle_scrollbar_mouse(owner, event):
    state = getattr(owner, "_frame_state", None)
    if state is None or not state.scrollable:
        return False

    if event.action == "wheel_up":
        max_top = state.total_lines - state.height
        current_top = getattr(owner, "_scroll_top", None)
        if current_top is None:
            current_top = state.top
        owner._scroll_top = max(0, min(current_top - 3, max_top))
        return True
    if event.action == "wheel_down":
        max_top = state.total_lines - state.height
        current_top = getattr(owner, "_scroll_top", None)
        if current_top is None:
            current_top = state.top
        owner._scroll_top = max(0, min(current_top + 3, max_top))
        return True

    dragging = getattr(owner, "_scrollbar_dragging", False)
    if event.action == "release":
        owner._scrollbar_dragging = False
        return dragging

    if not state.scrollbar_visible:
        return False

    if event.action == "press":
        if event.button != 0 or event.x != state.width:
            return False
        thumb_start = state.thumb_top + 1
        thumb_end = thumb_start + state.thumb_height - 1
        if thumb_start <= event.y <= thumb_end:
            owner._scrollbar_drag_offset = event.y - thumb_start
        else:
            owner._scrollbar_drag_offset = state.thumb_height // 2
        owner._scrollbar_dragging = True
        owner._scroll_top = _scroll_top_from_mouse_y(state, event.y, owner._scrollbar_drag_offset)
        return True

    if event.action == "drag" and dragging:
        owner._scroll_top = _scroll_top_from_mouse_y(
            state, event.y, getattr(owner, "_scrollbar_drag_offset", 0)
        )
        return True

    return False


def _move_selection_with_wheel(owner, event, step=1):
    if event.action not in ("wheel_up", "wheel_down"):
        return False

    delta = -step if event.action == "wheel_up" else step

    if hasattr(owner, "_move_cursor"):
        items = getattr(owner, "items", None)
        if items:
            _clear_manual_scroll(owner)
            owner._move_cursor(delta)
            return True

    options = getattr(owner, "options", None)
    if options and hasattr(owner, "selected_index"):
        _clear_manual_scroll(owner)
        owner.selected_index = (owner.selected_index + delta) % len(options)
        Navigator.play_click()
        return True

    items = getattr(owner, "items", None)
    if items and hasattr(owner, "selected_index"):
        _clear_manual_scroll(owner)
        owner.selected_index = (owner.selected_index + delta) % len(items)
        Navigator.play_click()
        return True

    return False


def _consume_mouse(owner, key):
    if isinstance(key, MouseEvent):
        if _move_selection_with_wheel(owner, key):
            return True
        _handle_scrollbar_mouse(owner, key)
        return True
    return key == "MOUSE_EVENT"


def pad_and_truncate(text, width):
    """Pads or truncates text to fit a specific width, accounting for CJK characters."""
    actual_w = 0
    for char in text:
        actual_w += _char_width(char)

    if actual_w > width:
        curr_w = 0
        res = ""
        for char in text:
            char_w = _char_width(char)
            if curr_w + char_w + 3 > width:
                res += "..."
                break
            res += char
            curr_w += char_w
        return res + " " * (width - curr_w - 3 if width > curr_w + 3 else 0)
    else:
        return text + " " * (width - actual_w)


def get_color_for_percent(percent):
    """Returns the ANSI color code for a given percentage."""
    if percent > 80:
        return RED
    if percent > 50:
        return YELLOW
    return GREEN


def draw_bar(percent, width=20, force_color=None):
    """Draws a sleek progress bar using the '▬' character."""
    if width <= 0:
        return ""
    # Ensure even small percentages show at least one block to distinguish from 0%
    filled = int((percent / 100) * width)
    if percent > 0 and filled == 0:
        filled = 1
    empty = width - filled

    color = force_color or get_color_for_percent(percent)

    if percent <= 0:
        # For 0%, show a consistent gray empty bar
        return f"{GRAY}{'▬' * width}{RESET}"

    # For >0%, show colored filled part and gray empty part
    return f"{color}{'▬' * filled}{RESET}{GRAY}{'▬' * empty}{RESET}"


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
    MOUSE_DISABLE = terminal_state.MOUSE_DISABLE
    MOUSE_ENABLE = terminal_state.MOUSE_ENABLE
    _last_size = None
    is_muted = False

    @staticmethod
    @contextmanager
    def raw_mode(enable_mouse=False):
        """Context manager to put the terminal into cbreak mode and restore it later."""
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        terminal_state.remember_raw_state(fd, old_settings)
        try:
            tty.setcbreak(fd)
            terminal_state.disable_mouse_tracking()
            if enable_mouse:
                terminal_state.enable_mouse_tracking()
            yield fd
        finally:
            if enable_mouse:
                terminal_state.disable_mouse_tracking()
            terminal_state.restore_raw_state(fd, old_settings)

    @staticmethod
    def get_key(fd=None):
        """Reads a key or escape sequence. If fd is provided, assumes already in raw mode."""
        if fd is None:
            with Navigator.raw_mode() as raw_fd:
                return Navigator._read_key(raw_fd)
        return Navigator._read_key(fd)

    @staticmethod
    def _read_key(fd):
        if Navigator._last_size is None:
            Navigator._last_size = shutil.get_terminal_size()

        try:
            while True:
                # Poll with small timeout. SIGWINCH will likely interrupt this.
                try:
                    r, _, _ = select.select([fd], [], [], 0.05)
                except (InterruptedError, OSError):
                    r = False

                # Detect terminal resize
                new_size = shutil.get_terminal_size()
                if new_size != Navigator._last_size:
                    Navigator._last_size = new_size
                    return "RESIZE"

                if r:
                    # Read first character using raw FD
                    ch = os.read(fd, 1).decode("utf-8", "ignore")
                    break

            if ch == "\x1b" and select.select([fd], [], [], 0.05)[0]:
                ch += os.read(fd, 1).decode("utf-8", "ignore")

                if ch == "\x1b[" and select.select([fd], [], [], 0.05)[0]:
                    next_ch = os.read(fd, 1).decode("utf-8", "ignore")
                    ch += next_ch

                    if next_ch == "M":
                        # Standard X11 Mouse tracking
                        for _ in range(3):
                            ch += os.read(fd, 1).decode("utf-8", "ignore")
                        return Navigator._parse_x10_mouse(ch) or "MOUSE_EVENT"

                    if next_ch == "<":
                        # SGR Mouse tracking
                        while True:
                            last = os.read(fd, 1).decode("utf-8", "ignore")
                            ch += last
                            if last in ("m", "M") or len(ch) > 25:
                                break
                        return Navigator._parse_sgr_mouse(ch) or "MOUSE_EVENT"

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
    def _parse_mouse_code(code, x, y, final):
        button = code & 0b11
        if final == "m":
            return MouseEvent("release", button, x, y)
        if code & 64:
            action = "wheel_down" if button == 1 else "wheel_up"
            return MouseEvent(action, button, x, y)
        if code & 32:
            return MouseEvent("drag", button, x, y)
        return MouseEvent("press", button, x, y)

    @staticmethod
    def _parse_sgr_mouse(sequence):
        match = SGR_MOUSE_RE.fullmatch(sequence)
        if not match:
            return None
        return Navigator._parse_mouse_code(
            int(match.group("button")),
            int(match.group("x")),
            int(match.group("y")),
            match.group("final"),
        )

    @staticmethod
    def _parse_x10_mouse(sequence):
        if len(sequence) < 6:
            return None
        code = max(0, ord(sequence[3]) - 32)
        x = max(1, ord(sequence[4]) - 32)
        y = max(1, ord(sequence[5]) - 32)
        return Navigator._parse_mouse_code(code, x, y, "M")

    @staticmethod
    def wait_for_return(message="Press Enter to return to Main Menu, ESC to exit..."):
        """Standardized non-blocking return/exit prompt."""
        print(f"\n\033[1;90m{message} \033[0m", end="", flush=True)
        while True:
            key = Navigator.get_key()
            if key in Navigator.ENTER:
                print()
                return True
            if key == Navigator.ESC and len(key) == 1:
                print()
                return False

    @staticmethod
    def read_number(fd, first_digit):
        """Reads a quickly-typed multi-digit number starting with first_digit."""
        num_str = first_digit
        while select.select([fd], [], [], 0.4)[0]:
            ch = os.read(fd, 1).decode("utf-8", "ignore")
            if ch.isdigit():
                num_str += ch
            else:
                break
        return num_str

    @staticmethod
    def _get_sound_player(sound_name):
        """Resolves the player and path for a named sound (e.g. 'click', 'delete')."""
        if not hasattr(Navigator, "_sound_configs"):
            Navigator._sound_configs = {}

        if sound_name not in Navigator._sound_configs:
            from ..core.paths import get_config_dir

            # 1. Check for user override
            config_sound = get_config_dir() / "sounds" / f"{sound_name}.wav"
            # 2. Check for bundled asset (default)
            project_root = Path(__file__).parent.parent.parent
            asset_map = {"click": "cli_click.wav", "delete": "delete_remove.wav"}
            bundled_sound = project_root / "assets" / asset_map.get(sound_name, "")

            target_sound = None
            if config_sound.exists():
                target_sound = config_sound
            elif bundled_sound.exists():
                target_sound = bundled_sound

            player = None
            if target_sound:
                if shutil.which("pw-play"):
                    player = ["pw-play", str(target_sound)]
                elif shutil.which("paplay"):
                    player = ["paplay", str(target_sound)]
                elif shutil.which("aplay"):
                    player = ["aplay", str(target_sound)]

            if not player:
                player = "bell"

            Navigator._sound_configs[sound_name] = player

        return Navigator._sound_configs[sound_name]

    @staticmethod
    def play_click():
        """Plays a subtle navigation sound."""
        if Navigator.is_muted:
            return
        player = Navigator._get_sound_player("click")
        if player == "bell":
            sys.stdout.write("\a")
            sys.stdout.flush()
        else:
            try:
                subprocess.Popen(player, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                sys.stdout.write("\a")
                sys.stdout.flush()

    @staticmethod
    def play_delete():
        """Plays a distinct sound for deletion or uninstallation."""
        if Navigator.is_muted:
            return
        player = Navigator._get_sound_player("delete")
        if player == "bell":
            # For delete, we can play bell twice for a different feel if no wav
            sys.stdout.write("\a\a")
            sys.stdout.flush()
        else:
            try:
                subprocess.Popen(player, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                sys.stdout.write("\a\a")
                sys.stdout.flush()

    @staticmethod
    def hide_cursor():
        terminal_state.hide_cursor()

    @staticmethod
    def show_cursor():
        terminal_state.show_cursor()


@contextmanager
def _selector_session(enable_mouse=True):
    """Shared full-screen scaffolding: hide cursor, clear, raw mode, then restore."""
    Navigator.hide_cursor()
    sys.stdout.write("\033[2J")
    try:
        with Navigator.raw_mode(enable_mouse=enable_mouse) as fd:
            yield fd
    finally:
        Navigator.show_cursor()


class _PagedSelector:
    """Mixin with page math and cursor movement shared by paginated selectors.

    Subclasses provide ``self.items``, ``self.selected_index`` and
    ``self.current_page``. ``page_size`` defaults to 15 and may be overridden
    per-instance.
    """

    page_size = 15

    def _total_pages(self):
        return max(1, (len(self.items) + self.page_size - 1) // self.page_size)

    def _page_bounds(self):
        start = self.current_page * self.page_size
        return start, min(start + self.page_size, len(self.items))

    def _move_cursor(self, delta):
        n = len(self.items)
        if n:
            self.selected_index = (self.selected_index + delta) % n
            self.current_page = self.selected_index // self.page_size
            Navigator.play_click()

    def _flip_page(self, delta):
        if self.items:
            self.current_page = (self.current_page + delta) % self._total_pages()
            self.selected_index = self.current_page * self.page_size

    def _toggle_index_selection(self, idx):
        if idx in self.selected_items:
            self.selected_items.remove(idx)
        else:
            self.selected_items.add(idx)

    def _toggle_current_page_selection(self):
        start, end = self._page_bounds()
        page_indices = set(range(start, end))
        if page_indices.issubset(self.selected_items):
            self.selected_items -= page_indices
        else:
            self.selected_items |= page_indices


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

        buf.append(f"\n {THEME_TITLE}{self.title}{RESET}\033[K\n")
        buf.append("\033[K\n")
        focus_line = 0
        for i, (label, desc) in enumerate(self.options):
            prefix = " \033[1;36m>\033[0m " if i == self.selected_index else "   "
            if i == self.selected_index:
                focus_line = _frame_line_count(buf)
                buf.append(f"{prefix}\033[1;36m{label:<15} {desc}{RESET}\033[K\n")
            else:
                buf.append(f"{prefix}{label:<15} {desc}\033[K\n")
        buf.append("\033[K\n")
        mute_label = "Unmute" if Navigator.is_muted else "Mute"
        buf.append(f"{GRAY} ↑/↓ | M: {mute_label} | Enter: Select | ESC: Quit{RESET}\033[K\n")
        buf.append("\033[J")
        _render_scrollable_frame(self, buf, focus_line)

    def run(self):
        if not self.options:
            return None
        with _selector_session(enable_mouse=True) as fd:
            while True:
                self.render()
                key = Navigator.get_key(fd)
                if _consume_mouse(self, key):
                    continue
                _clear_manual_scroll(self)
                if key in (Navigator.UP, "\x1bOA"):
                    self.selected_index = (self.selected_index - 1) % len(self.options)
                    Navigator.play_click()
                elif key in (Navigator.DOWN, "\x1bOB"):
                    self.selected_index = (self.selected_index + 1) % len(self.options)
                    Navigator.play_click()
                elif len(key) == 1 and key.lower() == "m":
                    Navigator.is_muted = not Navigator.is_muted
                elif key in (Navigator.LEFT, Navigator.RIGHT, "\x1bOC", "\x1bOD"):
                    continue
                elif key in Navigator.ENTER:
                    return self.selected_index
                elif key == Navigator.ESC and len(key) == 1:
                    return None
                elif key == "MOUSE_EVENT":
                    continue


class AnalyzeSelector(_PagedSelector):
    def __init__(
        self, title, items, show_banner=None, can_select=True, notice="", sort_mode="size"
    ):
        self.title = title
        self.items = items
        self.selected_index = 0
        self.selected_items = set()
        self.sort_mode = sort_mode
        self.sort_reverse = sort_mode == "size"
        self.show_banner = show_banner
        self.can_select = can_select
        self.notice = notice
        self.page_size = 15
        self.current_page = 0
        self.confirming_delete = False
        self.confirm_text = ""
        self._sort_items()

    def _sort_items(self):
        self.selected_items.clear()
        self.confirming_delete = False
        if self.sort_mode == "name":
            self.items.sort(key=lambda x: x["name"].lower(), reverse=self.sort_reverse)
            self.items.sort(key=lambda x: x.get("sort_group", 1))
        else:
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

        buf.append(f"\n {THEME_TITLE}{self.title}{RESET}\033[K\n\n")

        total_disk = bytes_to_human(shutil.disk_usage("/").total)
        hint = (
            f"{GRAY}Select a location to explore (Type numbers or Space to select):{RESET}"
            if self.can_select
            else f"{GRAY}Select a category to explore (Total: {total_disk}):{RESET}"
        )
        buf.append(f"{hint}\033[K\n")
        if self.notice:
            buf.append(f"{YELLOW}⚠ {self.notice}{RESET}\033[K\n")
        buf.append("\033[K\n")

        columns = shutil.get_terminal_size().columns
        available = columns - (2 + 5 + 8 + 3 + 2 + 5 + 12 + 5)
        bar_w = 20 if available > 40 else 10 if available > 20 else 0
        name_w = min(30, available - bar_w) if bar_w > 0 else max(15, available)

        total_len = len(self.items)
        total_pages = max(1, (total_len + self.page_size - 1) // self.page_size)
        self.current_page = max(0, min(self.current_page, total_pages - 1))
        start = self.current_page * self.page_size
        end = min(start + self.page_size, total_len)
        focus_line = 0

        if total_len == 0:
            focus_line = _frame_line_count(buf)
            buf.append(f"   {GRAY}No items found{RESET}\033[K\n")
        else:
            for i in range(start, end):
                item = self.items[i]
                is_hover = i == self.selected_index
                is_selected = i in self.selected_items
                cursor = "\033[1;36m▶\033[0m" if is_hover else " "
                if self.can_select:
                    num = (i - start) + 1
                    num_str = f" {num}" if num < 10 else str(num)
                    checkbox_str = (
                        f"\033[1;32m✓ {num_str}.\033[0m "
                        if is_selected
                        else f"{GRAY}○{RESET} {num_str}."
                    )
                    checkbox_str = f"{checkbox_str} "
                else:
                    checkbox_str = f" {i + 1:2}. "

                size_known = item.get("size_known", True)
                if size_known:
                    bar = draw_bar(item["percent"], width=bar_w)
                    percent_str = f"{item['percent']:>5.1f}%"
                    size_str = bytes_to_human(item["size"])
                else:
                    bar = f"{GRAY}{' ' * bar_w}{RESET}" if bar_w > 0 else ""
                    percent_str = "   --"
                    size_str = "--"
                bar_str = f"{bar}  " if bar_w > 0 else ""
                style = "\033[1;35m" if is_hover else ""
                name_padded = pad_and_truncate(item["name"], name_w)
                icon = item.get("icon", "🗂️")
                icon_gap = "  " if icon == "🗂️" else " "
                if is_hover:
                    focus_line = _frame_line_count(buf)
                buf.append(
                    f"{cursor} {checkbox_str}{RESET}{bar_str}{percent_str}  {icon}{icon_gap}{style}{name_padded}{RESET} | {style}{size_str:>10}{RESET}\033[K\n"
                )

        order_icon = "↓" if self.sort_reverse else "↑"
        page_info = f" Page {self.current_page + 1}/{total_pages} |" if total_pages > 1 else ""

        if self.can_select:
            prompts = [
                f" {page_info} ↑↓←→ | PgUp/PgDn:Page | A:All | F:Open Folder | R:Reload | S:Sort {order_icon} | Space:Select"
            ]
        else:
            prompts = [f" {page_info} ↑↓→ | F:Open Folder | R:Reload | S:Sort {order_icon}"]

        buf.append("\n\033[K\n")
        for p in prompts:
            buf.append(f"\033[1;90m{p}\033[0m\033[K\n")

        if self.selected_items:
            buf.append(
                f"\n {THEME_TITLE}☉ Selected Items to Remove:{RESET} {GRAY}Enter:Delete{RESET}\033[K\n"
            )
            selected_indices = sorted(list(self.selected_items))
            for i in range(0, len(selected_indices), 2):
                pair = selected_indices[i : i + 2]
                line = ""
                for idx in pair:
                    item = self.items[idx]
                    icon = item.get("icon", "🗂️")
                    icon_gap = "  " if icon == "🗂️" else " "
                    name_padded = pad_and_truncate(item["name"], 35)
                    line += f"   {THEME_TITLE}•{RESET} {icon}{icon_gap}{name_padded}"
                buf.append(line + "\033[K\n")

        if self.confirming_delete:
            buf.append(f"\n {self.confirm_text}\033[K\n")

        buf.append("\033[J")  # Clear remaining
        _render_scrollable_frame(self, buf, focus_line)

    def run(self):
        with _selector_session() as fd:
            while True:
                self.render()
                key = Navigator.get_key(fd)
                if _consume_mouse(self, key):
                    continue
                _clear_manual_scroll(self)

                if self.confirming_delete:
                    if key in Navigator.ENTER:
                        self.confirming_delete = False
                        return "DELETE_BATCH", list(self.selected_items)
                    if key == Navigator.SPACE or key == Navigator.ESC or len(key) > 1:
                        self.confirming_delete = False
                        continue
                    continue

                total_len = len(self.items)
                if key in (Navigator.UP, "\x1bOA"):
                    self._move_cursor(-1)
                elif key in (Navigator.DOWN, "\x1bOB"):
                    self._move_cursor(1)
                elif key == Navigator.PGUP:
                    self._flip_page(-1)
                elif key == Navigator.PGDN:
                    self._flip_page(1)
                elif key in (Navigator.LEFT, "\x1bOD"):
                    if self.current_page == 0:
                        return "BACK", None
                    if self._total_pages() > 1:
                        self._flip_page(-1)
                    else:
                        return "BACK", None
                elif key in (Navigator.RIGHT, "\x1bOC"):
                    if total_len == 0:
                        continue
                    if self.items[self.selected_index]["path"].is_dir():
                        return "DRILL_DOWN", self.selected_index
                    elif self._total_pages() > 1:
                        self._flip_page(1)
                elif key == Navigator.SPACE and self.can_select:
                    self._toggle_index_selection(self.selected_index)
                elif key.isdigit() and self.can_select:
                    num_str = Navigator.read_number(fd, key)
                    try:
                        num = int(num_str)
                        page_offset = 9 if num_str == "0" else num - 1
                        idx = self.current_page * self.page_size + page_offset
                        if idx < total_len:
                            self._toggle_index_selection(idx)
                    except Exception:
                        pass
                elif key in Navigator.ENTER:
                    if total_len == 0:
                        continue
                    if self.can_select and self.selected_items:
                        self.confirming_delete = True
                        count = len(self.selected_items)
                        selected = [self.items[i] for i in self.selected_items]
                        total_size = sum(
                            item["size"] for item in selected if item.get("size_known", True)
                        )
                        unknown_count = sum(
                            1 for item in selected if not item.get("size_known", True)
                        )
                        item_text = "item" if count == 1 else "items"
                        size_text = bytes_to_human(total_size)
                        if unknown_count:
                            size_text = f"{size_text} known, {unknown_count} uncalculated"
                        self.confirm_text = (
                            f"{PURPLE}➔{RESET} Delete {count} {item_text}, "
                            f"{size_text}  "
                            f"{GREEN}Enter{RESET} confirm, {GRAY}Space{RESET} cancel:"
                        )
                        continue
                    return "DRILL_DOWN", self.selected_index
                elif len(key) == 1 and key.lower() == "s":
                    self.sort_reverse = not self.sort_reverse
                    self._sort_items()
                elif len(key) == 1 and key.lower() == "r":
                    return "REFRESH", None
                elif len(key) == 1 and key.lower() == "f":
                    if total_len == 0:
                        continue
                    if self.can_select:
                        if self.selected_items:
                            return "OPEN_BATCH", list(self.selected_items)
                        return "OPEN", self.selected_index
                    else:
                        return "DRILL_DOWN", self.selected_index
                elif len(key) == 1 and key.lower() == "a" and self.can_select:
                    self._toggle_current_page_selection()
                elif key == Navigator.ESC and len(key) == 1:
                    return "QUIT", None
                elif key == "MOUSE_EVENT":
                    continue


class PaginatedSelector(_PagedSelector):
    def __init__(self, title, items, page_size=10):
        self.title = title
        self.items = items
        self.page_size = page_size
        self.current_page = 0
        self.selected_index = 0
        self.selected_items = set()

    def render(self):
        buf = ["\033[H"]
        buf.append(f"\n {THEME_TITLE}{self.title}{RESET}\033[K\n")
        buf.append("\033[K\n")
        start = self.current_page * self.page_size
        end = min(start + self.page_size, len(self.items))
        focus_line = 0
        for i in range(start, end):
            item = self.items[i]
            is_hover = i == self.selected_index
            is_checked = i in self.selected_items
            cursor = "\033[1;35m>\033[0m" if is_hover else " "
            checkbox = "[\033[1;32m✓\033[0m]" if is_checked else "[ ]"
            style = "\033[1;37m" if is_hover else ""
            name_padded = pad_and_truncate(item["project"], 20)
            size_str = bytes_to_human(item["size"])
            if is_hover:
                focus_line = _frame_line_count(buf)
            buf.append(f"{cursor} {checkbox} {style}{name_padded}{RESET} | {size_str:>10}\033[K\n")
        buf.append("\033[K\n")
        total_pages = (len(self.items) + self.page_size - 1) // self.page_size
        buf.append(
            f" Page {self.current_page + 1}/{total_pages} | {GRAY}Space: Select | A: All | Enter: Confirm | S: Manage Paths | ESC: Exit{RESET}\033[K\n"
        )
        buf.append("\033[J")
        _render_scrollable_frame(self, buf, focus_line)

    def run(self):
        if not self.items:
            return None
        with _selector_session() as fd:
            while True:
                self.render()
                key = Navigator.get_key(fd)
                if _consume_mouse(self, key):
                    continue
                _clear_manual_scroll(self)
                if key in (Navigator.UP, "\x1bOA"):
                    self._move_cursor(-1)
                elif key in (Navigator.DOWN, "\x1bOB"):
                    self._move_cursor(1)
                elif key in (Navigator.RIGHT, "\x1bOC", Navigator.PGDN):
                    if self._total_pages() > 1:
                        self._flip_page(1)
                elif key in (Navigator.LEFT, "\x1bOD", Navigator.PGUP):
                    if self._total_pages() > 1:
                        self._flip_page(-1)
                elif key == Navigator.SPACE:
                    self._toggle_index_selection(self.selected_index)
                elif len(key) == 1 and key.lower() == "a":
                    self._toggle_current_page_selection()
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


class UninstallSelector(_PagedSelector):
    def __init__(self, title, items):
        self.title = title
        self.items = items
        self.selected_index = 0
        self.selected_ids = set()
        self.sort_key = "install_time"
        self.sort_reverse = True
        self.page_size = 15
        self.current_page = 0
        self._sort_items()

    def _sort_items(self):
        if self.sort_key == "name":
            self.items.sort(key=lambda x: x.get("name", "").lower(), reverse=not self.sort_reverse)
        elif self.sort_key == "install_time":
            self.items.sort(
                key=lambda x: (x.get("install_time", 0), x.get("size_bytes", 0)),
                reverse=self.sort_reverse,
            )
        else:
            self.items.sort(key=lambda x: x.get(self.sort_key, 0), reverse=self.sort_reverse)

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
        total_len = len(self.items)
        buf.append(
            f"\n {THEME_TITLE}Select Application to Remove{RESET} "
            f"{GRAY}{len(self.selected_ids)}/{total_len} selected{RESET}\033[K\n\n"
        )

        if total_len == 0:
            focus_line = _frame_line_count(buf)
            buf.append(f"\n   {GRAY}No applications found{RESET}\033[K\n")
        else:
            total_pages = (total_len + self.page_size - 1) // self.page_size
            self.current_page = max(0, min(self.current_page, total_pages - 1))
            start = self.current_page * self.page_size
            end = min(start + self.page_size, total_len)
            focus_line = 0
            for i in range(start, end):
                item = self.items[i]
                is_hover = i == self.selected_index
                is_selected = item["id"] in self.selected_ids
                num = (i - start) + 1
                num_key = f" {num}" if num < 10 else str(num)
                cursor = "\033[1;36m▶\033[0m" if is_hover else " "
                checkbox = (
                    f"\033[1;32m✓ {num_key}.\033[0m"
                    if is_selected
                    else f"{GRAY}○{RESET} {num_key}."
                )
                name_style = "\033[1;35m" if is_selected else "\033[1;36m" if is_hover else ""
                name_padded = pad_and_truncate(item["name"], 35)
                if is_hover:
                    focus_line = _frame_line_count(buf)
                buf.append(
                    f"{cursor} {checkbox} {name_style}{name_padded}{RESET}  {name_style}{item['size_str']:>12}{RESET} | {self._format_time_ago(item['install_time'])}\033[K\n"
                )
            sort_dir = "↓" if self.sort_reverse else "↑"
            sort_labels = {
                "name": f"N: Name {sort_dir}",
                "size_bytes": f"S: Size {sort_dir}",
                "install_time": f"T: Time {sort_dir}",
            }
            sort_hint = " | ".join(
                sort_labels[key] if key == self.sort_key else sort_labels[key].rsplit(" ", 1)[0]
                for key in ("name", "size_bytes", "install_time")
            )
            buf.append(
                f"\n Page {self.current_page + 1}/{total_pages} | "
                f"{GRAY}↑↓←→ | PgUp/PgDn:Page | A: All | {sort_hint} | Space: Select{RESET}\033[K\n"
            )

        if self.selected_ids:
            buf.append(
                f"\n {THEME_TITLE}☉ Selected Apps to Remove:{RESET} "
                f"{GRAY}Press Enter to Uninstall, ESC to Exit{RESET}\033[K\n"
            )
            selected_names = [i["name"] for i in self.items if i["id"] in self.selected_ids]
            for i in range(0, len(selected_names), 2):
                pair = selected_names[i : i + 2]
                line = ""
                for name in pair:
                    line += f"   {THEME_TITLE}•{RESET} {pad_and_truncate(name, 35)}"
                buf.append(line + "\033[K\n")

        buf.append("\033[J")
        _render_scrollable_frame(self, buf, focus_line)

    def run(self):
        if not self.items:
            return []
        with _selector_session() as fd:
            while True:
                self.render()
                key = Navigator.get_key(fd)
                if _consume_mouse(self, key):
                    continue
                _clear_manual_scroll(self)
                total_len = len(self.items)
                if key in (Navigator.UP, "\x1bOA"):
                    self._move_cursor(-1)
                elif key in (Navigator.DOWN, "\x1bOB"):
                    self._move_cursor(1)
                elif key == Navigator.PGUP:
                    self._flip_page(-1)
                elif key == Navigator.PGDN:
                    self._flip_page(1)
                elif key in (Navigator.LEFT, "\x1bOD"):
                    if self.current_page == 0:
                        return []
                    if self._total_pages() > 1:
                        self._flip_page(-1)
                    else:
                        return []
                elif key in (Navigator.RIGHT, "\x1bOC"):
                    if self._total_pages() > 1:
                        self._flip_page(1)
                elif key == Navigator.SPACE and total_len > 0:
                    self._toggle_selected_id(self.selected_index)
                elif key.isdigit() and total_len > 0:
                    num_str = Navigator.read_number(fd, key)
                    try:
                        num = int(num_str)
                        page_offset = 9 if num_str == "0" else num - 1
                        idx = self.current_page * self.page_size + page_offset
                        if idx < total_len:
                            self._toggle_selected_id(idx)
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
                    start, end = self._page_bounds()
                    page_ids = {self.items[i]["id"] for i in range(start, end)}
                    if page_ids.issubset(self.selected_ids):
                        self.selected_ids -= page_ids
                    else:
                        self.selected_ids |= page_ids
                elif key in Navigator.ENTER:
                    if not self.selected_ids:
                        continue
                    return [
                        i for i, item in enumerate(self.items) if item["id"] in self.selected_ids
                    ]
                elif key == Navigator.ESC and len(key) == 1:
                    return []
                elif key == "MOUSE_EVENT":
                    continue

    def _toggle_selected_id(self, idx):
        item_id = self.items[idx]["id"]
        if item_id in self.selected_ids:
            self.selected_ids.remove(item_id)
        else:
            self.selected_ids.add(item_id)


class TopFilesSelector:
    def __init__(self, title, items):
        self.title, self.items, self.selected_index, self.selected_items = (
            title,
            items,
            0,
            set(),
        )
        self.confirming_delete = False
        self.confirm_text = ""

    def render(self):
        buf = ["\033[H"]
        buf.append(f"\n {THEME_TITLE}{self.title}{RESET}\033[K\n")
        buf.append("\033[K\n")
        viewport = 20
        start = max(0, self.selected_index - viewport // 2)
        end = min(len(self.items), start + viewport)
        focus_line = 0
        for i in range(start, end):
            item = self.items[i]
            cursor = "\033[1;36m▶\033[0m" if i == self.selected_index else " "
            checkbox = "[\033[1;32m✓\033[0m]" if i in self.selected_items else "[ ]"
            if i == self.selected_index:
                focus_line = _frame_line_count(buf)
            buf.append(
                f"{cursor} {checkbox} {WHITE}{bytes_to_human(item.get('size', item.get('size_bytes', 0))):>12}{RESET} | {str(item['path'])}\033[K\n"
            )
        buf.append("\033[K\n")
        buf.append(f"{GRAY} ↑/↓: Move | Space: Toggle | Enter: Delete | ESC: Back{RESET}\033[K\n")
        if self.selected_items:
            buf.append(f"\n {THEME_TITLE}☉ Selected Large Files to Remove:{RESET}\033[K\n")
            selected_indices = sorted(list(self.selected_items))
            for i in range(0, len(selected_indices), 2):
                pair = selected_indices[i : i + 2]
                line = ""
                for idx in pair:
                    line += f"   {THEME_TITLE}•{RESET} 📄 {Path(self.items[idx]['path']).name}"
                buf.append(line + "\033[K\n")

        if self.confirming_delete:
            buf.append(f"\n {self.confirm_text}\033[K\n")

        buf.append("\033[J")
        _render_scrollable_frame(self, buf, focus_line)

    def run(self):
        if not self.items:
            return []
        with _selector_session() as fd:
            while True:
                self.render()
                key = Navigator.get_key(fd)
                if _consume_mouse(self, key):
                    continue
                _clear_manual_scroll(self)

                if self.confirming_delete:
                    if key in Navigator.ENTER:
                        self.confirming_delete = False
                        return (
                            list(self.selected_items)
                            if self.selected_items
                            else [self.selected_index]
                        )
                    if key == Navigator.SPACE or key == Navigator.ESC or len(key) > 1:
                        self.confirming_delete = False
                        continue
                    continue

                if key in (Navigator.UP, "\x1bOA"):
                    self.selected_index = (self.selected_index - 1) % len(self.items)
                    Navigator.play_click()
                elif key in (Navigator.DOWN, "\x1bOB"):
                    self.selected_index = (self.selected_index + 1) % len(self.items)
                    Navigator.play_click()
                elif key == Navigator.SPACE:
                    if self.selected_index in self.selected_items:
                        self.selected_items.remove(self.selected_index)
                    else:
                        self.selected_items.add(self.selected_index)
                elif key in Navigator.ENTER:
                    self.confirming_delete = True
                    selected_idxs = (
                        list(self.selected_items) if self.selected_items else [self.selected_index]
                    )
                    count = len(selected_idxs)
                    total_size = sum(
                        self.items[i].get("size", self.items[i].get("size_bytes", 0))
                        for i in selected_idxs
                    )
                    item_text = "item" if count == 1 else "items"
                    self.confirm_text = (
                        f"{PURPLE}➔{RESET} Delete {count} {item_text}, "
                        f"{bytes_to_human(total_size)}  "
                        f"{GREEN}Enter{RESET} confirm, {GRAY}Space{RESET} cancel:"
                    )
                elif key == Navigator.ESC and len(key) == 1:
                    return []
                elif key == "MOUSE_EVENT":
                    continue


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
        focus_line = _frame_line_count(buf)
        buf.append(f"  {y}   {n}\033[K\n\n")
        buf.append("\033[J")
        _render_scrollable_frame(self, buf, focus_line)

    def run(self):
        with _selector_session(enable_mouse=False) as fd:
            while True:
                self.render()
                key = Navigator.get_key(fd)
                if _consume_mouse(self, key):
                    continue
                _clear_manual_scroll(self)
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
                    Navigator.play_click()
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


class CleanSelector:
    def __init__(self, title, items):
        self.title = title
        self.items = items
        self.selected_index = 0
        self.selected_items = set(range(len(items)))

    def render(self):
        buf = ["\033[H"]
        buf.append(f"\n \033[1;36m{self.title}\033[0m\033[K\n")
        buf.append("\033[K\n")
        total_freed = 0
        focus_line = 0
        for i, item in enumerate(self.items):
            is_hover = i == self.selected_index
            is_checked = i in self.selected_items
            if is_checked:
                total_freed += item["size"]
            cursor = "\033[1;36m▶\033[0m" if is_hover else " "
            checkbox = "[\033[1;32m✓\033[0m]" if is_checked else "[ ]"
            name_padded = pad_and_truncate(item["name"], 25)
            size_str = bytes_to_human(item["size"]) if item["size"] > 0 else "Scan Result"
            if is_hover:
                focus_line = _frame_line_count(buf)
            buf.append(
                f"{cursor} {checkbox} \033[1;36m{name_padded}{RESET} |     {size_str:>12} | {GRAY}{item['desc']}{RESET}\033[K\n"
            )
        buf.append("\033[K\n")
        buf.append(f" Total Selected: \033[1;32m{bytes_to_human(total_freed)}\033[0m\033[K\n")
        buf.append(
            f"\n{GRAY} ↑/↓: Move | Space: Toggle | Enter: Clean Selected | ESC: Cancel{RESET}\033[K\n"
        )
        buf.append("\033[J")
        _render_scrollable_frame(self, buf, focus_line)

    def run(self):
        if not self.items:
            return []
        with _selector_session() as fd:
            while True:
                self.render()
                key = Navigator.get_key(fd)
                if _consume_mouse(self, key):
                    continue
                _clear_manual_scroll(self)
                if key in (Navigator.UP, "\x1bOA"):
                    self.selected_index = (self.selected_index - 1) % len(self.items)
                    Navigator.play_click()
                elif key in (Navigator.DOWN, "\x1bOB"):
                    self.selected_index = (self.selected_index + 1) % len(self.items)
                    Navigator.play_click()
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
