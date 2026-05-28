from typing import Any


def interactive_select(items: list[dict[str, Any]]) -> list[int]:
    """A simple interactive selector for now. Will be upgraded to Rich TUI later."""
    if not items:
        return []

    print("\n📦 Select artifacts to purge:")
    for i, item in enumerate(items):
        print(
            f"[{i:2}] {item['human_size']:>8} | {item['project']:<15} | {item['path'].relative_to(item['path'].parents[1])}"
        )

    print("\nOptions: index(es) separated by space (e.g. '0 1 5'), 'all', or 'q' to cancel.")
    try:
        choice = input("➤ ").strip().lower()
        if choice == "q":
            return []
        if choice == "all":
            return list(range(len(items)))

        indices = [int(i) for i in choice.split() if i.isdigit()]
        return [i for i in indices if 0 <= i < len(items)]
    except (ValueError, KeyboardInterrupt):
        return []
