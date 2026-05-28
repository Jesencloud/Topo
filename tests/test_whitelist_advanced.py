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
