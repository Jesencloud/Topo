import json

from src.core.whitelist import (
    add_to_whitelist,
    get_whitelist,
    get_whitelist_file,
    is_cleanable_linux_app_data,
    is_protected,
    remove_from_whitelist,
)


def test_whitelist_persistence(test_env):
    """Verify adding/removing paths from the whitelist persists to disk."""
    my_secure_folder = test_env / "secure_data"
    my_secure_folder.mkdir()

    # 1. Add to whitelist
    assert add_to_whitelist(str(my_secure_folder)) is True
    assert str(my_secure_folder.resolve()) in get_whitelist()
    assert is_protected(my_secure_folder) is True

    # 2. Check child protection
    child_file = my_secure_folder / "secret.txt"
    assert is_protected(child_file) is True

    # 3. Remove from whitelist
    assert remove_from_whitelist(str(my_secure_folder)) is True
    assert is_protected(my_secure_folder) is False
    assert is_protected(child_file) is False


def test_whitelist_normalization(test_env):
    """Verify that different path formats resolve to the same protection."""
    folder = test_env / "Work"
    folder.mkdir()

    add_to_whitelist(str(folder))

    # Relative paths or trailing slashes should still match
    assert is_protected(str(folder) + "/") is True
    assert is_protected(folder) is True


def test_legacy_seeded_system_paths_are_ignored(test_env):
    whitelist_file = get_whitelist_file()
    whitelist_file.parent.mkdir(parents=True, exist_ok=True)
    whitelist_file.write_text(json.dumps(["/", "/var", "/usr", str(test_env / "keep")]))

    assert get_whitelist() == [str(test_env / "keep")]
    assert is_protected("/var/tmp/topo-stale.tmp") is False


def test_linux_sensitive_app_data_is_protected(test_env):
    sensitive_paths = [
        test_env / ".ssh/id_ed25519",
        test_env / ".gnupg/private-keys-v1.d/key.key",
        test_env / ".mozilla/firefox/profile.default/logins.json",
        test_env / ".config/google-chrome/Default/Login Data",
        test_env / ".config/microsoft-edge/Default/Cookies",
        test_env / ".config/BraveSoftware/Brave-Browser/Default/Login Data",
        test_env / ".config/vivaldi/Default/Local State",
        test_env / ".librewolf/profile.default/key4.db",
        test_env / ".config/Bitwarden/data.json",
        test_env / ".config/fcitx5/profile",
        test_env / ".config/rime/default.custom.yaml",
        test_env / ".local/share/fcitx/table/user.mb",
        test_env / ".config/dconf/user",
        test_env
        / ".config/gnome-shell/extensions/user-theme@gnome-shell-extensions.gcampax.github.com",
        test_env / ".config/gtk-3.0/settings.ini",
        test_env / ".local/share/gvfs-metadata/home",
        test_env / ".local/share/DBeaverData/workspace6/General/.dbeaver/credentials-config.json",
        test_env / ".config/Code/User/settings.json",
        test_env / ".config/Signal/config.json",
        test_env / ".config/discord/Local State",
        test_env / ".config/Slack/storage.json",
        test_env / ".local/share/TelegramDesktop/tdata/settings",
        test_env / ".aws/credentials",
        test_env / ".kube/config",
        test_env / ".docker/config.json",
        test_env / ".config/gh/hosts.yml",
        test_env / ".bashrc",
        test_env / ".zsh_history",
        test_env / ".config/fish/config.fish",
        test_env / ".config/nvim/init.lua",
        test_env / ".emacs.d/init.el",
        test_env / ".config/sublime-text/Packages/User/Preferences.sublime-settings",
        test_env / ".var/app/org.mozilla.firefox/.mozilla/firefox/profile.default/logins.json",
        test_env / ".var/app/org.keepassxc.KeePassXC/config/keepassxc.ini",
        test_env / ".var/app/org.telegram.desktop/data/TelegramDesktop/tdata/settings",
        test_env / ".var/app/com.discordapp.Discord/config/discord/Local State",
    ]

    for path in sensitive_paths:
        assert is_protected(path) is True


def test_linux_browser_cache_paths_are_cleanable_inside_protected_profiles(test_env):
    cleanable_paths = [
        test_env / ".config/google-chrome/Default/Cache",
        test_env / ".config/google-chrome/Default/Code Cache/js",
        test_env / ".config/google-chrome/Default/Service Worker/CacheStorage/index",
        test_env / ".config/chromium/Default/GPUCache/data.bin",
        test_env / ".config/BraveSoftware/Brave-Browser/Default/ShaderCache/data.bin",
        test_env / ".config/microsoft-edge/Default/DawnWebGPUCache/data.bin",
        test_env / ".config/vivaldi/Default/GrShaderCache/data.bin",
        test_env / ".config/opera/Default/Crashpad/completed/report.dmp",
        test_env / ".mozilla/firefox/profile.default/cache2/entries/abc",
        test_env / ".mozilla/firefox/profile.default/startupCache/startupCache.8.little",
        test_env / ".librewolf/profile.default/jumpListCache/icon.bin",
        test_env
        / ".var/app/org.mozilla.firefox/.mozilla/firefox/profile.default/cache2/entries/abc",
        test_env / ".var/app/com.brave.Browser/config/BraveSoftware/Brave-Browser/Default/Cache",
    ]

    for path in cleanable_paths:
        assert is_cleanable_linux_app_data(path) is True
        assert is_protected(path) is False


def test_linux_browser_profile_roots_and_credentials_stay_protected(test_env):
    protected_paths = [
        test_env / ".config/google-chrome",
        test_env / ".config/google-chrome/Default",
        test_env / ".config/google-chrome/Default/Login Data",
        test_env / ".config/google-chrome/Default/Cookies",
        test_env / ".config/google-chrome/Default/Service Worker",
        test_env / ".config/microsoft-edge/Default",
        test_env / ".config/microsoft-edge/Default/Login Data",
        test_env / ".config/BraveSoftware/Brave-Browser/Default/Cookies",
        test_env / ".mozilla/firefox/profile.default",
        test_env / ".mozilla/firefox/profile.default/logins.json",
        test_env / ".mozilla/firefox/profile.default/key4.db",
        test_env / ".mozilla/firefox/profile.default/cookies.sqlite",
        test_env / ".librewolf/profile.default",
        test_env / ".var/app/org.mozilla.firefox/.mozilla/firefox/profile.default/logins.json",
    ]

    for path in protected_paths:
        assert is_cleanable_linux_app_data(path) is False
        assert is_protected(path) is True


def test_linux_sensitive_app_data_does_not_protect_unrelated_paths(test_env):
    assert is_protected(test_env / ".cache/some-app/cache.db") is False
    assert is_protected(test_env / ".config/my-normal-app/config.json") is False


def test_xdg_user_data_dirs_protected_as_directories(test_env):
    """Standard XDG user-data dirs are protected as whole directories (so
    uninstall can't wipe ~/Music), but files inside them stay deletable."""
    from src.core.whitelist import get_hard_protection_reason

    for name in ("Music", "Videos", "Documents", "Pictures", "Downloads"):
        assert get_hard_protection_reason(test_env / name) == "user data directory"
        assert is_protected(test_env / name) is True

    # Files inside are NOT hard-protected — Analyze can still delete them.
    assert get_hard_protection_reason(test_env / "Music" / "song.mp3") is None
    assert is_protected(test_env / "Music" / "song.mp3") is False
