"""How strictly a name has to match before something is acted on.

One rule, in one place, because the two halves that consume it are the two that
do damage: residue discovery decides which directories to delete, and process
termination decides which comm patterns to hand `pkill -9`. Both used to reach
into ``UninstallManager`` for the same static method, so a change made with
residue in mind silently changed which processes get killed -- and vice versa.
Keeping the rule here does not remove that coupling, it makes it the point: the
two must stay equally strict, and now there is one definition to read.

Nothing here touches the filesystem, runs a command, or imports anything: it is
string policy, and the modules that act on its answer are elsewhere.
"""

# Tokens too short or generic to safely substring-match against folder names.
# Matching these loosely would flag unrelated directories for deletion
# (e.g. "desktop" from "org.telegram.desktop", or "data"/"app").
GENERIC_TOKENS = frozenset(
    {
        "app",
        "apps",
        "data",
        "core",
        "bin",
        "cache",
        "config",
        "share",
        "gui",
        "lib",
        "tmp",
        "temp",
        "default",
        "common",
        "main",
        "client",
        "desktop",
        "system",
        "settings",
        "local",
        "user",
        "code",
        "go",
        "id",
    }
)


def name_matches(entry_lower: str, token: str) -> bool:
    """Conservatively decide whether a folder name belongs to an app token.

    Avoids deleting unrelated directories by rejecting short/generic tokens
    and requiring a word boundary for prefix matches. Only distinctive
    tokens (>= 5 chars) are allowed to match as a free substring.
    """
    token = token.strip().lower()
    if not token or token in GENERIC_TOKENS:
        return False
    if entry_lower == token:
        return True
    if len(token) < 3:
        return False  # too short for any fuzzy matching
    # Word-boundary prefix, e.g. "telegram" -> "telegram-desktop"
    if any(entry_lower.startswith(token + sep) for sep in ("-", "_", ".", " ")):
        return True
    # Distinctive tokens may appear anywhere in the folder name
    return len(token) >= 5 and token in entry_lower
