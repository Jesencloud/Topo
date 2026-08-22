"""Notification sounds: resolve a player once, then fire and forget.

Despite being triggered from the UI, none of this is terminal interaction --
it spawns pw-play/paplay/aplay in the background and falls back to the terminal
bell when no player or sound file is available. It lived on Navigator as a set
of static methods, which meant core.analyze had to import ui.navigator just to
chime after a delete, putting a `core -> ui` edge in the module graph.

Mute is module state rather than a caller-held flag because the toggle (M in
the main menu) and the consumers are in different modules; a from-imported
boolean would copy the value and desync the moment it flipped. Read it through
is_muted() / flip it through toggle_mute() for that reason.
"""

import shutil
import subprocess
import sys
from pathlib import Path

from .paths import get_config_dir

# Resolved lazily per sound name and cached: `shutil.which` over three players
# plus two `Path.exists` calls is not something to redo on every keystroke.
_sound_configs: dict[str, str | list[str]] = {}

_muted = False

_ASSET_MAP = {"click": "cli_click.wav", "delete": "delete_remove.wav"}


def is_muted() -> bool:
    """Whether sounds are currently suppressed."""
    return _muted


def toggle_mute() -> bool:
    """Flip the mute flag and return the new state."""
    global _muted
    _muted = not _muted
    return _muted


def _get_sound_player(sound_name):
    """Resolves the player and path for a named sound (e.g. 'click', 'delete')."""
    if sound_name not in _sound_configs:
        # 1. Check for user override
        config_sound = get_config_dir() / "sounds" / f"{sound_name}.wav"
        # 2. Check for bundled asset (default)
        # This file sits at src/core/, so three parents up is the project root --
        # the same depth ui/navigator.py had when this lived there.
        project_root = Path(__file__).parent.parent.parent
        bundled_sound = project_root / "assets" / _ASSET_MAP.get(sound_name, "")

        target_sound = None
        if config_sound.exists():
            target_sound = config_sound
        elif bundled_sound.exists():
            target_sound = bundled_sound

        player: str | list[str] | None = None
        if target_sound:
            if shutil.which("pw-play"):
                player = ["pw-play", str(target_sound)]
            elif shutil.which("paplay"):
                player = ["paplay", str(target_sound)]
            elif shutil.which("aplay"):
                player = ["aplay", str(target_sound)]

        if not player:
            player = "bell"

        _sound_configs[sound_name] = player

    return _sound_configs[sound_name]


def _play_sound(sound_name: str, fallback_bells: int) -> None:
    if _muted:
        return
    player = _get_sound_player(sound_name)
    if player == "bell":
        sys.stdout.write("\a" * fallback_bells)
        sys.stdout.flush()
    else:
        try:
            subprocess.Popen(player, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            sys.stdout.write("\a" * fallback_bells)
            sys.stdout.flush()


def play_click() -> None:
    """Plays a subtle navigation sound."""
    _play_sound("click", 1)


def play_delete() -> None:
    """Plays a distinct sound for deletion or uninstallation."""
    _play_sound("delete", 2)
