from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_system_packages_do_not_modify_user_home_from_maintainer_scripts():
    build_script = (REPO_ROOT / "packaging/build-linux-packages.sh").read_text()

    assert "--after-install" not in build_script
    assert "--after-remove" not in build_script
    assert not (REPO_ROOT / "packaging/scripts/after-install.sh").exists()
    assert not (REPO_ROOT / "packaging/scripts/after-remove.sh").exists()
