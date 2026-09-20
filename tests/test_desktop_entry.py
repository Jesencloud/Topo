from src.core.desktop_entry import (
    get_desktop_exec_command,
    get_desktop_exec_names,
    get_desktop_icon,
    get_desktop_name,
    get_desktop_type,
    is_hidden_desktop,
    is_launchable_application,
    parse_desktop_entry,
)


def test_parse_desktop_entry_keeps_localized_keys(tmp_path):
    desktop_file = tmp_path / "app.desktop"
    desktop_file.write_text(
        "# comment\n[Desktop Entry]\nName=English Name\nName[zh_CN]=中文名字\nIcon=my-icon\n"
    )

    fields = parse_desktop_entry(desktop_file)

    assert fields["Name"] == "English Name"
    assert fields["Name[zh_CN]"] == "中文名字"
    assert fields["Icon"] == "my-icon"


def test_desktop_name_prefers_locale_and_falls_back_to_name(tmp_path):
    desktop_file = tmp_path / "app.desktop"
    desktop_file.write_text("Name=English Name\nName[zh_CN]=中文名字\n")

    assert get_desktop_name(desktop_file) == "中文名字"
    assert get_desktop_name(desktop_file, locale="fr_FR") == "English Name"


def test_desktop_exec_command_handles_quoted_path_and_field_codes(tmp_path):
    desktop_file = tmp_path / "app.desktop"
    app_path = tmp_path / "My App"
    desktop_file.write_text(f'Exec="{app_path}" --open %U\nIcon=my-app\n')

    assert get_desktop_exec_command(desktop_file) == str(app_path)
    assert get_desktop_exec_names(desktop_file) == {"My App"}
    assert get_desktop_icon(desktop_file) == "my-app"


def test_desktop_exec_command_rejects_malformed_quoted_exec(tmp_path):
    desktop_file = tmp_path / "app.desktop"
    desktop_file.write_text('Exec="/missing/dead-app\n')

    assert get_desktop_exec_command(desktop_file) == ""
    assert get_desktop_exec_names(desktop_file) == set()


def test_desktop_exec_command_unwraps_env_and_its_assignments(tmp_path):
    desktop_file = tmp_path / "app.desktop"
    desktop_file.write_text("Exec=env FOO=bar /opt/app %U\n")

    assert get_desktop_exec_command(desktop_file) == "/opt/app"
    assert get_desktop_exec_names(desktop_file) == {"app"}


def test_desktop_exec_command_skips_multiple_env_assignments(tmp_path):
    desktop_file = tmp_path / "app.desktop"
    desktop_file.write_text("Exec=env A=1 B=2 /usr/bin/foo\n")

    assert get_desktop_exec_command(desktop_file) == "/usr/bin/foo"
    assert get_desktop_exec_names(desktop_file) == {"foo"}


def test_desktop_exec_command_drops_bare_interpreter(tmp_path):
    desktop_file = tmp_path / "app.desktop"
    desktop_file.write_text("Exec=python /opt/app/main.py\n")

    assert get_desktop_exec_command(desktop_file) == ""
    assert get_desktop_exec_names(desktop_file) == set()


def test_desktop_exec_command_recognises_interpreter_by_basename(tmp_path):
    desktop_file = tmp_path / "app.desktop"
    desktop_file.write_text("Exec=/usr/bin/python3 script.py\n")

    assert get_desktop_exec_command(desktop_file) == ""
    assert get_desktop_exec_names(desktop_file) == set()


def test_desktop_exec_command_drops_sh_wrapper(tmp_path):
    desktop_file = tmp_path / "app.desktop"
    desktop_file.write_text("Exec=sh -c 'exec /opt/app'\n")

    assert get_desktop_exec_command(desktop_file) == ""
    assert get_desktop_exec_names(desktop_file) == set()


def test_desktop_exec_command_drops_flatpak_and_snap_run(tmp_path):
    flatpak_file = tmp_path / "flatpak.desktop"
    flatpak_file.write_text("Exec=flatpak run org.example.App\n")
    snap_file = tmp_path / "snap.desktop"
    snap_file.write_text("Exec=snap run foo\n")

    assert get_desktop_exec_command(flatpak_file) == ""
    assert get_desktop_exec_command(snap_file) == ""


def test_launchable_application_reads_hidden_and_type(tmp_path):
    hidden = tmp_path / "hidden.desktop"
    hidden.write_text("Type=Application\nHidden=true\nName=Gone\n")
    link = tmp_path / "link.desktop"
    link.write_text("Type=Link\nName=A link\n")
    default_type = tmp_path / "default.desktop"
    default_type.write_text("Name=No type\n")
    nodisplay = tmp_path / "nodisplay.desktop"
    nodisplay.write_text("Type=Application\nNoDisplay=true\nName=Runs anyway\n")

    assert is_hidden_desktop(hidden) is True
    assert is_launchable_application(hidden) is False

    assert get_desktop_type(link) == "Link"
    assert is_launchable_application(link) is False

    assert get_desktop_type(default_type) == ""
    assert is_launchable_application(default_type) is True

    # NoDisplay only hides the entry from menus; the app still launches.
    assert is_launchable_application(nodisplay) is True
