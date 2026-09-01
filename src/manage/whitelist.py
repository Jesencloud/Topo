"""The ``topo whitelist`` subcommand: protect a path, unprotect one, or list them.

The list itself lives in :mod:`src.core.whitelist`, which every deletion in the
program consults. What is here is only the command that edits it: the lines it
prints and the exit status each answer earns.

That pairing is the reason this is a module rather than four print statements in
the argument parser. To the store, "the path was already there" and "the file
could not be written" are both non-events -- neither changed anything -- and
reporting them the same way made a full disk look exactly like a duplicate. Each
branch below decides its line and its boolean in the same place, so the two
cannot drift apart again.

``main.py`` keeps the argument checks, which need the subparser to fail the way
argparse fails, and calls in here for the doing.
"""

from ..core.constants import FAIL, INFO, OK, RESET, THEME_TITLE
from ..core.whitelist import add_to_whitelist, get_whitelist, remove_from_whitelist


def _add(path: str) -> bool:
    """Protect *path*, reporting whether the list now says what was asked.

    A duplicate is not a failure: adding a path that is already protected leaves
    the system in the state the caller asked for, so ``topo whitelist add`` is
    safe to run unconditionally in a script. A write that did not happen *is* a
    failure, and used to be reported with that same "already whitelisted" line
    and an exit status of 0 -- so a full disk or a corrupt whitelist looked
    exactly like success.
    """
    result = add_to_whitelist(path)
    if result == "changed":
        print(f"{OK} Added to whitelist: {path}")
    elif result == "unchanged":
        print(f"{INFO} Path already whitelisted: {path}")
    else:
        print(f"{FAIL} Could not update the whitelist: {path}")
        return False
    return True


def _remove(path: str) -> bool:
    """Unprotect *path*, reporting whether it was there to unprotect.

    Nothing removed is a failure here, unlike nothing added above: the caller
    named a path to stop protecting and that path was not protected, so the
    answer to what they asked is no. The two ways of failing still print
    differently -- one is about the path, the other about the file.
    """
    result = remove_from_whitelist(path)
    if result == "changed":
        print(f"{OK} Removed from whitelist: {path}")
        return True
    if result == "unchanged":
        print(f"{FAIL} Path not found in whitelist: {path}")
    else:
        print(f"{FAIL} Could not update the whitelist: {path}")
    return False


def _show() -> None:
    """Print the protected paths, saying so plainly when there are none."""
    entries = get_whitelist()
    print(f"{THEME_TITLE}🛡️  Current Whitelist:{RESET}")
    if not entries:
        print("   (Empty)")
    for entry in entries:
        print(f"   - {entry}")


def run_whitelist(action: str, path: str | None) -> bool:
    """Run one whitelist action, returning whether it did what was asked.

    ``list`` is the only action that arrives without a path: main.py turns a
    missing PATH for the other two into argparse's own exit 2 before this is
    called, so reaching the guard below means nothing was named to act on.
    """
    if action == "list":
        _show()
        return True
    if path is None:
        return False
    return _add(path) if action == "add" else _remove(path)
