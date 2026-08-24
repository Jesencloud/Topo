from types import SimpleNamespace
from unittest.mock import patch

from src.core import sound


def setup_function():
    sound._sound_configs.clear()
    sound._muted = False


def test_toggle_mute_flips_state():
    assert sound.is_muted() is False
    assert sound.toggle_mute() is True
    assert sound.is_muted() is True
    assert sound.toggle_mute() is False


def test_sound_player_prefers_user_override_and_pw_play(tmp_path):
    custom = tmp_path / ".config/topo/sounds/click.wav"
    custom.parent.mkdir(parents=True)
    custom.write_bytes(b"wav")
    with (
        patch("src.core.sound.get_config_dir", return_value=tmp_path / ".config/topo"),
        patch(
            "src.core.sound.shutil.which",
            side_effect=lambda name: "/usr/bin/pw-play" if name == "pw-play" else None,
        ),
    ):
        assert sound._get_sound_player("click") == ["pw-play", str(custom)]


def test_sound_player_uses_bundled_asset_and_player_fallbacks(tmp_path):
    bundled = tmp_path / "assets/cli_click.wav"
    bundled.parent.mkdir(parents=True)
    bundled.write_bytes(b"wav")
    with (
        patch("src.core.sound.get_config_dir", return_value=tmp_path / "config"),
        patch("src.core.sound.__file__", str(tmp_path / "src/core/sound.py")),
        patch(
            "src.core.sound.shutil.which",
            side_effect=lambda name: "/usr/bin/paplay" if name == "paplay" else None,
        ),
    ):
        assert sound._get_sound_player("click") == ["paplay", str(bundled)]

    sound._sound_configs.clear()
    with (
        patch("src.core.sound.get_config_dir", return_value=tmp_path / "config"),
        patch(
            "src.core.sound.shutil.which",
            side_effect=lambda name: "/usr/bin/aplay" if name == "aplay" else None,
        ),
        patch("src.core.sound.Path.exists", return_value=True),
    ):
        assert sound._get_sound_player("delete")[0] == "aplay"


def test_sound_player_falls_back_to_bell_without_asset_or_player(tmp_path):
    with (
        patch("src.core.sound.get_config_dir", return_value=tmp_path / "config"),
        patch("src.core.sound.Path.exists", return_value=False),
        patch("src.core.sound.shutil.which", return_value=None),
    ):
        assert sound._get_sound_player("unknown") == "bell"


def test_sound_player_result_is_cached(tmp_path):
    with (
        patch("src.core.sound.get_config_dir", return_value=tmp_path / "config"),
        patch("src.core.sound.Path.exists", return_value=False),
        patch("src.core.sound.shutil.which", return_value=None) as which,
    ):
        assert sound._get_sound_player("click") == "bell"
        assert sound._get_sound_player("click") == "bell"
    which.assert_not_called()


def test_play_sound_uses_bell_and_respects_mute(capsys):
    with patch("src.core.sound._get_sound_player", return_value="bell"):
        sound._play_sound("click", 2)
    assert capsys.readouterr().out == "\a\a"
    sound._muted = True
    with patch("src.core.sound._get_sound_player") as resolve:
        sound._play_sound("click", 1)
    resolve.assert_not_called()


def test_play_sound_starts_player_and_falls_back_on_failure(capsys):
    player = ["aplay", "/tmp/click.wav"]
    with (
        patch("src.core.sound._get_sound_player", return_value=player),
        patch("src.core.sound.subprocess.Popen", return_value=SimpleNamespace()) as popen,
    ):
        sound._play_sound("click", 1)
    popen.assert_called_once_with(
        player, stdout=sound.subprocess.DEVNULL, stderr=sound.subprocess.DEVNULL
    )
    with (
        patch("src.core.sound._get_sound_player", return_value=player),
        patch("src.core.sound.subprocess.Popen", side_effect=OSError("missing player")),
    ):
        sound._play_sound("click", 2)
    assert capsys.readouterr().out == "\a\a"


def test_public_sound_helpers_delegate():
    with patch("src.core.sound._play_sound") as play:
        sound.play_click()
        sound.play_delete()
    assert play.call_args_list[0].args == ("click", 1)
    assert play.call_args_list[1].args == ("delete", 2)
