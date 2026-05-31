from pathlib import Path

FORBIDDEN_MACOS_TOKENS = [
    "/library/",
    "launchagent",
    "launchdaemon",
    "osascript",
    "spotlight",
    "homebrew cask",
    "mobilesync",
    "deriveddata",
    "xcode",
]


def test_src_does_not_use_macos_only_cleanup_primitives():
    root = Path(__file__).parents[1] / "src"
    offenders = []
    for path in root.rglob("*.py"):
        text = path.read_text(errors="ignore").lower()
        for token in FORBIDDEN_MACOS_TOKENS:
            if token in text:
                offenders.append(f"{path.relative_to(root)}:{token}")

    assert offenders == []
