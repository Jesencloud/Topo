from src.core.whitelist import add_to_whitelist, get_whitelist, is_protected, remove_from_whitelist


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


def test_linux_sensitive_app_data_is_protected(test_env):
    sensitive_paths = [
        test_env / ".ssh/id_ed25519",
        test_env / ".gnupg/private-keys-v1.d/key.key",
        test_env / ".mozilla/firefox/profile.default/logins.json",
        test_env / ".config/google-chrome/Default/Login Data",
        test_env / ".config/Bitwarden/data.json",
        test_env / ".config/fcitx5/profile",
        test_env / ".config/rime/default.custom.yaml",
        test_env / ".local/share/fcitx/table/user.mb",
        test_env / ".config/dconf/user",
        test_env / ".config/gnome-shell/extensions/user-theme@gnome-shell-extensions.gcampax.github.com",
        test_env / ".config/gtk-3.0/settings.ini",
        test_env / ".local/share/gvfs-metadata/home",
        test_env / ".local/share/DBeaverData/workspace6/General/.dbeaver/credentials-config.json",
        test_env / ".config/Code/User/settings.json",
        test_env / ".var/app/org.mozilla.firefox/.mozilla/firefox/profile.default/logins.json",
        test_env / ".var/app/org.keepassxc.KeePassXC/config/keepassxc.ini",
    ]

    for path in sensitive_paths:
        assert is_protected(path) is True


def test_linux_sensitive_app_data_does_not_protect_unrelated_paths(test_env):
    assert is_protected(test_env / ".cache/some-app/cache.db") is False
    assert is_protected(test_env / ".config/my-normal-app/config.json") is False
