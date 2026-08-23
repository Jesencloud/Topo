"""Interactive screens: the flows that drive a full-terminal selector loop.

These sit above both the widget toolkit in ui.navigator and the feature logic in
core/ and the feature modules, composing the two. Keeping them out of those
modules is what lets ui.navigator stay a toolkit that knows nothing about
applications or cleanup -- tach enforces that: src.ui may depend only on
src.core, while src.ui.screens may reach for the feature modules it drives.

Non-interactive flows (run_clean, optimize_system, run_remove, ...) print and
exit, so they stay in their own feature modules; only the selector-driven ones
live here.
"""
