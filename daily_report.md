# Daily Modification Report - 2026-06-10

## Project: topo (Topo) - CLI-First Scrolling and Main Menu Mouse Support

Today's session refined Topo's terminal interaction model. The main menu and stateful selectors still use Topo's TUI rendering, while command-style result output from Clean, Optimize, and Status now uses the terminal's native scrollback like other CLI tools.

### 1. Main Menu Mouse Scrolling
*   **Root Cause**: `InteractiveMenu.run()` entered the selector session with mouse tracking disabled, so the main menu could draw a scrollbar but could not receive mouse wheel or scrollbar drag events after terminal resizing.
*   **Fix**: Enabled mouse tracking for the main menu by switching `InteractiveMenu.run()` to `_selector_session(enable_mouse=True)`.
*   **Regression Coverage**: Added tests proving the main menu enables mouse tracking and that mouse wheel input scrolls a short terminal viewport.

### 2. Lightweight TUI Scrollbar and Hide Option
*   **Lighter Visual Style**: Replaced the heavier `┃` / `│` scrollbar rendering with a minimal gray `▐` thumb and blank track, making the TUI scrollbar less visually dominant.
*   **Configurable Visibility**: Added `show_scrollbar` to `~/.config/topo/config.json`, defaulting to `true`.
*   **Hidden Scrollbar Behavior**: When `show_scrollbar` is `false`, the right-edge scrollbar is hidden but mouse wheel scrolling still works for scrollable TUI views.
*   **Internal State Split**: Split frame state into `scrollable` and `scrollbar_visible`, so content scrolling and visual scrollbar rendering are no longer coupled.

### 3. CLI-First Result Output
*   **Design Change**: Clean, Optimize, and Status launched from the main menu now exit the alternate screen before printing results, so the terminal's native scrollback and default scrollbar handle long output.
*   **TUI Boundary**: Main menu, Analyze, Uninstall, and other stateful selectors still use Topo's alternate-screen TUI because they need active cursor state, keyboard navigation, and mouse handling.
*   **Return Flow**: After Clean, Optimize, or Status finishes in the normal terminal screen, `Navigator.wait_for_return()` is used to return to the main menu or exit.
*   **Helper Cleanup**: Added small helpers in `src/main.py` for clearing alternate-screen views, running normal terminal commands, and running alternate-screen TUI commands.

### 4. Removed Abandoned Result Viewer Path
*   **No Internal Result Viewer Left**: Removed the temporary `OutputViewer` approach that would have shown command results inside Topo's own scrollable frame.
*   **Residual Code Audit**: Checked for abandoned symbols such as `OutputViewer`, `_run_scrollable_tui_command`, `_StdoutTee`, and `redirect_stdout`; none remain from the discarded result-viewer implementation.
*   **Expected Existing Uses**: Remaining `io.StringIO` usage is unrelated and still valid: banner capture in `navigator.py` and grouped Clean subtask output in `clean/runner.py`.

### 5. Regression Coverage and Verification
*   **Main Flow Tests**: Added `tests/test_main.py` to verify terminal-mode command execution waits for return and skips the wait when a command returns `False`.
*   **Navigator Tests**: Extended selector tests for main-menu mouse tracking, mouse-wheel scrolling, lightweight scrollbar rendering, hidden scrollbar behavior, and continued wheel scrolling without a visible scrollbar.
*   **Config Tests**: Extended config normalization coverage for `show_scrollbar`.
*   **Verification Commands**:
    *   `pytest -q` passed with **279 tests**.
    *   `ruff check` passed.
    *   `ruff format --check` passed.
    *   `git diff --check` passed.

### 6. Analyze High-Fanout Directory Guard
*   **Problem Found**: Entering directories with many direct small files, such as `~/.cache/gnome-software/icons`, could still make Analyze expensive because even a single-level Rust scan must `stat` every entry before rendering.
*   **Generalized Shallow Preview**: Changed shallow-preview selection from a hardcoded set of cache-like leaf names to any directory whose direct child count exceeds the preview threshold. This keeps browser `Cache_Data`, `node_modules`, GNOME Software icon caches, and ordinary high-fanout directories responsive.
*   **Preserved Real Filesystem Display**: The idea of hiding `~/.cache` from Analyze was reviewed and rejected. Analyze continues to show the real directory tree; Topo does not hide or rewrite the user's filesystem view.
*   **Clean Boundary**: Cache cleanup remains the responsibility of `topo clean`. Analyze only protects responsiveness by using bounded preview mode for very wide directories.
*   **Regression Coverage**: Updated Analyze tests to cover `~/.cache/gnome-software/icons`, ordinary wide directories, `node_modules`, and small directories that should not trigger shallow preview.
*   **Verification Commands**:
    *   `pytest -q` passed with **279 tests**.
    *   `ruff check` passed.
    *   `ruff format --check` passed.
    *   `git diff --check` passed.

### 7. Analyze Preview Scope Correction
*   **Avoided Over-Eager File-Manager Mode**: The first follow-up made all directory drill-downs behave like a fast file-manager listing. That opened quickly, but it removed the useful size-ranked Analyze view too early and made ordinary directory screens less useful.
*   **Final Scope**: Ordinary directories now keep the Rust size-analysis path and continue to show calculated child sizes sorted by size. Fast Preview mode is used only when a directory has more than **500** direct children.
*   **Bounded Preview Behavior**: For high-fanout directories, Analyze samples the first **500** direct entries, avoids recursive directory-size calculation, shows unknown folder sizes as `--`, and sorts preview rows directory-first by name.
*   **No Manual Scan Shortcut**: No user-triggered `S` scan action was added. `S` remains the existing sort toggle.
*   **Cache Boundary**: Preview data is intentionally built from the live directory listing and does not reuse or pollute the Rust scan cache. This prevents stale Rust analysis data from affecting high-fanout preview screens.
*   **Redundancy Review**: Checked the final code for abandoned tree/shallow-preview symbols, unused preview fields, and stale `S:Scan`/scan-current UI paths. No Analyze-related residue remains.
*   **Regression Coverage**: Updated Analyze and Navigator tests for threshold-based Preview selection, ordinary Rust size views, unknown folder-size rendering, directory-first name sorting, and preview deletion confirmation text.
*   **Verification Commands**:
    *   `pytest -q` passed with **277 tests**.
    *   `pytest -q tests/test_analyze.py tests/test_navigator.py` passed with **59 tests**.
    *   `ruff check` passed.
    *   `ruff format --check` passed.
    *   `git diff --check` passed.

---

# Daily Modification Report - 2026-06-09

## Project: topo (Topo) - Shared Heavy Cache Metadata for Analyze and Clean

Today's session completed the next shared-cache refactor after the 2026-06-08 work. Package-manager caches, container cache roots, and AI/model caches now share metadata between Analyze and Clean, while destructive cleanup behavior remains owned by Clean and still uses tool-specific commands or age-based cleanup rules.

### 1. Shared Heavy Cache Definitions
*   **New Metadata Module**: Added `src/core/heavy_cache.py` as the shared source for heavyweight cache families.
*   **Package Manager Cache Roots**: Centralized Analyze metadata for APT, DNF, and Pacman cache paths.
*   **Container Cache Roots**: Centralized Analyze metadata for Docker user data, Docker system data, Podman transfer cache, and Flatpak data.
*   **AI/Model Cache Roots**: Centralized Analyze metadata for Ollama models, HuggingFace Hub, LM Studio, PyTorch kernel cache, OpenAI Triton cache, and NVIDIA CUDA cache.
*   **Dynamic Home Resolution**: Shared definitions store paths as strings and resolve `~` at runtime, keeping tests and user environments isolated from import-time `HOME` values.
*   **Analyze Integration**: `run_deep_analysis()` now builds Linux insights through `build_linux_insights(home)`, replacing the previous hardcoded Docker/APT/Ollama list.
*   **Display Threshold Metadata**: Analyze now reads `min_display_bytes` from each shared definition, while keeping the default 10 MB threshold for ordinary insight entries.

### 2. Clean Integration Boundary
*   **Clean Still Owns Cleanup**: The shared metadata is used for discovery and consistency, but actual cleanup remains in Clean because package managers, containers, and model tools need command-specific or age-specific behavior.
*   **Package Manager Cleaner Mapping**: `clean_package_manager()` now uses shared cleaner definitions for `apt-get clean`, `dnf clean all`, and `pacman -Sc --noconfirm` instead of maintaining local command branches.
*   **AI/Model Cleanup Rules**: `clean_ai_models()` now iterates shared age-based cleanup definitions, including HuggingFace Hub, Ollama blobs, PyTorch, Triton, CUDA, and LM Studio caches.
*   **Podman Cache Reuse**: `clean_podman()` now resolves the Podman transfer cache path from shared container metadata.
*   **Podman Storage Policy**: `Podman Storage` is intentionally not shown as an Analyze cache item because `~/.local/share/containers` is real container storage. Podman cleanup should stay command-driven through Clean instead of direct Analyze deletion.

### 3. Duplication and API Cleanup
*   **Removed Duplicate AI Cache Constants**: Deleted HuggingFace, Ollama, Triton, Torch, and CUDA entries from `DEV_CACHES`; that constant now only keeps developer-tool caches for npm, pip, cargo, and Go.
*   **Removed Dead Category Metadata**: Dropped the unused `cache_category` plumbing and related category constants after review showed production code did not consume them.
*   **Stronger Container Lookup API**: `get_container_cache_def()` now raises an explicit `KeyError` with the missing key instead of leaking a bare `StopIteration`.

### 4. Regression Coverage
*   **Heavy Cache Tests**: Added `tests/test_heavy_cache.py` to verify shared package-manager, container, and AI/model definitions, dynamic home resolution, cleaner commands, missing-key behavior, and removal of duplicate AI entries from `DEV_CACHES`.
*   **Analyze Tests**: Added coverage proving Linux insights are built from shared metadata and that `Podman Storage` is not surfaced as an Analyze cache target.
*   **Clean Tests**: Added coverage proving AI/model cleanup uses the shared age-based cleanup definitions.

### 5. Verification
*   **Full Test Suite**: `pytest -q` passed with **257 tests**.
*   **Ruff Check**: `ruff check` passed.
*   **Ruff Format**: `ruff format --check` passed.
*   **Diff Hygiene**: `git diff --check` passed.

### 6. Package Update GPG Verification
*   **Automatic Signature Verification**: Package-mode `topo update` now verifies the Release checksum manifest before installing downloaded `.deb` / `.rpm` packages when `gpg` is available.
*   **Release Assets Used**: The updater downloads `SHA256SUMS`, `SHA256SUMS.asc`, and `topo-release-public.asc` alongside the target package. It verifies the manifest signature first, then verifies the package hash against `SHA256SUMS`.
*   **Temporary GPG Home**: Signature verification imports the release public key into a temporary `gnupg` directory under the update temp directory, so user keyrings are not modified.
*   **Pinned Signer Binding**: Verification no longer trusts a key merely because the expected Topo fingerprint exists in the temporary keyring. It parses `gpg --status-fd=1 --verify` output and requires the `VALIDSIG` primary-key fingerprint to match the pinned Topo release fingerprint `4B35 C17C F8E6 6373 2726 A99F 5008 6DB9 98B4 D883`.
*   **Multi-Key Attack Rejection**: Added coverage for the case where a downloaded public-key asset contains both the real Topo public key and another key, but `SHA256SUMS.asc` is signed by the other key. That update is now rejected.
*   **No-GPG Fallback Warning**: If `gpg` is not installed, package-mode update falls back to SHA256-only verification and prints that SHA256 detects corrupted downloads but does not authenticate the release publisher.
*   **Fail-Closed With GPG**: If `gpg` is installed, missing or invalid signature/key assets stop the update. Release publishing must continue to attach `SHA256SUMS.asc` and `topo-release-public.asc` for every package release.
*   **README Trust Boundary Note**: The package install section now states that `topo update` verifies `SHA256SUMS.asc` when `gpg` exists, and that SHA256-only fallback does not authenticate the publisher.
*   **Regression Coverage**: Added update tests for no-`gpg` fallback messaging, pinned `VALIDSIG` parsing, foreign signer rejection, multi-key public-key asset rejection, and signature asset download behavior.
*   **Verification**: Re-ran `pytest -q` with **264 tests**, `ruff check`, `ruff format --check`, and `git diff --check`; all passed.

### 7. Analyze Large Cache Directory Preview
*   **Problem Found**: Entering browser cache leaf directories such as `~/.cache/BraveSoftware/Brave-Browser/Default/Cache/Cache_Data` could still feel stuck because the directory contains many direct small files. Avoiding `--tree` was not enough; a single-level Rust scan still had to stat every file before the UI could open.
*   **Tree Scan Guard**: Analyze now skips whole-subtree `--tree` cache priming for browser/cache/profile paths, `node_modules`, `.git`, known cache directory names, and very wide directories. Normal directories still use `--tree` so drill-down remains fast where it helps.
*   **Shallow Preview Mode**: Large leaf cache paths now use a bounded direct-child preview instead of a full scan. If a cache-like leaf has more than **500** direct entries, Analyze samples the first **500** direct entries and then shows the largest **50** rows from that sample.
*   **Clear UI Notice**: `AnalyzeSelector` now supports a notice row. Shallow preview views display: `Preview mode: sampled first 500 direct entries; listing largest 50 from that sample, not a complete size ranking.`
*   **Explicit Tradeoff**: The preview opens quickly but is intentionally not a complete size ranking. Full browser/cache cleanup remains better handled by `topo clean`, or by selecting the whole cache directory from its parent view.
*   **Preview Cache**: Shallow preview results are stored in `ScanCache`, so returning to the parent and entering the same cache leaf again is served from memory during the same Topo session.
*   **Test Runtime Fix**: Added `preview_sampled_entries` metadata so tests can verify the 500-entry preview message without constructing 500 fake result rows. `tests/test_analyze.py` dropped from about **4.6s** to about **0.4s** with durations enabled.
*   **Regression Coverage**: Added tests for wide-directory detection, cache/profile single-level fallback, shallow preview triggering, bounded non-recursive preview data, preview notice rendering, and cached preview behavior.
*   **Verification**: Re-ran `pytest -q` with **273 tests**, `ruff check`, `ruff format --check`, and `git diff --check`; all passed.

---

# Daily Modification Report - 2026-06-08

## Project: topo (Topo) - Shared Cache Classification for Clean and Analyze

Today's session continued the cache-cleanup refactor after the browser-cache work. The goal was to reduce duplicated cache rules between Clean and Analyze while keeping deletion behavior conservative: Clean owns automatic cleanup, Analyze only marks or safely handles paths that match shared cache metadata.

### 1. Desktop Application Cache Definitions
*   **Single Desktop App Cache Source**: Added `src/core/desktop_app_cache.py` to centralize high-precision desktop application cache definitions for Discord, Telegram, Slack, Spotify, Zoom, Microsoft Teams, VLC, OBS Studio, and WeChat.
*   **Cache vs Extra Cleanup Split**: Desktop app definitions now separate `cache_paths` from `extra_cleanup_paths`. Clean can still process app-specific cleanup paths, but Analyze only marks explicit cache paths as `App cache`.
*   **Dynamic Home Resolution**: Desktop app paths are resolved with `Path.home()` at runtime instead of being frozen when `constants.py` is imported. This improves test isolation and avoids stale HOME assumptions.
*   **Reduced Constants Bloat**: Removed the old `APP_DEFS` block from `src/core/constants.py`; Clean now calls `get_desktop_app_cleanup_defs()`.
*   **Duplicate Detection Avoidance**: `proactive_app_detection()` now skips known desktop app cache/config roots via `DESKTOP_APP_DETECTION_NAMES`, avoiding duplicate registry entries for apps already handled by shared definitions.

### 2. CACHEDIR.TAG Standard Cache Handling
*   **Shared Standard Cache Logic**: Moved standard cache classification into `src/core/app_cache.py` with `is_standard_cache_dir()`, `find_standard_cache_dirs()`, and `get_cache_cleanable_reason()`.
*   **Analyze Reason Reuse**: `build_analysis_entry()` no longer hardcodes `CACHEDIR.TAG` vs `App cache` logic. It now asks `get_cache_cleanable_reason()` for the shared reason string.
*   **Clean Discovery Reuse**: `clean_generic_xdg_caches()` now uses `find_standard_cache_dirs()` before generic XDG cache cleanup, so tagged cache discovery is shared instead of local to Clean.
*   **Symlink Safety**: `has_valid_cachedir_tag()` now rejects symlinked directories and symlinked tag files, preventing a tagged symlink from pulling an external directory into cleanup.
*   **Traversal Pruning**: Standard cache discovery prunes below a matched tagged cache directory so nested tags inside an already-cleanable cache root do not create duplicate cleanup targets.

### 3. Generic XDG Cache Classification
*   **Shared XDG Candidate Model**: Added `XdgCacheCandidate` and `find_xdg_cache_candidates()` in `src/core/app_cache.py` for top-level `~/.cache/<app>` candidates.
*   **Consistent Age Policy**: Generic XDG cache classification now carries its cleanup policy with the candidate:
    *   Names containing `cache`, `log`, `tmp`, or `temp` are labeled `Generic Cache` and use the conservative 3-day policy.
    *   Other top-level `~/.cache/<app>` entries are labeled `Stale App Data` and use the 30-day policy.
*   **Analyze Metadata**: Analyze now marks `~/.cache/<app>` entries as `XDG cache`, while the `~/.cache` root itself remains unmarked to avoid encouraging whole-root deletion.
*   **Profile Discovery Boundary**: Generic XDG classification is intentionally not used inside browser/profile child discovery. This prevents broad `~/.cache/mozilla/...` matching from hiding more precise cache children such as `cache2`.
*   **Symlink Safety**: Generic XDG candidate discovery skips symlinked cache entries so cleanup cannot follow `~/.cache/<name>` links into external data.

### 4. Code Simplification
*   **Removed Dead Helper**: Dropped the unused `is_cleanable_cache_path()` helper after the discovery path was narrowed.
*   **Avoided Double Handling**: `find_xdg_cache_candidates()` now skips `CACHEDIR.TAG` directories, so Clean no longer needs a separate `handled_tagged_cache_paths` set.
*   **Simplified Desktop App Cache Iteration**: Removed the internal-only `get_desktop_app_cache_defs()` helper; `iter_desktop_app_cache_paths()` now derives paths directly from `DESKTOP_APP_DEFS`.
*   **Cleaner Detection Setup**: Removed an unnecessary temporary variable from `proactive_app_detection()` when building known handled app names.

### 5. Regression Coverage
*   **Desktop App Coverage**: Added tests proving desktop app cache definitions resolve against the current HOME, Clean uses those definitions, Analyze marks desktop app cache paths as `App cache`, and WeChat extra cleanup paths are not marked as cache.
*   **Standard Cache Coverage**: Added tests for `CACHEDIR.TAG` discovery, recursive pruning, and symlinked tagged directory rejection.
*   **XDG Cache Coverage**: Added tests for generic XDG candidate classification, Analyze `XDG cache` metadata, symlink skip behavior, and non-interference with browser cache discovery.

### 6. Verification
*   **Full Test Suite**: `pytest -q` passed with **248 tests** after the follow-up cache safety and performance hardening.
*   **Ruff Check**: `python -m ruff check` passed.
*   **Ruff Format**: `python -m ruff format --check` passed.
*   **Diff Hygiene**: `git diff --check` passed.

### 7. Follow-Up Notes
*   The next shared-cache candidates worth considering are package-manager, container, and AI/model caches. Those should likely share metadata for Analyze display, but keep command-based cleanup in Clean because they often require package-manager or tool-specific commands instead of direct file deletion.
*   Analyze should continue to mark cache-like entries and provide safe deletion fallback only where validation allows it; broad automated cleanup should remain in Clean.

### 8. Follow-Up Cache Safety and Performance Review
*   **Analyze Icon Reverted**: Cleanable cache entries in Analyze no longer use the `🧹` icon. They now keep the regular directory/file icons while preserving `is_cleanable` and `cleanable_reason` metadata, avoiding the impression that every marked item will be automatically cleaned.
*   **Browser Cache Symlink Guard**: `find_cleanable_cache_dirs()` now prunes symlinked child directories before matching named cache directories such as `Cache` or `cache2`. This prevents browser-cache cleanup from following a profile-local symlink into external user data.
*   **Clean Root Symlink Guard**: `clean_app_generic()` now skips cleanup roots that are themselves symlinks before resolving paths, adding defense in depth for any future caller that passes a symlinked cache root directly.
*   **WeChat User Data Protection**: WeChat cleanup now keeps `.xwechat`, Flatpak `config/xwechat`, and `Documents/WeChat Files` out of Clean definitions. Only the explicit Flatpak cache path remains cleanable, so chat files and user documents are preserved.
*   **Fast Child Size Accounting**: Added `get_direct_child_sizes_fast()` in `src/core/file_ops.py` so Clean can scan a cache parent once with the Rust engine and reuse direct child sizes when calling `safe_remove()`. If the engine is unavailable or returns incompatible data, cleanup falls back to per-child `get_size_fast()`.
*   **Duplication Review Result**: Moved Rust child-size parsing out of `src/clean/apps.py` and into `file_ops`, keeping fast-size logic centralized with `get_size_fast()`. The remaining symlink and WeChat tests cover different safety layers, so they were kept intentionally rather than collapsed.
*   **Regression Coverage**: Added tests for symlinked browser cache discovery, symlinked cleanup roots, WeChat data preservation, Analyze icon expectations, parent-scan size reuse, and fallback sizing behavior.

### 9. Post-v0.9.5 Follow-Up Fixes
*   **Package Update Partial Download Retry**: Hardened package-mode `topo update` against intermittent GitHub Release/CDN short downloads such as `curl: (18) Transferred a partial file`. Downloads now write to `PACKAGE.part`, delete partial files on failure, retry up to four times, and only replace the final package path after a successful transfer.
*   **Package Update Error Detail**: `topo update` keeps curl stderr quiet during recoverable retries, but if all attempts fail it prints the final curl error detail below the user-facing failure message.
*   **Checksum Boundary Preserved**: The retry fix does not weaken package integrity checks. The downloaded `.deb` / `.rpm` still must match the Release `SHA256SUMS` entry before apt/dnf upgrade runs.
*   **Analyze Disk Icon Spacing**: Refined the v0.9.5 Analyze Disk spacing polish so folder rows show `🗂️  name`, while file rows show `📄 name` with only one visible space. This keeps folder names visually separated without making file rows look over-spaced.
*   **Regression Coverage**: Added update tests for User-Agent downloads, partial transfer retry, partial file cleanup after repeated failures, and navigator tests for folder/file icon spacing in Analyze Disk.
*   **Verification**: Re-ran `pytest -q` with **251 tests**, `ruff check`, `ruff format --check`, and `git diff --check`; all passed.

---

# Daily Modification Report - 2026-06-07

## Project: topo (Topo) - Browser Cache Cleanup Refactor, Analyze Safety & App Removal Sorting

Today's session focused on making browser cache cleanup safer and less duplicated across Clean, Analyze, and whitelist protection code, improving the app-removal list ordering, and locking the behavior with regression tests.

### 1. Analyze Disk Browser Cache Cleanup
*   **Problem Found**: `Analyze > Explore disk usage` could not delete content under paths such as `/home/jiantai/.config/google-chrome` because browser profile directories are intentionally protected as sensitive app data.
*   **Safety Boundary Preserved**: The fix does **not** allow deleting an entire browser profile root such as `.config/google-chrome`, `.mozilla`, or a `Default` profile directory. Credentials and state files remain protected, including `Login Data`, `Cookies`, `logins.json`, `key4.db`, and `cookies.sqlite`.
*   **Single Browser Rule Source**: Added `src/core/browser_cache.py` as the single source for browser profile roots, cache roots, Flatpak app IDs, process names, and cache directory names. Covered Firefox-family and Chromium-family browsers, including Firefox, LibreWolf, Floorp, Waterfox, Zen, Chrome, Chromium, Ungoogled Chromium, Brave, Edge, Vivaldi, Opera, Thorium, and Yandex Browser.
*   **Derived Protection Rules**: `src/core/whitelist.py` now imports browser profile roots, Flatpak browser app IDs, and cleanable cache directory names from `browser_cache.py` instead of maintaining a separate copy.
*   **Cleanable Cache Names**: Added a reusable `CLEANABLE_APP_CACHE_DIR_NAMES` set for cache-like directories such as `Cache`, `Code Cache`, `GPUCache`, `ShaderCache`, `DawnWebGPUCache`, `Crashpad`, `cache2`, `startupCache`, `CacheStorage`, and `jumpListCache`. The `Service Worker` root remains protected; only known cache children such as `Service Worker/CacheStorage` are cleanable.
*   **Analyze Delete Behavior**: When a user selects a protected browser profile root in Analyze, Topo now searches only inside the selected directory for known cache children and removes those cache directories. It does not scan the whole system and does not remove the profile root itself.
*   **Visible Skip Reasons**: Analyze now prints a skip message when a selected path is rejected by validation, instead of failing silently.
*   **Cleanable UI Metadata**: Browser cache directories are marked as cleanable entries with the `App cache` reason, matching the existing cleanable-entry model used for `CACHEDIR.TAG`.

### 2. Clean Browser Cache Refactor
*   **Clean Owns Browser Cache Cleanup**: Browser cache cleanup now lives in the Clean workflow through `clean_browser_caches()`. Analyze keeps only a safety fallback for cases where a user manually selects a protected browser profile root.
*   **Shared Cache Discovery**: Added `src/core/app_cache.py` with `find_cleanable_cache_dirs()` and `find_cleanable_cache_dirs_in_roots()`, so Clean and Analyze reuse the same cache-child discovery logic instead of walking browser profiles independently.
*   **Reduced Browser Hardcoding**: Removed old browser entries from `APP_DEFS` and stopped duplicating browser root/name derivation in `src/clean/apps.py`. Browser cleanup roots and skip names are now derived from `BROWSER_DEFS`.
*   **Safer Generic App Detection**: `proactive_app_detection()` now skips browser cache/profile roots already handled by the dedicated browser cleaner, avoiding duplicate registry entries and double-cleaning.
*   **Faster Size Accounting**: Clean app/cache cleanup now uses `get_size_fast()` and passes `known_size_bytes` into `safe_remove()`, avoiding repeated Python directory scans for the same deletion target.
*   **Rule Definition Cleanup**: Browser path definitions use `_xdg_browser_roots()` and `_home_browser_roots()` helpers, reducing repetitive `.config/foo` / `.cache/foo` path pairs while keeping the same behavior.

### 3. App Removal List Ordering
*   **Default Sort Changed**: The `Select Application to Remove` view now defaults to sorting apps by install time instead of size, so recently installed apps appear first.
*   **Sort Stability**: Missing install times are handled safely, and size is used as a secondary sort key when install times are equal.
*   **Tests Updated**: Added and updated navigator/uninstall tests to verify install-time ordering in the UI and scanned package list.

### 4. Output Polish
*   **Analyze Progress Text**: Replaced the separate `Analyzing Linux Insights...` message with a more consistent Rust-engine progress line: `Rust Engine: Analyzing Linux insights, please wait . . .`.
*   **Status Icon**: Updated the `Top Processes` status output icon from `🚀` to `🔝` to better match the meaning of the line.

### 5. Regression Coverage
*   **Whitelist Coverage**: Added tests proving browser cache paths are cleanable across Chrome/Chromium/Brave/Edge/Vivaldi/Opera/Firefox/LibreWolf and Flatpak browser layouts.
*   **Credential Protection Coverage**: Added tests proving browser profile roots and credential/state files remain protected.
*   **File Removal Coverage**: Added tests proving `safe_remove()` can remove browser cache directories but refuses profile roots.
*   **Analyze Coverage**: Added tests proving selecting Chrome and Firefox profile roots cleans only cache children while keeping profile directories and login databases.
*   **Clean Browser Cache Coverage**: Added tests proving Clean discovers browser cache children, removes cache contents while keeping login databases, skips cleanup when a browser process is running, and reuses fast size results when calling `safe_remove()`.
*   **Navigator Coverage**: Added tests for the new install-time default sort in the uninstall selector.

### 6. Verification
*   **Focused Tests**: `pytest -q tests/test_whitelist_advanced.py tests/test_file_ops.py tests/test_analyze.py` passed with **56 tests**.
*   **Full Test Suite**: `pytest -q` passed with **234 tests**.
*   **Ruff Check**: `python -m ruff check` passed.
*   **Ruff Format**: `python -m ruff format --check` passed.
*   **Diff Hygiene**: `git diff --check` passed.

### 7. Follow-Up Notes
*   New browser support should usually be added by extending `BROWSER_DEFS` and, only when needed, `CLEANABLE_APP_CACHE_DIR_NAMES` in `src/core/browser_cache.py`; Clean, Analyze, and whitelist protection should not need browser-specific logic.
*   Whole-profile deletion should remain blocked in Analyze. If Topo later adds a dedicated browser-data cleanup workflow, it should expose explicit categories such as cache, cookies, history, and saved sessions instead of deleting profile roots.

---

# Daily Modification Report - 2026-06-06

## Project: topo (Topo) - GitHub Release Debian/RPM Distribution

Today's session completed the first package-manager distribution milestone: GitHub Releases can now attach `.deb` and `.rpm` installers while preserving the existing one-line `curl | bash` installer. The project now has two clear installation channels: script installs under `~/.topo`, and package-manager installs under `/usr/lib/topo` with `/usr/bin/topo` as the launcher.

### 1. Distribution Strategy
*   **Kept the existing installer**: `curl -fsSL https://raw.githubusercontent.com/Jesencloud/Topo/main/install.sh | bash` remains supported for users who prefer the current GitHub-based install/update flow.
*   **Added package-manager-ready layout**: Debian/RPM packages stage Topo into `/usr/lib/topo`, expose `/usr/bin/topo`, include the architecture-specific Rust engine, and keep user state/config under XDG paths.
*   **Unified lifecycle commands**: Users can use `topo update` / `topo remove` for both script installs and package installs. Topo chooses the correct implementation based on install source.

### 2. Install Source Detection
*   **Marker File**: Added `.topo-install-source` to distinguish `script` from `package` installs.
*   **Script Install Marker**: `install.sh` now writes `script` into the marker after fetching/refining the runtime tree.
*   **Package Install Marker**: The packaging script writes `package` into `/usr/lib/topo/.topo-install-source`.
*   **Runtime Guardrails**:
    *   Package-mode `topo update` no longer runs the GitHub installer; it checks the latest GitHub Release, downloads the matching `.deb` or `.rpm`, verifies it against `SHA256SUMS`, and upgrades it through `apt` or `dnf`.
    *   Package-mode `topo remove` no longer deletes `/usr/lib/topo`; it directly calls `apt remove -y topo` or `dnf remove -y topo`.
*   **Distro-Aware Package Commands**: Added package-manager command selection based on `/etc/os-release`:
    *   Ubuntu/Debian-family systems use `.deb` assets and `apt` commands.
    *   Fedora/RHEL-family systems use `.rpm` assets and `dnf` commands.
    *   Unknown systems fall back to showing both common command families.

### 3. Packaging Script
*   **New Script**: Added `packaging/build-linux-packages.sh`, powered by `fpm`.
*   **Generated Outputs**:
    *   `topo_${VERSION}_amd64.deb` - Debian/Ubuntu x86_64.
    *   `topo_${VERSION}_arm64.deb` - Debian/Ubuntu ARM64.
    *   `topo-${VERSION}-1.x86_64.rpm` - Fedora/RHEL x86_64.
    *   `topo-${VERSION}-1.aarch64.rpm` - Fedora/RHEL ARM64.
*   **Naming Note**: Debian uses `amd64`/`arm64`; RPM uses `x86_64`/`aarch64`. The RPM `-1` is the package release/iteration, not the Topo upstream version.
*   **Clean Staging**: The packaging script removes `__pycache__`, `.pyc`, `.pyo`, and `$py.class` files from the staged runtime tree so local test caches never enter release packages.
*   **Runtime Contents**: Packages include `topo`, `src/`, `VERSION`, bundled WAV assets, `LICENSE`, `README.md`, and exactly one matching `topo-core-$ARCH` binary.
*   **Runtime Dependencies**: Packages depend on `curl`, `python3`, and `python3-packaging` so package-mode `topo update` can query/download GitHub Release assets and compare versions.
*   **Package Install Compatibility Launcher**: Added package `after-install` / `after-remove` scripts. When a package is installed through `sudo`, the post-install script creates a managed `~/.local/bin/topo` compatibility launcher for the invoking user if that path is missing, a symlink, or already Topo-managed. This makes `topo` work immediately even when the current shell cached an older script-install launcher path.
*   **Package Remove Cleanup**: Verified that package removal deleted the RPM-owned files but could leave an empty `/usr/lib/topo` directory tree and user configuration under `~/.config/topo`. Updated package post-remove cleanup to remove empty `/usr/lib/topo` directories, and updated package-mode `topo remove` to remove Topo user config/cache/state, script-install remnants, and Topo-managed launchers after the package manager succeeds.
*   **Package Checksums**: The final release job now creates one compact `SHA256SUMS` manifest covering the raw engine binaries and all `.deb` / `.rpm` packages instead of uploading one `.sha256` file per asset. The manifest records final Release asset filenames, so users can verify downloads from one local directory.

### 4. GitHub Actions Release Integration
*   **Existing Engine Build Reused**: `.github/workflows/build-engine.yml` already cross-compiles `topo-core-x86_64` and `topo-core-aarch64`.
*   **Package Build Job**: The workflow now installs Ruby/RPM tooling in the dedicated `package` job, installs `fpm`, runs `packaging/build-linux-packages.sh`, and uploads `topo-linux-packages` as a workflow artifact for smoke testing and release upload.
*   **Release Asset Staging**: The release job now copies final user-facing files into `release-assets/` before checksum generation and upload. This keeps the GitHub Release attachment list compact because per-file `.sha256`, per-file `.asc`, and `.sha256.asc` sidecars are no longer uploaded.
*   **Checksum Uploads**: Release assets now include `SHA256SUMS` and `SHA256SUMS.asc` as first-party integrity checks for package downloads.
*   **Ubuntu Smoke Test Job**: Added `smoke-ubuntu`, which installs the generated `amd64` `.deb` with `sudo apt install`, simulates a stale Bash command cache for `~/.local/bin/topo`, checks `topo --version`, verifies `/usr/lib/topo/.topo-install-source`, confirms `topo update` uses the package-mode update path, and confirms `topo remove` removes the package through apt.
*   **Fedora Smoke Test Job**: Added `smoke-fedora`, which runs inside a `fedora:44` container, installs the generated `x86_64` `.rpm` with `dnf install`, checks `topo --version`, verifies the install-source marker, confirms `topo update` uses the package-mode update path, and confirms `topo remove` removes the package through dnf.
*   **Release Gate**: The release job now depends on both smoke-test jobs, so `.deb` / `.rpm` assets are attached only after package installation checks pass.
*   **GPG Detached Signatures**: The release job now imports a maintainer-provided GPG key from `GPG_PRIVATE_KEY`, signs the `SHA256SUMS` manifest with a detached armored `SHA256SUMS.asc` signature, then uploads the manifest and signature alongside the assets.
*   **Required Release Secrets**: GPG signing requires GitHub Secrets `GPG_PRIVATE_KEY` and `GPG_PASSPHRASE`; both secrets were configured for the repository after the release key was generated.
*   **Public Release Key**: Added `assets/topo-release-public.asc` and configured the release workflow to upload it as a Release asset. Fingerprint: `4B35 C17C F8E6 6373 2726  A99F 5008 6DB9 98B4 D883`.
*   **Release Notes Gate Preserved**: Tag releases still require `docs/releases/${TAG}.md` before assets are attached.

### 5. Local Command Usage
*   **Install local packaging tools on Fedora**:
    ```bash
    sudo dnf install -y ruby ruby-devel gcc make rpm-build redhat-rpm-config dpkg
    sudo gem install --no-document fpm
    ```
*   **Verify tooling**:
    ```bash
    fpm --version
    rpmbuild --version
    dpkg-deb --version
    ```
*   **Build local packages**:
    ```bash
    packaging/build-linux-packages.sh
    ```
*   **Reinstall a local DEB after packaging-code changes**:
    ```bash
    sudo apt install --reinstall ./topo_0.9.4_amd64.deb
    ```
*   **Install or reinstall a local RPM after packaging-code changes**:
    ```bash
    sudo dnf install ./topo-0.9.4-1.x86_64.rpm
    sudo dnf reinstall ./topo-0.9.4-1.x86_64.rpm
    ```
*   **Build with an explicit ARM64 engine**:
    ```bash
    packaging/build-linux-packages.sh \
      --aarch64-engine /path/to/topo-core-aarch64
    ```
*   **Inspect RPM metadata and contents**:
    ```bash
    rpm -qip dist/packages/topo-0.9.4-1.x86_64.rpm
    rpm -qlp dist/packages/topo-0.9.4-1.x86_64.rpm
    ```
*   **Inspect DEB metadata and contents**:
    ```bash
    dpkg-deb -I dist/packages/topo_0.9.4_amd64.deb
    dpkg-deb -c dist/packages/topo_0.9.4_amd64.deb
    ```
*   **Check for accidental Python cache files**:
    ```bash
    rpm -qlp dist/packages/topo-0.9.4-1.x86_64.rpm | rg '__pycache__|\.pyc|\.pyo|\$py\.class'
    dpkg-deb -c dist/packages/topo_0.9.4_amd64.deb | rg '__pycache__|\.pyc|\.pyo|\$py\.class'
    ```
*   **Verify package checksums**:
    ```bash
    sha256sum -c SHA256SUMS
    ```
*   **Verify the checksum manifest GPG signature**:
    ```bash
    curl -fsSLO https://github.com/Jesencloud/Topo/releases/latest/download/topo-release-public.asc
    gpg --import topo-release-public.asc
    gpg --fingerprint "Topo Release"
    gpg --verify SHA256SUMS.asc SHA256SUMS
    ```

### 6. Verification
*   **Shell Syntax**: `bash -n install.sh`, `bash -n packaging/build-linux-packages.sh`, `bash -n packaging/scripts/after-install.sh`, and `bash -n packaging/scripts/after-remove.sh` passed.
*   **Python Quality**: `ruff check .` and `ruff format --check .` passed.
*   **Test Suite**: `env HOME=/tmp/topo_pytest_home pytest -q` passed with **223 tests**.
*   **Local Package Build**: Confirmed all four local package files exist in `dist/packages/`.
*   **Package Hygiene**: Verified RPM and DEB contents no longer include `__pycache__` or `.pyc` files.
*   **Distro-Aware Package Lifecycle Tests**: Verified package-mode `topo update` / `topo remove` behavior with focused tests for Ubuntu/Fedora/unknown systems, including architecture-aware GitHub Release package filenames, SHA256 verification, direct package upgrade commands, and direct package removal commands.
*   **GitHub Latest Release Fallback**: Hardened `topo update` against GitHub API 403 responses by adding GitHub API request headers and falling back to the `/releases/latest` redirect URL when the API request fails.
*   **Quiet GitHub API Fallback**: Real package installs can still hit intermittent GitHub API 403 responses. The fallback path already succeeds, but curl's stderr leaked `curl: (22) ... 403` before the successful "already up to date" message. The latest-release probe now suppresses recoverable curl stderr and lets Topo print only the final user-facing result.
*   **Launcher Cleanup Regression**: Added coverage so package-mode removal ignores arbitrary binary user launcher files instead of trying to decode or delete them.
*   **Regenerated Packages**: Rebuilt all four local packages after package lifecycle fixes so Ubuntu/Fedora installs receive the corrected package-mode behavior.
*   **Package Metadata Checks**: Verified regenerated DEB/RPM packages depend on `curl`, `python3`, and `python3-packaging`, include package post-install/post-remove scripts, and leave `dist/packages` with only the four user-facing `.deb` / `.rpm` files.
*   **Checksum Verification**: Release-time checksum verification now uses a single `sha256sum -c SHA256SUMS` manifest with Release asset filenames instead of internal CI paths.
*   **Workflow Syntax**: Parsed `.github/workflows/build-engine.yml` locally with Ruby YAML loading after adding package smoke-test jobs and the compact `release-assets/` upload flow.
*   **Ubuntu Smoke Test Hardening**: The first v0.9.4 tag run failed in the Ubuntu DEB smoke step because the test assumed the package maintainer script would always create `~/.local/bin/topo` for the CI user. That user cannot be inferred reliably in every apt/dpkg environment. The smoke test now validates `/usr/bin/topo` and the package marker first, then verifies the compatibility launcher only when the post-install script created it, while still running `topo --version`, `topo update`, and `topo remove`. The follow-up run showed the DEB removal assertion was also too strict: `apt remove` can leave a dpkg package record even when the package is no longer installed. The smoke test now checks that Topo is not in `install ok installed` state instead of requiring `dpkg-query -W topo` to fail.
*   **README Install Docs**: Updated the README Quick Start section with script install and GitHub Release `.deb`/`.rpm` install commands. Integrity and GPG verification details stay in this development report for now.
*   **v0.9.3 Release Notes**: Consolidated the exploratory v0.9.1 and v0.9.2 package-release notes into `docs/releases/v0.9.3.md` as the formal Debian/RPM package distribution release note, then removed the old v0.9.1/v0.9.2 release-note files.
*   **v0.9.4 Release Notes**: Added `docs/releases/v0.9.4.md` for the package lifecycle hardening release.
*   **Obsolete Asset Removal**: Confirmed `assets/topo_home.png` is no longer referenced by the current README or release notes, so it is intentionally removed in the v0.9.3 release commit.
*   **Shell Command Cache Guidance**: Added install/link messaging that tells users to run `hash -r` or reopen the terminal if a previous package install left the current shell trying a stale `/usr/bin/topo` command path.
*   **Script-vs-Package Migration Warning**: Updated `install.sh` to detect an already-registered RPM/DEB Topo package during script installation and tell users to remove the package install first, preventing `/usr/bin/topo` from shadowing the script install under `~/.local/bin/topo`.

### 7. Future Work
*   **Automatic GPG Verification**: `topo update` currently verifies downloaded packages with `SHA256SUMS`; the next hardening step is automatic verification of `SHA256SUMS.asc` with the Topo release public key.
*   **Package Stability Window**: Keep using GitHub Release `.deb`/`.rpm` packages for several stable versions before building first-party APT/DNF repositories, so install/upgrade/remove behavior and release automation can mature under real usage.
*   **Create First-Party APT/DNF Repositories**: After GitHub Release packages are stable, publish repository metadata so users can run `sudo apt install topo` or `sudo dnf install topo` after adding the Topo repo.
*   **Long-Term Official Repo Path**: Later evaluate Debian/Fedora official inclusion for higher user trust, enterprise acceptance, and native distro update workflows.
*   **Standardize Python Packaging**: Consider converting the runtime to a standard Python package with `[project]` metadata and console entry points once the first release-package path is stable.

---

# Daily Modification Report - 2026-06-05

## Project: topo (Topo) - Unified Aesthetics & Deep System Cleanup

Today's session focused on unifying the visual theme, implementing low-level system optimization tasks, hardening the test suite against environment-specific failures, and — driven by a white-box security audit of the deletion core — closing a prioritized set of deletion-safety findings (uninstall residue, sudo deletion, symlink amplification).

### 1. Unified Theme & Visual Polish
*   **Centralized Theme Constant**: Introduced `THEME_TITLE` in `src/core/constants.py` (defaulting to Bright Purple `\033[1;95m`). This replaces various hardcoded ANSI sequences and ensures consistent branding across all TUI and CLI components.
*   **TUI Consistency Sweep**: Updated all selectors (`InteractiveMenu`, `AnalyzeSelector`, `UninstallSelector`, `TopFilesSelector`) to use `THEME_TITLE` for their headers. Added leading spaces to aligned headers for a more balanced viewport.
*   **Consistent Bullet Points**: Standardized selection summaries (☉) and bullet points (•) to use the theme color across all modules.
*   **Icon Modernization**: Replaced the standard folder emoji `📁` with the more professional card index dividers `🗂️` throughout the codebase, including the interactive analyzer and the `README.md` examples.

### 2. Deep System Cleanup (Package & Process)
*   **Orphaned Package Removal**: Implemented `clean_orphaned_packages()` with cross-distro support:
    *   **DNF**: `dnf autoremove` for Fedora/RHEL.
    *   **APT**: `apt-get autoremove` for Ubuntu/Debian.
    *   **Pacman**: Two-stage removal using `pacman -Qtdq` and `pacman -Rns` for Arch.
*   **Zombie Process Reaping**: Added `clean_zombies()` to detect processes in the `Z` (defunct) state. Instead of simple detection, it now signals the parent processes via `SIGCHLD` to trigger standard Unix reaping, reclaiming system PID resources safely.
*   **Runner Integration**: Integrated both tasks into the "System & Package Manager" category in `run_clean()`, with full support for `--dry-run` previews and accurate space-freed parsing.

### 3. Code Quality & Linting
*   **Idiomatic Error Handling**: Resolved Ruff `SIM105` violation in `app_manager.py` by replacing a `try-except-pass` block with the more readable `contextlib.suppress(OSError)`.
*   **Import Optimization**: Centralized `contextlib` imports at the top level and removed redundant local imports across the application management module.

### 4. Test Robustness & Compatibility
*   **Precise Stat Mocking**: Fixed a `TypeError` in `test_clean_path_by_age` reported on Ubuntu by switching from `MagicMock` to a real `os.stat_result` object for file age simulation. This ensures compatibility with Python 3.14+ type checking.
*   **Environment Isolation**: Fixed `test_clean_podman` by routing it through the `test_env` fixture and mocking `Path.exists`, preventing failures caused by the presence (or absence) of local container caches.
*   **Error Handling Verification**: Refined `test_get_size_error_handling` to mock `Path.exists` returning `True` alongside `stat` failures, accurately testing the recursive error recovery logic.
*   **Regression Coverage**: Added 8 new test cases covering orphaned package detection (DNF/APT/Pacman) and zombie process signaling.

### 5. [Security] White-Box Audit — Uninstall & Deletion Hardening
A focused white-box review of the deletion core (`whitelist`/`file_ops`/`analyze` + every `clean/*` module + the Rust engine) drove a prioritized fix pass. Each fix ships with regression coverage; the existing defense-in-depth (centralized `validate_path_for_deletion()`, hard-protection list, list-form command execution with no `shell=True`, read-only Rust engine) was confirmed intact.

#### [High] H1 — Uninstall residue matching could permanently delete a user workspace
*   **Root Cause**: `find_residue_paths()`'s home-root "deep search" fuzzily matched **visible** top-level home folders by the app's display name. An app with a common name (e.g. "Notes", "Studio") prefix-matched `~/notes-backup`, `~/studio-projects`, etc.; `execute_uninstall()` then removed them via `safe_remove(use_trash=False, allow_app_data_removal=True)` — a **permanent, non-recoverable** wipe of directories that sit outside every protection list.
*   **Fix (recoverable)**: Residue removal now uses `use_trash=True`, so a heuristic mis-match is recoverable from the trash. Hard protection (whitelist, credentials, XDG user-data dirs) still applies.
*   **Fix (scope)**: The home-root deep search now considers **only hidden dot-directories** (`~/.someapp`); visible workspaces (`~/Projects`, `~/IdeaProjects`, ...) are never eligible as residue. The `search_roots` substring contract (e.g. `~/.local/share/vendor-myapp-state`) is preserved unchanged.
*   **Fix (visibility)**: The uninstall preview now flags home-root paths in yellow with a `⚠` marker so the user reviews them before confirming.
*   **Regression Coverage**: Added `test_find_residue_paths_skips_visible_home_workspace` (visible workspace excluded while a hidden dotdir is still cleaned) and `test_execute_uninstall_residue_goes_to_trash`.

#### [Med] M1 — `sudo rm -rf` validated and executed on different paths
*   **Root Cause**: Analyze's `_sudo_remove()` validated the symlink-*resolved* path but handed the raw (unresolved) string to `rm -rf` under sudo — it could validate path A yet delete path B.
*   **Fix**: It now resolves once up front and runs validation, the existence check, the size read, and `rm -rf` all against that single `target_path`.
*   **Regression Coverage**: `test_sudo_remove_operates_on_resolved_path` (the resolved real directory is deleted, never the raw symlink path).

#### [Med] M2 — Proactive detection could amplify deletion through symlinks
*   **Root Cause**: `proactive_app_detection()` resolved a `~/.cache/<cmd>` symlink to its target and registered the target for cleanup, so `clean_app_generic()` could wipe the contents of an out-of-tree directory the link pointed at.
*   **Fix**: Detection now skips symlinks entirely, managing only real directories that physically live under the scanned roots.
*   **Regression Coverage**: `test_proactive_app_detection_skips_symlinks`.

#### [Low] L1 — Audit-log line forging
*   **Root Cause**: A target rejected *for* containing control characters was still written verbatim to the deletion audit log; an embedded newline could forge a second record and a tab could shift the column layout.
*   **Fix**: `record_deletion_audit()` now escapes `\`, `\n`, `\r`, `\t` before writing. Added `test_record_deletion_audit_escapes_control_chars`.

#### [Low] L3 — Import-style consistency
*   `clean/system.py` used absolute `from src.core...` imports (coupling the package name to `src`) while every other module uses relative imports; switched it to `from ..core...` to match.

#### [Low] L2 — Self-update validates the downloaded installer before executing
*   `run_update()` piped the downloaded `install.sh` straight into `bash`. The tag-pinned HTTPS URL already fixes the content for an untampered repo, but a CDN/error page or truncated body would still be executed. It now refuses any payload that does not begin with a `#!` shebang. Added `test_run_update_rejects_non_script_payload`.

#### [Low] L4 — Project purge no longer treats every `bin/` as a build artifact
*   `PURGE_TARGETS` includes `bin` (.NET output) but `scan_artifacts()` had no guard, so any project's `bin/` (scripts, vendored binaries) was purgeable despite the "(guarded)" comment. `bin/` is now collected only when a .NET project file (`.csproj`/`.sln`/`.fsproj`/`.vbproj`) sits beside it. Added `test_scan_artifacts_bin_requires_dotnet_project`.

#### [Low] L5 — Analyze reveals `.desktop` files instead of launching them
*   Opening a file in Analyze sent non-archive / non-executable files straight to `xdg-open`. A `.desktop` entry can launch arbitrary actions that way, so `.desktop` files are now treated like executables and open their parent directory instead.

#### [Low] L6 — Public-IP lookup uses HTTPS
*   The opt-in `topo status` public-IP lookup queried `http://ip-api.com` in cleartext. Switched to `https://ipinfo.io/json` (HTTPS), preserving the `[CC] IP` output format. Updated the status test for the new endpoint fields.

#### [Cleanup] N1 — Removed the misleading `bypass_whitelist` parameter
*   `safe_remove()` / `validate_path_for_deletion()` carried a `bypass_whitelist` keyword that was merely an alias for `allow_app_data_removal` and never actually bypassed the user whitelist (which lives in hard protection). With no remaining callers, it was removed in favour of the single, accurately-named `allow_app_data_removal`.

### 6. Verification
*   **Ruff**: `ruff check src tests` — **All checks passed** (incl. `ruff format`).
*   **Pytest**: `pytest` — **193 passed** (186 prior + 7 new security regressions across both fix batches).
*   **CLI**: Verified `./topo link` and `./topo remove` behavior for installation management.

### 7. CLI Help Modernization & Dry-Run Command Routing
*   **Modern Help Layout**: Reworked the top-level `topo --help` output into a clearer `Quick Start` / `Whitelist` / `Notes` structure, with concise examples and command-specific follow-up guidance.
*   **Stable Program Name**: Set `argparse`'s `prog` to `topo`, so help output is consistent whether launched through `./topo` or `python -m src.main`.
*   **Subcommand Help**: Added focused help text and examples for `clean`, `optimize`, `purge`, `all`, `remove`, `history`, and `whitelist`.
*   **Dry-Run Placement**: Removed global `--dry-run` support. Preview flags now live only on the commands that actually support them, so `topo clean --dry-run` works and `topo --dry-run clean` is rejected.
*   **Dry-Run Default Fix**: Fixed the `Namespace` crash in `topo all` and other non-preview invocations by reading `dry_run` with a safe default of `False`.
*   **Whitelist CLI Polish**: Removed duplicated handwritten usage text from normal whitelist output, improved metavar/help labels, and switched missing-path errors to standard `argparse` errors.
*   **README Help Refresh**: Updated the README's run/help section to match the new CLI help output and replaced the old home screenshot reference with the current terminal menu example.

### 8. Sudo Prompt Key Handling
*   **Strict Key Acceptance**: Updated the clean and optimize sudo pre-prompts so only `Enter`, `Space`, and standalone `ESC` are accepted. Other keys are ignored instead of falling through to password input.
*   **Escape Sequence Safety**: Direction keys and other terminal escape sequences are consumed and ignored, so they are not mistaken for an `ESC` cancellation.
*   **Clean Skip Semantics Preserved**: `Space` at the clean sudo prompt still skips the current clean flow rather than continuing with non-sudo cleanup tasks, avoiding misleading behavior.

### 9. Follow-Up Regression Coverage
*   **CLI Help Tests**: Added `tests/test_cli_help.py` to lock the top-level help and whitelist help wording that documents manual protection rules.
*   **Sudo Choice Tests**: Added `tests/test_sudo_choice.py` to verify unrecognized keys are ignored, escape sequences are consumed safely, and `Space` skips clean without sudo authorization or follow-on cleanup execution.
*   **Focused Verification**: Re-ran `tests/test_cli_help.py`, `tests/test_sudo_choice.py`, `tests/test_clean_system.py`, and `tests/test_clean_user.py`, plus Python bytecode compilation for the edited modules.

---

# Daily Modification Report - 2026-06-04

## Project: topo (Topo) - Security & Correctness Bug-Fix Sweep

Today's session was a comprehensive bug-fix pass driven by a full code audit of the Python (`src/`) and Rust (`topo-core/`) sources. Every finding was re-verified against the source before fixing, and audit false-positives were discarded. Fixes are grouped by severity, each with regression coverage where practical.

### 1. [Critical] Uninstall no longer deletes XDG user-data directories
*   **Root Cause**: `UninstallManager.find_residue_paths()` ran a top-level "deep search" of the home directory and matched folders by the app's *display name*. An app whose name is a common word (e.g. GNOME **Music** → `org.gnome.Music`, or **Videos**/Totem) matched `~/Music`, `~/Videos`, etc. `execute_uninstall()` then passed these to `safe_remove(..., use_trash=False, allow_app_data_removal=True)`, which **permanently deleted** them — and `get_hard_protection_reason()` only shielded credential dirs (`.ssh`/`.gnupg`/`.aws`/...), not standard user data.
*   **Fix (source)**: The deep search now skips standard XDG user-data directory names (`Desktop`, `Documents`, `Downloads`, `Music`, `Pictures`, `Public`, `Templates`, `Videos`), so they are never treated as app residue.
*   **Fix (defense in depth)**: Added `LINUX_USER_DATA_DIRS` to `whitelist.py` and taught `get_hard_protection_reason()` to protect each of these directories **as an exact path** ("user data directory"). This blocks wiping the whole directory in any deletion context — including uninstall — while files *inside* them remain deletable via Analyze, so disk cleanup still works.
*   **Regression Coverage**: Added tests proving `find_residue_paths("org.gnome.Music", "Music", ...)` never returns `~/Music`, that `safe_remove(~/Music, allow_app_data_removal=True)` is refused while `~/Music/song.mp3` is still removable, and that these directories report `is_protected() is True` while their children do not.

### 2. [High] Uninstall reports accurate results, terminates processes reliably, and is significantly faster
*   **Truthful Summary**: `run_uninstall()` ignored `execute_uninstall()`'s return value, so it counted every app as "Removed" and added its install size to "freed" even when `dnf/apt/... remove` failed (e.g. sudo denied). `execute_uninstall()` now returns `{"package_removed": bool, "removed_paths": [...]}`, and `run_uninstall()` only counts space for packages that actually uninstalled, listing the rest under a new `✗ Failed:` line.
*   **Reliable Process Termination**: Process killing built its target list from `app["name"].lower()` — the *localized display name*. A name like "Telegram Desktop" (with a space) can never match `pkill -x`, so GUI apps were never terminated (leaving file handles open), while a short display name risked killing an unrelated process. Added `_candidate_process_names()` / `_executable_names_from_desktop()`, which derive real `comm` names from the package/flatpak id and the `.desktop` `Exec` line. Both the preview "running" check and the kill step now use them.
*   **Faster Execution & Real-time Feedback**: Optimized the process termination loop by batching `pkill` commands, significantly reducing per-app delays. Removed the slow `--autoremove` flag from the per-app `apt purge` loop, running it only once at the end of the uninstallation batch. Added real-time visual feedback (`Removing [App Name]...`) to keep the user informed during the processing phase.
*   **Regression Coverage**: Added tests that the process-name candidates exclude the display name, that `.desktop` Exec names are parsed, and that a failed package removal reports `Removed 0 app(s)` + `Failed:` instead of phantom freed space. Updated the six `execute_uninstall` tests for the new return shape and optimized `apt` command.

### 3. [High] Age-based cleanup is symlink-safe and no longer aborts midway
*   **No Mid-Sweep Abort**: `clean_path_by_age()` wrapped its whole per-item loop in a single `try/except OSError`, so one broken symlink (whose `stat()` raised) aborted the *entire* directory sweep, silently skipping every remaining item. The `try` is now per-item with `continue`.
*   **Symlink-Safe & noatime-Aware**: It judged age via `item.stat().st_atime`, which (a) *follows* symlinks (reading the target's time, not the link's) and (b) ignores `noatime`/`relatime` mounts where atime barely updates, risking deletion of still-active data. It now uses `item.lstat()` and keeps an entry if *either* atime or mtime is recent — matching `clean_system_temp`.
*   **Bare-Byte Size Parsing**: `parse_size_to_bytes()` returned 0 for a unit-less numeric string; it now treats a wholly-numeric value as raw bytes (without misreading stray digits in command output).
*   **Regression Coverage**: Updated `test_clean_path_by_age` to mock `lstat` (atime + mtime); added bare-byte parsing assertions.

### 4. [Med] Trash fallback empties the real trash, safely
*   `clean_trash()`'s manual fallback used a literal `Path("/tmp/trash-$USER")` — `$USER` was never expanded, so it never matched a real trash dir; it deleted via `shutil.rmtree(ignore_errors=True)` (bypassing protection/audit) yet still added the size to the freed total even on failure.
*   It now targets `~/.local/share/Trash` and `/tmp/.Trash-<uid>` (real UID), removes them through `safe_remove(use_trash=False)`, recreates the empty dir, and counts space only when removal actually succeeds. Added a fallback regression test.

### 5. [Med] SQLite vacuum no longer leaks connections on error
*   `vacuum_single_db()` called `conn.close()` only on its success paths; if any `PRAGMA`/`VACUUM` raised `sqlite3.Error`, the outer `except` returned 0 with the connection (and its file handle) still open — leaking one per corrupt/locked DB across a full browser-database sweep. The connection is now wrapped in `contextlib.closing()`, so every path (success, early-return, exception) closes it. Added a regression test asserting `close()` is called on error.

### 6. [Med] Safer self-update (`topo update`)
*   `_fetch_latest_release_tag()`'s `curl` had no timeout, so a hung network blocked `topo update` indefinitely — added `timeout=15`.
*   The update ran `curl … | bash -s -- … --version {remote_tag}` via `subprocess.run(shell=True)`, interpolating the GitHub release tag into a shell command line. Now the tag is validated against a strict pattern (rejecting anything with shell metacharacters/whitespace even if PEP 440 accepts it, e.g. epoch tags `1!2.3`), the installer is fetched with a plain `curl` argv list (`timeout=30`), and executed via `bash -s` stdin with the tag passed as a separate argv element — no `shell=True`, no command-line interpolation.
*   **Regression Coverage**: Updated the install test for the shell-free flow and added a test that an unsafe tag is refused before any download/execution.

### 7. [Low] Accurate cleanup accounting
*   **Tool caches**: `clean_tool_cache()` reported the *pre-clean* directory size as freed whenever the command exited 0, overstating reclaimed space when `npm/pip/go cache clean` only partially clears. It now reports `before - after` (or the full size if the directory was removed entirely).
*   **Snap revisions**: `clean_package_manager()` discarded the item/category counts returned by `clean_snaps()` (it kept only the freed bytes, which are always 0 for snaps), so removed old revisions never showed in totals. Those counts now flow through.
*   **Regression Coverage**: Added tests for actual-freed reporting and for snap-revision stats reaching the package-manager totals.

### 8. [Low] Cleaner install / uninstall self-management
*   **PATH detection**: `run_install_link()` checked `str(target_dir) not in path_env` — a substring test, so `~/.local/bin` was wrongly considered present when PATH held something like `~/.local/bin-foo`. It now splits PATH on `os.pathsep` and compares entries.
*   **Atomic link**: the launcher symlink was created via `unlink()` then `symlink_to()`; an interruption between them removed the command. It is now built via a temp symlink + `os.replace()` (atomic).
*   **Thorough removal**: `topo remove` now also deletes the deletion-audit/state dir (`$XDG_STATE_HOME/topo`), strips the `# Added by topo` PATH block from `~/.bashrc`/`~/.zshrc`, and recognizes a *dangling* launcher symlink (the previous `resolve()`-based check skipped links whose target was already gone).
*   **Regression Coverage**: New `test_remove.py` covers rc-line stripping (and no-op without the marker) and dangling-link detection.

### 9. [Low] Assorted robustness
*   **TUI empty-list guard**: `CleanSelector.run()` (and `InteractiveMenu.run()`) lacked the empty-items guard the other selectors have, so an empty list + an arrow key hit `% len([])` → `ZeroDivisionError`. Both now return immediately when empty.
*   **Whitelist CLI exit codes**: `topo whitelist add/remove` without a path, or `remove` of an absent path, now exit non-zero instead of always 0 (so scripts can detect failure).
*   **Battery health**: clamped to 100% (new batteries report `energy_full > energy_full_design`, which previously displayed e.g. "Health: 103.4%").
*   **Multi-GPU status**: `get_gpu_info()` split the entire `nvidia-smi` output on `", "`; with more than one GPU this raised and silently returned no GPU info. It now parses the first line.
*   **Passwordless-sudo rule**: `setup_passwordless_sudo()` used `$USER` (wrong under sudo) and would emit a broken sudoers rule for paths containing spaces. It now uses the real invoking user (`SUDO_USER`) and refuses to emit an unsafe rule.
*   **Regression Coverage**: Added tests across `test_navigator.py`, `test_status.py`, and `test_system.py`.

### 10. [Low] Rust engine: overflow-safe sizes & consistent threshold
*   File-size and file-count aggregation used plain `+=` on `u64`, which panics in debug builds and silently wraps in release on overflow (e.g. pathological/corrupt metadata on an enormous tree). All accumulations in `compute_single`/`compute_tree` now use `saturating_add`.
*   The top-files cutoff was a literal `1_000_000` while the comment and the tree threshold use 1 MiB; introduced `TOP_FILE_MIN_BYTES = 1_048_576` so single- and tree-mode thresholds agree.
*   `cargo test`: 7/7 engine tests still pass.

### 11. [Low] Navigation Auditory Feedback Mute Toggle
*   **Mute Functionality**: Implemented a global mute toggle for navigation sound effects. Users can now press **`M`** in the Main Menu to turn sound effects on or off.
*   **Dynamic UI Prompt**: The Interactive Menu now dynamically displays `M: Mute` or `M: Unmute` based on the current state.
*   **Persistent State**: The mute setting is maintained as a class attribute in `Navigator`, ensuring consistent behavior across all selectors (Analyze, Uninstall, etc.) within the session.

### 12. Verification
*   `ruff check src tests` — **All checks passed**.
*   `pytest` — **183 passed** (full suite, including the new regression tests added throughout this report).
*   `cargo test` (topo-core) — **7 passed**.
*   Each fix above was committed individually on branch `bugfix/audit-fixes-2026-06-04` (not pushed).

## Responsive UI & Mouse Wheel Support

### 13. Responsive Terminal Resize
*   **Dynamic Size Polling**: Updated `Navigator._read_key` to poll for terminal size changes using `select.select` with a 50ms timeout. The loop now returns a `RESIZE` signal immediately when a change is detected, allowing the UI to re-render and keep the scrollbar attached to the right edge during window dragging.
*   **Viewport Artifact Clearing**: Integrated the `\033[J` (Clear from cursor to end of screen) command at the end of `_write_scrollable_frame`. This ensures that any visual artifacts remaining below the viewport after a terminal height reduction are completely wiped.

### 14. Mouse Wheel Interaction
*   **Vertical Wheel Scrolling**: Implemented native mouse wheel support in `_handle_scrollbar_mouse`. Users can now scroll through any scrollable list by 3 lines per wheel notch.
*   **Viewport-Wide Trigger**: Wheel scrolling works anywhere within the active viewport when a scrollbar is visible, providing a fluid navigation experience across all analysis and uninstallation views.

### 15. Scrollbar Visual Refinement
*   **Minimalist Characters**: Standardized the scrollbar to use the single-column linear characters `┃` (thumb) and `│` (track). The full block character `█` is no longer used for the scrollbar.
*   **Theme Inheritance**: Forced the use of the `RESET` ANSI sequence for scrollbar rendering, ensuring it inherits the user's terminal theme colors instead of using hardcoded bright white or gray.

### 16. Analyze Selection Layout Optimization
*   **Two-Column Summary**: Updated the "Selected Items to Remove" list in the Analyze Disk view to use a space-efficient two-column layout. Each row now displays up to two items with their respective icons, providing a more compact and readable summary of the removal queue.
*   **In-TUI Confirmation (Restored)**: Successfully integrated the deletion confirmation prompt directly into the Analyze TUI. After selecting items, the first `Enter` press now triggers a professional status line (item count and total size) immediately below the selected items list. A **second `Enter`** is required to confirm deletion, while `Space` or `Esc` cancels the action. This ensures a safe, visually anchored, and structural confirmation workflow within the TUI.

### 17. High-Quality Auditory Feedback
*   **Dual-Sound Support**: Implemented a comprehensive audio feedback system with distinct sounds for different actions.
    *   **Navigation Click**: Uses `assets/cli_click.wav` for cursor movement.
    *   **Action Completion**: Uses `assets/delete_remove.wav` for the successful completion of destructive actions like file deletion or app uninstallation.
*   **Context-Aware Timing**: Refined sound triggers to play only upon **task completion** rather than confirmation. To avoid excessive noise during frequent use, **One-Key Clean is now silent** upon completion, while manual file deletions (Analyze) and app uninstallations continue to provide the distinct `delete_remove.wav` auditory feedback to signal full execution.
*   **Custom Sound Priority**: Upgraded `Navigator.play_click()` and `Navigator.play_delete()` to prioritize user-provided WAV files at `~/.config/topo/sounds/` (`click.wav` and `delete.wav`), followed by the bundled assets.
*   **Removed GTK Dependency**: Removed the fallback to Linux standard system sounds (`canberra-gtk-play`) to ensure the application maintains its own signature sound profile.
*   **Asynchronous Playback**: Implemented playback using non-blocking `subprocess.Popen` with `pw-play`, `paplay`, or `aplay` to ensure zero impact on TUI responsiveness.
*   **Installation Preservation**: Updated `install.sh` to preserve the `assets/` directory during installation while still pruning non-essential images, ensuring the bundled WAV files remain available in the deployed `~/.topo` directory.
*   **Graceful Fallback**: Maintains a reliable fallback to the standard terminal bell (`\a`) if no audio players or files are available.

### 18. Copy-Paste Compatibility (Main UI)
*   **Selective Mouse Tracking**: Modified `_selector_session` to allow optional mouse tracking. Disabled mouse tracking for the **Main Menu** and **Confirm Dialog**, ensuring that users can select and copy text (like the banner or paths) using their terminal's standard mouse behavior without needing to hold `Shift`.
*   **Preserved Interaction**: Kept mouse wheel and dragging support enabled for high-density, scrollable views (Analyze, Uninstall, Clean) where scrolling is a priority.

### 19. Snap Data Relocation (Ubuntu Optimization)
*   **Snap Cache Cleanup**: Implemented `clean_snap_cache` in the Clean module. It proactively scans `~/snap/*/common/.cache` for application-specific caches. The logic has been refined to use a **0-day age threshold** (cleaning all cache files) and includes a **running process check** to ensure safety. To keep the execution log clean, it only reports entries where space was actually reclaimed (`> 0 B`), eliminating confusing "(0 B)" reports.
*   **Insight Relocation**: Removed "Snap Data" from the Analyze Disk "Insights" list. By moving it to the Clean module, it transitions from a view-only indicator to a functional maintenance task, keeping the Analyze root view focused on unhandled data.

### 20. Accurate Installation Timestamps for Ubuntu/Debian
*   **APT Package Time Detection**: Fixed an issue where all `apt`/`dpkg` packages showed "Unknown" install times in the uninstaller. Since `dpkg-query` does not expose installation dates natively, the scanner now retrieves the modification time (`mtime`) of each package's file list (`/var/lib/dpkg/info/<package_name>.list`). This provides a highly accurate and performant way to display when a package was installed or last upgraded.

### 21. System Optimization Tasks
*   **Smart Swap Management**: Re-implemented the intelligent swap reset logic. It now monitors `/proc/meminfo` and safely executes `swapoff -a && swapon -a` only when available RAM is at least twice the used swap size, eliminating micro-stutters.
*   **Aggressive Journal Maintenance**: Added a maintenance task to vacuum systemd journals to 3 days (`--vacuum-time=3d`), ensuring the logs remain compact without losing recent history.
*   **System Coredump Cleanup**: Integrated a task to clear system coredumps via `journalctl --vacuum-coredump=0`, reclaiming space from historical crash dumps.
*   **Broken Symlink Removal**: Added a user-level cleanup for broken symbolic links in common directories (`~/.local/bin`, `~/Desktop`, `~/Documents`).

# Daily Modification Report - 2026-06-03

## Project: topo (Topo) - Flicker-Free Analyze Navigation & Whole-Subtree Scan Cache

Today's session eliminated the vertical jitter that appeared when paging and drilling in the Analyze disk explorer, and made directory navigation scan-free by caching an entire subtree from a single Rust engine pass.

### 1. Analyze Paging/Drill Jitter Fixes
*   **Cache-Hit Skips Scan Screen**: `run_deep_analysis()` now loads directly from `ScanCache` on a hit instead of repainting the scan header, so drilling back into a previously visited directory no longer blanks and vertically shifts the view.
*   **Scan Header Alignment**: Aligned the scan header so its title sits on the same row as `AnalyzeSelector.render()` (home, one blank line, then the title), removing the one-row vertical jump when the scan screen handed off to the result list.
*   **Padded-Page Investigation**: Diagnosed and ruled out a separate within-page footer shift; the committed fix focuses on the scan-driven jitter that users actually hit while navigating directories.

### 2. Whole-Subtree Scan + Per-Directory Cache Priming
*   **Rust `--tree` Mode**: Added a `--tree` mode to the `topo-core` engine that, in the same single walk it already performs to compute totals, aggregates size, file count, and immediate-children for EVERY directory level and emits them keyed by a path relative to the scan root (`"."` is the root). The default single-level output is byte-for-byte unchanged for existing callers (`_parallel_scan_sizes`, `get_size_fast`).
*   **Shared Walker**: Factored the directory walk into a single `walk_files()` helper reused by both the single-level and tree scans, so skip-list, symlink, hidden-file, and zero-byte rules stay identical across modes.
*   **Size-Threshold Pruning**: Tree mode only emits directories ≥ 1 MiB (the root is always emitted, and every node still lists all immediate children), bounding output and memory on very large home directories while keeping every meaningful directory instantly drillable.
*   **Cache Priming**: `get_rust_tree_data()` and `_prime_cache_from_tree()` populate `ScanCache` for every directory level from one engine pass, rejoining relative keys onto the original (possibly symlinked) root so they match how the UI builds child paths via `parent / name`.
*   **Scan-Free Drilling**: Entering a directory now tree-scans once and primes all levels, so subsequent drilling into any cached subdirectory is an instant cache hit with no rescan. Sub-1 MiB directories fall back to a quick scoped scan, and engines predating `--tree` fall back to the original single-level scan.

### 3. Grace-Period Scan Screen (No Flash on Fast Scans)
*   **Deferred Scan UI**: `_scan_with_spinner()` runs the scan in a background thread and only paints the screen-clearing header + spinner if the scan exceeds a short grace period (`SCAN_SPINNER_DELAY = 0.15s`). Fast scans (small dirs) finish within the window and hand off to the result list with an in-place redraw — identical to a cache hit, with no flash or jitter — while genuinely slow scans (first Home scan, large directories) still show the loading spinner.

### 4. Thorough Uninstallation & UI Polish (v0.7.0 Prep)
*   **APT Purge & Autoremove**: Upgraded Ubuntu/Debian uninstallation from `apt remove` to `apt purge -y --autoremove`. This ensures system-level configurations and orphaned dependencies are fully cleared, aligning with the "Remove apps completely" promise.
*   **Light-Theme Compatibility**: Redefined the `WHITE` color constant to a dark gray (`\033[38;5;244m`), ensuring that file sizes and metadata are clearly visible on both dark and pure-white terminal backgrounds.
*   **UI Layout & Color Sync**:
    *   **Analyze Disk**: Moved the folder/file name to the center and pushed the file size to the far right with a separator. Matched the file size color with the name color (dynamic highlighting).
    *   **Uninstall List**: Reordered the layout to `[Checkbox] [App Name] [Size] | [Time]`.
    *   **Progress Bar Logic**: Refined `draw_bar` so that 0% is strictly gray and any value > 0% (even 0.1%) forces at least one colored block (Green/Yellow/Red).
    *   **Cleaner TUI**: Removed `age_hint` (e.g., `>1m`) from the Analyze Disk view for a more minimalist look. Changed the uninstaller title to "Select Application to Remove" in purple with tighter spacing.
*   **Sudo Cancellation Experience**: Added a graceful "Uninstall cancelled by user" prompt with a standardized "Enter: Return / ESC: Exit" handler when a password prompt is interrupted with `Ctrl+C`.
*   **System Health Enhancements**: 
    *   Applied dynamic color coding (Green/Yellow/Red) to **Memory** and **Disk** percentage text to match their progress bars.
    *   Added intelligent color coding for **CPU Temperature**: Green (<60°C), Yellow (60-80°C), and Red (>80°C).
    *   Added color coding for **Battery**: Green (>50%), Yellow (20-50%), and Red (<20%). Health and cycle details remain in the default color for clear hierarchy.
*   **Bug Fix**: Fixed a hardcoded `CYAN` color in `src/core/analyze.py` that was preventing Analyze Disk progress bars from showing their correct status colors (Green/Yellow/Red).
*   **v0.7.0 Release Prep**: Updated `VERSION` to `0.7.0` and drafted comprehensive release notes in `docs/releases/v0.7.0.md` summarizing all changes since v0.6.0.

### 5. Regression Coverage
*   **Uninstall Tests**: Updated `tests/test_uninstall.py` to verify the new `apt purge --autoremove` command.
*   **Rust & UI Tests**: Added `cargo` tests covering tree-mode totals and hierarchical data, and verified the final TUI state manually across different terminal themes.
*   **Verification**: Confirmed the final state with `cargo test`, `ruff check src tests`, and the full pytest suite.

# Daily Modification Report - 2026-06-01

## Project: topo (Topo) - Unified Destructive Action UX

### 1. Password Prompt & Cancellation Consistency
*   **Custom Sudo Prompts**: Extended sudo authorization to accept custom multi-line prompts, replacing raw `[sudo] password` output with Topo-styled messages such as `System cleanup requires admin access`, `System optimization requires admin access`, and `App removal requires admin access`.
*   **Clean Flow**: Added a pre-clean sudo decision prompt with `Enter` to continue and `Space` to skip. Space now returns directly to the main UI without printing an extra skipped message or showing a return prompt.
*   **Optimize Flow**: Applied the same password interaction model to Optimize. Space skips the task silently and returns to the main UI; password cancellation stops the task instead of continuing into maintenance steps.
*   **ESC Cancellation**: Clean and Optimize sudo pre-confirmation prompts now treat `ESC` as cancel, returning to the main UI instead of falling through to the password prompt.
*   **Uninstall Preview Flow**: Simplified uninstall preview confirmation into a single line: `Remove N application(s), size  Enter confirm, Space cancel`. Enter proceeds to the custom password prompt; Space/ESC returns to the application list without uninstalling.
*   **Uninstall List Shortcut Alignment**: Simplified the uninstall list footer by removing Back, ESC, and generic Enter confirmation hints, expanding `Space: Select`, and spelling out sort shortcuts as `N: Name`, `S: Size`, and `T: Time`.
*   **Explicit Uninstall Action Hint**: Added `Enter:Uninstall` beside `Selected Apps to Remove`, and changed Enter so it only proceeds when applications are explicitly selected instead of confirming the hovered app by default.
*   **Password Cancellation Safety**: Ctrl+C during password input now cancels Clean, Optimize, Uninstall, or privileged Analyze deletion without continuing into cleanup/removal logic.

### 2. Analyze Delete Flow
*   **Unified Selection Model**: Standardized Analyze deletion around `Space` to select and `Enter` to delete selected items. Removed the `Del` deletion shortcut and its hint so users do not need to choose between different destructive-action keys.
*   **Contextual Delete Hint**: Added an inline hint beside `Selected Items to Remove` so selected files clearly show `Enter:Delete` at the moment the action becomes available.
*   **Sudo Only When Needed**: Analyze now deletes user-owned writable home paths without asking for sudo, while system paths, non-home paths, or unwritable paths still require admin authorization.
*   **Privileged Delete Safety**: Admin Analyze deletes go through `validate_path_for_deletion()` before running `sudo rm -rf -- path`, and every result is recorded in the deletion audit log.
*   **Empty Directory Fix**: Fixed a busy-loop bug after deleting the last item in an Analyze directory. Empty results now render `No items found` and wait for user input instead of repeatedly refreshing and consuming CPU.
*   **Refresh Messaging**: Deletion-triggered rescans now display `Refreshing analysis...` instead of the initial Rust scan message, making post-delete state changes clearer.

### 3. TUI Page & Hint Cleanup
*   **Clean Page Isolation**: Selecting Clean from the main menu now clears the screen before running, so cleanup output appears on its own page instead of under the main menu.
*   **Status Page Isolation**: Selecting Status also clears the screen before rendering system health output.
*   **Readable CPU Load**: Replaced raw Linux load-average output in System Health Status with a user-readable load label and core-relative percentage while preserving 1m/5m/15m details.
*   **Analyze Banner Removal**: Removed the main-menu banner from the Analyze Disk root view so the analyzer starts as a focused tool page.
*   **Analyze Hint Simplification**: Reduced Analyze footer hints to the actions that need discovery, now showing a single concise line such as `A:All | F:Open Folder | R:Reload | S:Sort ↓ | Space:Select`.

### 4. Regression Coverage
*   **Navigator Tests**: Added coverage for Enter-based Analyze deletion, disabled Del deletion, and empty Analyze result handling.
*   **Analyze Permission Tests**: Added coverage proving user-writable paths avoid sudo while system paths use the privileged delete branch.
*   **Verification**: Confirmed the final state with Ruff and the full pytest suite (`132 passed`).

### 5. Protection Policy Hardening
*   **Expanded Sensitive Data Protection**: Added default protection for messaging apps, shell profiles, developer credentials, CLI tools, editor state, sync clients, and additional Flatpak app data.
*   **System Temp/Cache Carve-Out**: Allowed deletion validation for contents under `/var/tmp` and `/var/cache` while continuing to protect those root directories and unrelated `/var` system paths.
*   **Legacy Whitelist Migration**: Ignored old auto-seeded system entries such as `/`, `/usr`, and `/var` when reading `whitelist.json`, keeping hardcoded protections authoritative while preserving user-added paths.
*   **Topo Self-Protection**: Protected Topo's own configuration directory from accidental removal through the shared `is_protected()` policy.
*   **Redundant Delete Guardrail**: Restored a minimal prefix-based critical-path fallback inside `validate_path_for_deletion()` so system children remain blocked even if higher-level protection logic changes.
*   **Verification**: Added protection and carve-out regression tests and confirmed the suite with Ruff plus full pytest (`140 passed`).

### 6. Uninstall Thorough Cleanup Guardrails
*   **Graceful Process Termination**: Changed uninstall process cleanup from immediate SIGKILL to a staged SIGTERM, short wait, and SIGKILL fallback sequence.
*   **Uninstall Bypass Mode**: Added a `bypass_whitelist` path-removal mode so explicit app uninstall can remove protected app-owned residue such as browser or messaging app profiles.
*   **Hard Protection Boundary**: Split protection into hard rules and normal app-data rules. Uninstall bypass can skip ordinary app-data protection, but still refuses system paths, user whitelist entries, Topo configuration, and credential directories such as SSH/GPG/AWS/Kube/Docker/GitHub CLI.
*   **Clear Removal Context Naming**: Replaced the uninstall cleanup call site with `allow_app_data_removal=True`, making the intent explicit while keeping the older `bypass_whitelist` keyword compatible for internal callers.
*   **Hard Protection Reasons**: Added specific hard-protection reasons such as `critical system path`, `credential or identity data`, `Topo configuration`, and `user whitelist` so future UI/history output can explain skipped residue paths clearly.
*   **Regression Coverage**: Added tests proving uninstall bypass removes ordinary app data while preserving hard-protected credentials, user whitelist paths, and Topo config.
*   **Verification**: Confirmed the final state with Ruff and full pytest (`145 passed`).

### 7. Linux-Native Optimize Hardening
*   **Safer Browser Database Optimization**: Added Mole-inspired SQLite safeguards for browser database vacuuming: skip running browsers, reject WAL/SHM sidecar files, verify SQLite headers, cap database size at 100 MiB, run `PRAGMA integrity_check`, and enforce a VACUUM timeout guard.
*   **Conditional Memory Relief**: Changed PageCache release from unconditional sudo work to a memory-pressure check based on `/proc/meminfo`. Optimize now skips cache dropping when available memory is already healthy.
*   **Desktop/MIME Cache Refresh**: Added optional Linux desktop database and MIME database refresh tasks using `update-desktop-database` and `update-mime-database` when available.
*   **User Systemd Reload**: After removing broken user systemd service units, Topo now runs `systemctl --user daemon-reload` when available so user service state matches the filesystem.
*   **Dry-Run Parity**: Expanded optimize dry-run previews for fstrim, font cache, DNS cache, PageCache, thumbnail cache, desktop database, and MIME database tasks.
*   **Regression Coverage**: Added optimize tests for browser-running database skips, low-memory-pressure PageCache skips, systemd daemon reload, and desktop/MIME dry-run previews.
*   **Verification**: Confirmed the final state with Ruff and full pytest (`149 passed`).

### 8. Analyze Scan Feedback
*   **Animated Scan Indicator**: Replaced the static rocket icon in Analyze's Rust engine scan message with a spinner animation while scans are running.
*   **Refresh Feedback Alignment**: Reused the same animated status renderer for post-delete refresh scans so scan and refresh states feel consistent.
*   **Standalone Analyze Page**: Cleared the main menu before launching Analyze from the TUI, so the Rust engine scan animation starts on its own focused page like Clean and Status.
*   **Scan Header Placement**: Added an Analyze scan header so `Rust Engine: Intelligence Scan on Home` appears directly under `Analyze Disk` during the loading state.
*   **Scan Copy Simplification**: Updated the Analyze scan message to `Rust Engine: Analyzing disk usage, please wait . . .` for clearer loading feedback.
*   **Regression Coverage**: Added coverage for the scan status message to ensure the spinner frame is shown and the old rocket icon does not return.
*   **Verification**: Confirmed the final state with Ruff and full pytest (`151 passed`).

# Daily Modification Report - 2026-05-31

## Project: topo (Topo) - Deletion Audit Trail

### 1. Recoverable Deletion Observability
*   **Deletion Audit Log**: Added a best-effort deletion audit trail at `~/.local/state/topo/deletions.log`, with `TOPO_DELETE_LOG` override support for tests and custom deployments.
*   **Unified Event Recording**: `safe_remove()` now records destructive operation outcomes including missing paths, whitelist/critical-path rejections, dry-run previews, successful Trash moves, Trash failures, permanent deletions, and deletion failures.
*   **Trash Fallback Visibility**: When Trash tools fail and Topo falls back to permanent deletion, the audit log records both the `trash-failed` event and the final permanent deletion result.
*   **Dry-Run Coverage**: Routed age-based cleanup, stale temp cleanup, generic app cache previews, and Cargo cache previews through the audit layer so preview runs leave an inspectable trail without deleting files.
*   **Regression Tests**: Added coverage for permanent deletion audit rows, dry-run audit rows, XDG state path resolution, and Trash-failure fallback logging.

### 2. Dangerous Path Fuzz Corpus
*   **Linux Dangerous Path Corpus**: Added `tests/fuzz_corpus/dangerous_paths.txt` with Linux-specific deletion hazards including `/`, `/bin`, `/boot`, `/dev`, `/proc`, `/sys`, `/run`, `/home`, `/var/lib`, `/etc/passwd`, `/usr/bin/bash`, and traversal variants such as `/tmp/../etc`.
*   **Central Deletion Validation**: Introduced `validate_path_for_deletion()` to reject empty, relative, traversal, control-character, whitelisted, and critical Linux system paths before size checks or deletion attempts.
*   **Fuzz Regression Tests**: Added pytest coverage proving every corpus entry is rejected, generated control-character paths are blocked, and normal user-owned absolute paths remain allowed.
*   **Symlink Target Protection**: Tightened the validation tests around symlink targets so links pointing into critical system paths are rejected while broken user-owned symlinks remain removable as links.
*   **Single Deletion Gate**: Removed duplicated whitelist/critical-path checks from `safe_remove()` so analyze deletion, uninstall residue cleanup, and cache cleanup all rely on the same validation policy.

### 3. Cleanup History Summary
*   **History Command**: Added `topo history --limit N` to summarize recent deletion audit sessions from `deletions.log`.
*   **Session Boundaries**: `run_clean()` now records `started` and `ended` session markers after authorization succeeds, allowing history output to group cleanup operations by run.
*   **Summary Metrics**: History rendering reports removed, trashed, skipped, failed, and reclaimed size totals, with a short tail of recent paths per session.
*   **Legacy Log Support**: Existing deletion log rows without session markers are grouped into a `legacy` history block instead of being ignored.
*   **History Tests**: Added parser and renderer coverage for session logs, legacy ungrouped logs, failed rows, skipped rows, and size aggregation.
*   **Uninstall History Fix**: `execute_uninstall()` now records `uninstall <app>` session markers and package removal events, so app removals with no residue paths still appear in `topo history`.

### 4. Linux App Data Protection Model
*   **Sensitive Data Rules**: Added Linux-specific protected data paths for SSH/GPG credentials, keyrings, password managers, browser profiles, input methods, wallets, database clients, and IDE/editor configuration.
*   **Flatpak App Protection**: Added protected `~/.var/app/<app-id>` entries for sensitive Flatpak apps such as Firefox, Chromium/Chrome/Brave/Edge, Bitwarden, KeePassXC, Thunderbird, and pgAdmin.
*   **Unified Enforcement**: Routed the protection through `is_protected()`, so `safe_remove()`, analyze deletion, uninstall residue cleanup, and cache cleanup all share the same sensitive-data guard.
*   **Protection Tests**: Added regression coverage proving sensitive profile/config paths are blocked while ordinary app cache/config paths remain removable.

### 5. Linux-Native Migration Guardrails
*   **User Systemd Cleanup**: Added a Linux-native optimizer task that removes broken `~/.config/systemd/user/*.service` units when their `ExecStart` target no longer exists, with dry-run support.
*   **Conservative Scope**: Limited service cleanup to user-owned systemd units and avoided system-level `/etc/systemd` or `/usr/lib/systemd` paths.
*   **macOS-Only Regression Guard**: Added a source-level portability test that rejects direct use of macOS-only cleanup primitives such as `/Library`, LaunchAgent/LaunchDaemon, `osascript`, Spotlight, Homebrew Cask, Xcode, iOS backup, and DerivedData logic in `src/`.
*   **Linux Counterpart Coverage**: Kept cleanup aligned with Linux primitives already used by Topo: XDG directories, Flatpak, Snap, APT/DNF/Pacman, Docker/Podman, journalctl, and gio/trash-cli.

### 6. Official Uninstaller Rules
*   **Official-Only Residue Policy**: Added uninstall rules that keep residue cleanup disabled for high-risk software classes such as VPN/security tools, input methods, password managers, SSH/GPG-related tools, and similar sensitive apps.
*   **System Component Filtering**: Filtered driver, kernel, desktop-environment, audio/network, and display-stack packages out of the uninstall app list so system components are not presented as normal removable apps.
*   **Snap Uninstall Support**: Added Snap app discovery and routed Snap removals through `snap remove` instead of any direct file deletion.
*   **Uninstall Regression Tests**: Added coverage for official-only residue skipping, protected system package filtering, Snap scanning, and Snap removal command routing.

### 7. CACHEDIR.TAG Analysis
*   **Cache Directory Detection**: Added Linux `CACHEDIR.TAG` recognition using the standard `Signature: 8a477f597d28d172789f06886806bc55` marker.
*   **Analyze Cleanable Metadata**: Disk analysis entries now mark valid cache-tagged directories as `is_cleanable` with `cleanable_reason="CACHEDIR.TAG"` and a cache-cleaning icon.
*   **Regression Tests**: Added tests for valid tags, invalid/missing tags, and analysis-entry cleanable metadata.
*   **Shared Clean Integration**: Moved CACHEDIR.TAG recognition into `core.file_ops` and reused it from `clean_generic_xdg_caches()` so valid tagged cache directories under `~/.cache` can be cleaned directly while dry-run keeps them intact.

### 8. Help & Documentation
*   **History Help Example**: Expanded `topo --help` examples to include `topo history --limit 5`.
*   **README History Usage**: Added `topo history` commands and a cleanup history output example to the README.

### 9. Duplicate Code Reduction
*   **Critical Path Single Source**: Centralized critical Linux path constants in `whitelist.py` and reused them from `file_ops.validate_path_for_deletion()`, eliminating duplicated delete-protection lists.
*   **Run Path Protection Alignment**: Added `/run` to the default critical path set so whitelist checks and deletion validation share the same system path policy.
*   **Uninstall App Record Helper**: Added a shared `_app_record()` helper for DNF, Flatpak, and Snap scan results, reducing repeated dictionary construction in the uninstall scanner.
*   **Regression Coverage**: Added coverage proving `/run/systemd` is protected through the shared whitelist policy.

### 10. GNOME Uninstall List Filtering
*   **GNOME Core Component Hiding**: Extended uninstall scan filtering so GNOME desktop infrastructure such as GDM, GNOME Control Center, Settings Daemon, Software, Terminal, Nautilus, GVFS, dconf, and XDG Desktop Portal components are not presented as normal removable apps.
*   **Conservative App Retention**: Kept ordinary user-facing GNOME apps visible, such as `gnome-calculator`, instead of filtering every package that starts with `gnome-`.
*   **Regression Coverage**: Added uninstall scan tests proving GNOME system components are hidden while user GNOME apps remain selectable.
*   **System Utility Refinement**: Hid additional GNOME integration utilities from uninstall results, including Browser Connector, Color Manager, Disk Utility, Initial Setup, Logs, Online Accounts, and System Monitor, while keeping user apps such as Calendar, Characters, Clocks, Connections, Contacts, Font Viewer, and Maps visible.
*   **Input Method Protection**: Hid IBus language engine packages such as `ibus-libpinyin`, `ibus-hangul`, `ibus-chewing`, and `ibus-anthy` from the uninstall list because they are input-method framework components rather than standalone applications.
*   **LibreOffice Package Filtering**: Hid internal LibreOffice packages such as `libreoffice-core` and `libreoffice-xsltfilter`, while keeping user-facing modules such as Writer, Calc, and Impress visible in the uninstall list.
*   **APT/Pacman Mock Verification**: Added Ubuntu/Debian APT and Arch/Manjaro Pacman uninstall scan paths with mocked regression tests, including package-manager-specific removal commands so non-Fedora systems no longer fall through to DNF.
*   **Linux Kernel Package Filtering**: Hid Debian-family kernel packages such as `linux-image-*` and `linux-headers-*` from uninstall results as system components.
*   **Cross-Distro Desktop Ownership Detection**: Extended uninstall pre-scan beyond RPM by using `dpkg-query -S` for APT systems and `pacman -Qo` for Pacman systems to identify packages that own `.desktop` launchers.

### 11. Installer Link Reliability
*   **Root-Friendly Command Link**: Updated `topo link` so root installs create the launcher in `/usr/local/bin`, while regular users continue to use `~/.local/bin`.
*   **Install-Time Link Verification**: `install.sh` now distinguishes fresh installs from updates, runs a visible link setup on fresh installs, and warns with a direct launcher path if `topo` is still not available in `PATH`.
*   **Non-Git Install Recovery**: Existing `~/.topo` directories that are not git checkouts are now replaced with a clean clone instead of failing during `git fetch`.
*   **Remove Alignment**: `topo remove` now recognizes both `~/.local/bin/topo` and `/usr/local/bin/topo` links when they point to the active Topo installation.
*   **Regression Tests**: Added install-link tests for override directories, root target selection, and launcher symlink creation.
*   **Packaging Dependency Check**: Added an installer prerequisite check for Python's `packaging` module with Debian/Ubuntu, Fedora/RHEL, and Arch/Manjaro install commands.

### 12. Release-Based Update Channel
*   **GitHub Release Version Source**: Changed `topo update` to read the latest GitHub Release `tag_name` instead of the development branch `VERSION` file.
*   **Tag-Based Install Path**: Updated `install.sh` to accept `--version/--ref`, allowing updates to clone/reset the exact release tag and download assets from that release instead of `main`.
*   **Stable Update Semantics**: Version comparisons now normalize leading `v` prefixes, reject invalid release tags, avoid downgrades, and only update when the latest release tag is semantically newer.
*   **Update Regression Tests**: Updated tests to mock GitHub Releases API responses and verify that the generated installer command targets the release tag.

### 13. Release Asset Automation
*   **Multi-Arch Core Build Workflow**: Updated the GitHub Actions release workflow to build `topo-core` for `x86_64-unknown-linux-gnu` and `aarch64-unknown-linux-gnu` on tag pushes.
*   **Automatic Release Assets**: The workflow uploads `topo-core-x86_64` and `topo-core-aarch64` as Actions artifacts and attaches them to the tag's GitHub Release automatically.
*   **Checksum Artifacts**: Added SHA-256 checksum files for both native engine assets.
*   **Rust Source Tracking**: Adjusted `.gitignore` so `topo-core` source and Cargo metadata can be committed while `topo-core/target/` remains ignored.
*   **Version Prep**: Prepared the local version file for the upcoming `0.6.0` tag.
*   **Pre-Release Detection**: Release tags containing `-rc.`, `-beta.`, or `-alpha.` are marked as GitHub pre-releases automatically.

### 14. v0.6.0 Release Notes
*   **Release Draft**: Added `docs/releases/v0.6.0.md` as the first public release note draft, using `assets/circle.png` as the title image.
*   **Launch Summary**: Summarized the safety guardrails, deletion history, Linux-native uninstall filtering, CACHEDIR.TAG support, release update channel, and multi-arch engine assets from the recent development reports.
*   **User Guidance**: Documented install commands, verified environments, supported distro families, architecture targets, caution notes, and the next release priorities.
*   **Bilingual Format**: Reworked the release note to lead with English release copy and include a focused Chinese summary for key changes.
*   **Workflow Body Path**: Updated the release workflow so future tag releases automatically read `docs/releases/<tag>.md` as the GitHub Release body before attaching binary assets.
*   **Stable Default Install**: Changed `install.sh` so the default README install command resolves and installs the latest GitHub Release, while `--ref main` remains available for development installs.
*   **Quiet Tag Checkout**: Adjusted release-tag installs to fetch and checkout annotated tags explicitly, avoiding Git's shallow-clone detached-HEAD warning during normal installs.
*   **Lean Install Footprint**: Removed runtime-unnecessary `.github/`, `assets/`, and `docs/` directories from installed copies so `~/.topo` only keeps files needed to run Topo.
*   **Release Resolver Fallback**: Made latest-release detection prefer GitHub's `/releases/latest` redirect and fall back to the API with an explicit User-Agent, reducing install failures caused by API/network quirks.

# Daily Modification Report - 2026-05-30

## Project: topo (Topo) - High-Performance Input & Flicker-Free UI

Today's session focused on reaching the pinnacle of TUI performance, achieving a flicker-free rendering experience, and hardening the input system against hardware interference.

### 1. Advanced UI Rendering (Zero-Flicker)
*   **Double-Buffering Implementation**: Rewrote the rendering engine to use a full-frame memory buffer. Screens are now built in memory and written to the terminal in a single atomic `sys.stdout.write` operation, eliminating the "blanking" effect of full-screen clears.
*   **Atomic Overwriting**: Replaced `os.system("clear")` with a "Home-and-Overwrite" strategy (`\033[H`). This ensures that only changing pixels are updated, making rapid transitions and long-press scrolling perfectly smooth.
*   **Pedantic Line Clearing**: Integrated the `\033[K` (Clear Line) command into every row of the buffer. This guarantees that remnants and "ghost" characters from previous larger menus are immediately and completely wiped, ensuring a crisp visual state.
*   **Layout Standardization**: Unified the placement of help prompts across all views. Interaction hints are now consistently positioned immediately below the dashed separator line, providing a predictable and stable UI layout.

### 2. Input System Hardening
*   **Raw FD Capture**: Refactored `Navigator.get_key` to use raw file descriptors (`os.read(fd, 1)`) and high-frequency polling (20-30ms). This bypasses high-level Python buffers, ensuring that multi-byte escape sequences (arrow keys, mouse events) are captured as single, atomic units.
*   **Persistent Terminal Modes**: Implemented a `raw_mode` context manager that maintains a non-echoing terminal state throughout interactive loops. This eliminates visual artifacts like `^[[A` appearing during rapid scrolling.
*   **Immune Mouse Filtering**: Engineered bit-precise parsing for X11 and SGR mouse protocols. Topo now perfectly identifies and swallows mouse wheel events, preventing them from being misinterpreted as hotkeys (like 'A' for select all) during fast scrolling.
*   **Strict Hotkey Validation**: Added a `len(key) == 1` enforcement for all single-letter hotkeys. This protects the application logic from fragmented or malformed escape sequences.

### 3. Navigation & Uninstaller Refinements
*   **Two-Column Selection Display**: Optimized the `Selected Apps to Remove` summary to use a space-efficient 2-column layout.
*   **Full Selection Visibility**: Removed truncation logic ("and xx more") to ensure the user can review every single selected application before confirming uninstallation.
*   **Stable Confirmation Loop**: Wrapped the uninstaller preview in a dedicated internal loop. This prevents accidental returns to the selection list caused by unrecognized inputs like mouse scrolls or side-arrow presses.
*   **Secure Authorization**: Integrated a mandatory `[sudo]` password prompt before uninstallation, featuring a clear `Ctrl+C` cancellation path and accurate user feedback ("Authorization failed" vs "Cancelled by user").
*   **Intelligent Back-Navigation**: Refined the `LEFT` arrow key behavior to trigger a "Back" action only when on the first page of a list, preventing confusing wrap-around behavior.

### 4. Disk Analyzer (Analyze Disk) Polish
*   **Pixel-Perfect Alignment**: Tightened the layout between filenames and sizes by reducing padding and truncating long names to 30 characters, creating a more compact and readable view. 
*   **Dynamic Separator Line**: Refactored the UI separator line to automatically match the exact length of the help prompt text below it, ensuring clean visual symmetry.
*   **Vertical Alignment Fix**: Fixed a visual shift issue where navigating into folders with more than 9 items caused the percentage and size columns to misalign. Checkbox indices are now strictly formatted to a fixed width.
*   **Accurate Percentage Calculation**: Resolved a critical bug where returning to the Root (/) view from a subdirectory caused disk percentages to exceed 100%. The `total_scan_size` is now correctly resynchronized with system disk usage upon returning.
*   **Intuitive Navigation**: Restored the `Enter` key functionality to safely drill down into subdirectories or open files directly, matching user expectations.
*   **Strict Deletion Safety**: Enforced a stricter policy for the `Del` key. It now strictly requires explicit item selection (via Space or numbers) before triggering the batch deletion workflow, preventing accidental deletion of merely hovered items.

### 5. Architecture & Quality
*   **100% Test Pass Rate**: Aligned the 57-unit test suite with the new modular `system` calls and persistent TUI modes. The project maintains rock-solid reliability in CI environments.
*   **Ruff Elite Standard**: Maintained a zero-error state under strict Ruff linting, ensuring all new high-performance code adheres to modern Python 3.10+ standards.

### 6. Refactoring & Consistency Cleanup
*   **Selector Deduplication**: Further consolidated `navigator.py` by expanding the shared `_PagedSelector` and `_selector_session` patterns. Paginated selectors now reuse common cursor movement, page flipping, page selection, and raw terminal session handling instead of duplicating loop scaffolding.
*   **Uninstaller Navigation Reuse**: Refactored `UninstallSelector` to inherit the shared paginated behavior and reuse `Navigator.read_number()` for multi-digit input, reducing repeated pagination and numeric selection logic.
*   **Dead Code Removal**: Removed the unused `src/ui/menu.py` legacy `interactive_select` implementation, which had been fully replaced by the newer selector system.
*   **Configuration Path Centralization**: Added `src/core/paths.py` as the single source for `get_config_dir()`, eliminating duplicate definitions in `config.py` and `whitelist.py`.
*   **Default Purge Path Single Source**: Reused `DEFAULT_PURGE_SEARCH_PATHS` from `constants.py` inside `DEFAULT_CONFIG`, preventing drift between duplicated purge path defaults.
*   **Size Parsing Consolidation**: Introduced a shared `parse_size_to_bytes()` helper in `file_ops.py` and routed the uninstaller size parser through it while keeping the existing compatibility method.
*   **Binary Unit Alignment**: Updated `bytes_to_human()` to use 1024-based binary units (`KiB`, `MiB`, `GiB`, `TiB`) so display units match the codebase's threshold semantics.
*   **Unused Constant Cleanup**: Removed unused `PURGE_CONFIG_FILE` and `MIN_AGE_DAYS` constants while preserving actively used UI constants such as `EARTH`.
*   **Verification**: Confirmed the cleanup with `ruff check src tests` and the full pytest suite (`70 passed`).

### 7. Safety & Privacy Hardening
*   **Safer Uninstall Residue Matching**: Hardened `find_residue_paths()` against accidental deletion by treating high-risk short or generic tokens such as `code`, `go`, and `id` as unsafe residue match keys. Added regression coverage to ensure Flatpak/RPM IDs like `org.example.go` do not match unrelated directories such as `~/.cache/go`.
*   **Residue Matching Regression Tests**: Added tests that preserve legitimate app-specific matches such as `telegram-desktop` and `vendor-myapp-state` while blocking generic short-tail tokens.
*   **Temporary Directory Safety Coverage**: Locked down the `/tmp` and `/var/tmp` cleanup policy with tests proving that only stale, user-owned entries are removed, while fresh files, hidden entries, and `systemd` private temp directories are skipped.
*   **Exception Scope Reduction**: Narrowed broad exception handling in the uninstall and user-temp cleanup paths to expected operational failures (`OSError`, `subprocess.SubprocessError`, `ValueError`) instead of swallowing all program errors.
*   **Status Privacy Default**: Added `status_public_ip: False` to configuration and changed `topo status` so it no longer contacts `ip-api.com` by default. Public IP lookup now requires explicit opt-in.
*   **Public IP Test Coverage**: Added tests proving `get_ip_info()` does not call `urllib.request.urlopen` unless public IP lookup is enabled, preserving fast and private default status checks.
*   **Verification**: Confirmed the safety and privacy changes with targeted Ruff checks and the full pytest suite (`75 passed`).

### 8. Deletion Safety & Command Reliability
*   **Symlink-Safe Removal**: Fixed `safe_remove()` so symlink inputs delete only the symlink itself while still applying protection checks to the resolved target path. Added regression coverage proving symlink targets remain intact.
*   **Unified Dangerous Delete Path**: Routed Cargo registry cleanup through `safe_remove()` instead of direct `shutil.rmtree(..., ignore_errors=True)`, keeping deletion safeguards centralized.
*   **Conservative Cache Aging**: Adjusted generic XDG cache cleanup so obvious cache/log/temp directories still require at least 3 days of inactivity, avoiding same-session application cache deletion.
*   **Command Success Accounting**: Updated Docker, Podman, and Multipass cleanup routines to report success and increment cleaned-item counters only when the underlying command exits successfully.
*   **Config Default Isolation**: Changed `load_config()` to return deep copies of defaults, preventing callers from mutating shared default lists such as `purge_search_paths`.
*   **Regression Tests**: Added targeted tests for symlink deletion behavior, independent config defaults, XDG cache age thresholds, and developer-tool cleanup success accounting.
*   **Verification**: Confirmed the changes with `ruff check src tests` and the full pytest suite (`77 passed`).

### 9. Update Version Semantics
*   **Semantic Version Comparison**: Replaced raw string equality checks in `topo update` with `packaging.version.Version`, so `1.10.0` is correctly treated as newer than `1.9.0`.
*   **Downgrade Protection**: Prevented update execution when the remote version is older than the local installation, reporting that the local copy is already newer instead of treating any mismatch as an upgrade.
*   **Invalid Remote Guard**: Added validation for malformed remote version strings such as `latest`; invalid values now abort safely instead of triggering the installer.
*   **Update Regression Tests**: Added focused tests for newer, equal, older, and invalid remote versions, including assertions that the install script is not run for downgrade or invalid-version cases.
*   **Verification**: Confirmed the update hardening with `ruff check src tests` and the full pytest suite (`81 passed`).

### 10. Unified Command Execution Layer
*   **CommandResult Contract**: Reworked `core.system.run_command()` to always return a structured `CommandResult` with `ok`, `returncode`, `stdout`, `stderr`, `error`, and `timed_out` fields instead of mixing raw `CompletedProcess` objects and `None`.
*   **Timeout Support**: Added a default command timeout plus per-call overrides for short probes such as `pgrep`, `xdg-open`, `docker info`, `nvidia-smi`, and `ps`, preventing command hangs from blocking cleanup or status flows indefinitely.
*   **Centralized Subprocess Usage**: Migrated cleanup, status, analyzer, trash, Docker/Podman, package-manager, and uninstaller command calls onto `run_command()` so success/failure semantics are consistent across modules.
*   **Accurate Success Reporting**: Tightened Snap, package-manager, journal, Flatpak-unused, fstrim, font-cache, DNS, memory, Docker, Podman, and Multipass tasks so they only report success or increment counters when `CommandResult.ok` is true.
*   **Command Layer Tests**: Added direct tests for successful, failed, and timed-out command results, and updated existing tests to assert the unified command layer instead of raw `subprocess.run()` details.
*   **Verification**: Confirmed the command-layer refactor with `ruff check src tests` and the full pytest suite (`84 passed`).

### 11. Exception, Deletion & Config Hardening
*   **Config Schema Normalization**: Added `normalize_config()` to validate user config types and fall back to safe defaults when values like `purge_search_paths`, `use_trash`, `min_age_days`, or `status_public_ip` have invalid shapes.
*   **Config Regression Tests**: Added tests proving invalid config values are rejected and valid values are preserved, preventing malformed JSON from producing surprising runtime behavior.
*   **Expanded Removal Safety Tests**: Strengthened `safe_remove()` coverage for broken symlinks, parent-whitelist protection, and permission errors, expanding the deletion-layer test matrix beyond normal files and directory symlinks.
*   **Narrower Exception Handling**: Replaced broad `except Exception` blocks in `apps.py`, `analyze.py`, `status.py`, `config.py`, and `file_ops.py` with expected exception classes such as `OSError`, `JSONDecodeError`, `ValueError`, `IndexError`, `UnicodeDecodeError`, and `URLError`.
*   **Analyzer Parse Safety**: Made Rust scan JSON parsing fail closed on malformed output without hiding unrelated programming errors.
*   **Verification**: Confirmed the hardening pass with `ruff check src tests` and the full pytest suite (`89 passed`).

---

# Daily Modification Report - 2026-05-29

## Project: topo (Topo) - Professional Polishing & Enterprise Quality

Today's session achieved a major milestone in Topo's development, reaching a production-ready state with elite-level code quality and refined user experience.

### 1. Analyze Disk & File Safety
*   **Safe File Handling**: Implemented a security layer for the Disk Analyzer. Topo now detects executable files and archives (zip, tar, etc.) and prevents direct execution or extraction. Instead, it safely opens the parent directory in the system file manager.
*   **Root View Optimization**: Removed the redundant "Largest Files" (L) shortcut (now handled by the standardized 'S' sort) and hidden the non-navigable "Root (/)" entry to declutter the interface.
*   **Bug Resolution**: Fixed a critical `KeyError: 'size'` in the Top Files view by aligning with the Rust engine's `size_bytes` schema. Resolved variable scope shadowing and undefined name bugs in the analysis logic.

### 2. Uninstaller & UX Refinement
*   **Smart App Filtering**: Re-engineered the application scanner to cross-reference RPM packages with `.desktop` files. This filtered out over 2,000 system libraries, reducing the uninstaller list from 140+ pages to a focused set of user-facing applications.
*   **Automated Registry Health**: Upgraded the Proactive Detection engine to automatically prune "dead" entries from `detected_apps.json` when both the binary and data paths are confirmed missing.
*   **Interaction Standardization**: Standardized all exit prompts to "Press Enter to return, ESC to exit..." and implemented a unified, non-blocking key capture model via `Navigator.wait_for_return()`.

### 3. Architecture & Code Quality
*   **Rust Core Engine Refactor**: Completely rewrote the core scanning engine for massive performance and stability gains:
    *   **Memory Efficiency**: Implemented a **Min-Heap (BinaryHeap)** algorithm to track the top 100 largest files, reducing memory complexity from O(N) to O(1).
    *   **Accuracy Fixes**: Resolved a logical bug where files in the root directory were incorrectly categorized as subdirectories.
    *   **Robust Path Parsing**: Adopted a component-based path processing strategy for 100% reliable subdirectory size attribution.
    *   **API Standardization**: Updated the codebase to be fully compatible with standard `jwalk` 0.8+ interfaces.
*   **Zero Ruff Errors**: Achieved a 100% clean state project-wide using the Ruff linting engine. Refactored over 100 code sections to adhere to strict Python 3.10+ standards, including the elimination of all bare `except` blocks.
*   **Advanced Test Coverage**: Successfully pushed test coverage to **96% for core file operations** and **70% for business logic**. The project now boasts a robust suite of 57 unit tests with a 100% pass rate.
*   **Redundancy Elimination**: Purged duplicate cleanup logic in `user.py` that was already managed by the more advanced `APP_DEFS` engine.

### 4. Distribution & Git Hygiene
*   **Install Script Polish**: Refined the `install.sh` sequence to show the ASCII banner and version number as a final success screen. Improved post-install guidance for new users.
*   **Repository Cleanup**: Refined `.gitignore` to ensure `pyproject.toml` is tracked while excluding transient artifacts like `.coverage`. Successfully removed accidentally tracked binary files from the remote history.
*   **Asset Management**: Migrated all branding images to a dedicated `assets/` directory for better repository organization.

---

# Daily Modification Report - 2026-05-28

## Project: topo (Topo) - Hardware Insights & Navigation Polish

Today's session focused on expanding Topo's diagnostic capabilities and achieving a professional, silent exit experience for high-efficiency users.

### 1. Hardware & Network Diagnostics
*   **Real-time Fan Monitoring**: Implemented `get_fan_speed()` to probe `/sys/class/hwmon`. Topo now displays active fan RPMs in the Status dashboard with "Intelligent Silence" (hiding the line on fanless systems).
*   **Network IP Insights**: Integrated public and local IP detection. The dashboard now displays the user's geographic location via a 2-letter country code (e.g., `[CN]`) alongside their public IP address, using a lightweight API with aggressive timeouts for responsiveness.
*   **Architecture Parity Verification**: Successfully conducted ARM64 cross-architecture testing using Podman and QEMU, confirming that all TUI and installation logic is 100% compatible with aarch64 environments.

### 2. Interaction & TUI Refinement
*   **Uninstaller Intelligence**: Fixed the issue where application installation time was shown as "Unknown".
    *   **RPM/DNF Support**: Now retrieves exact installation timestamps using the `%{INSTALLTIME}` query format.
    *   **Flatpak Support**: Estimates installation time by analyzing the modification time of application data directories.
*   **Smart Sorting**: Reinforced the default sorting logic in the Uninstaller to ensure applications are always ranked by disk usage (largest to smallest) upon opening.
*   **Time-Ago Precision**: Improved the human-readable time format in lists to include "hours", "months", and "years" for better historical context.
*   **Unified Return/Exit Prompts**: Standardized all post-task prompts to "**Press Enter to return, ESC to exit...**" using a new `Navigator.wait_for_return()` helper. This ensures consistent, non-blocking single-key interaction across the entire application (Clean, Uninstall, Purge, Status).
*   **Terminal History Preservation**: Implemented the **Alternate Screen Buffer** (`\033[?1049h`) for all interactive modes. Topo now runs in a temporary terminal layer, ensuring that your previous shell history and output are perfectly restored upon exit.
*   **Professional Silent Exit**: Removed all conversational "Goodbye!" messages. Topo now exits cleanly and silently to the shell prompt.
*   **Intelligent Header Silence**: Implemented stdout redirection in the Clean runner. Category headers are now only printed if their sub-tasks actually reclaim space.

### 3. Intelligent Cleanup Engine (Architecture 2.0)
*   **Proactive App Detection**: Implemented a self-learning engine that automatically identifies newly installed software and registers their cache/config paths for high-precision cleaning.
*   **Registry Self-Maintenance**: Introduced `detected_apps.json` with automatic "health checks" that prune entries for uninstalled apps once their remnants are cleared.
*   **AI Developer Lifecycle**: Optimized `Hugging Face` and `Ollama` cleanup with age-aware logic (keeping "hot" models from the last 14 days). Added smart purging for `PyTorch`, `Triton`, and `CUDA` kernel caches.
*   **Cross-Distro Enhancements**:
    *   **Ubuntu**: Added specialized cleanup for `Snap` revisions and `Multipass` instances.
    *   **Fedora/Generic**: Implemented full `Podman` system pruning and transfer cache removal.
    *   **AppImage Support**: Developed a "Desktop Link Trace" method to identify and purge remnants of deleted AppImage files.
*   **WeChat Ecosystem Support**: Added comprehensive multi-path, multi-process protection and cleanup for various Linux WeChat versions (Flatpak, Wine, UOS).
*   **Self-Preservation Logic**: Implemented path-based protection to prevent Topo from recursively deleting its own configuration and registry files.

### 4. Architecture & Maintenance
*   **Zero Ruff Errors**: Completed a massive code quality overhaul using the Ruff engine. Fixed over 100 issues including bare `except` blocks, undefined names, deprecated typing annotations, and complex nested statements (`SIM102`, `SIM108`, `SIM117`). The codebase now fully adheres to modern Python 3.10+ standards.
*   **Test Suite Health**: Resolved all `ImportError` and logic regressions introduced during the architecture refactoring. The project maintains a 100% pass rate across all 29 pytest units.
*   **Installation UX Polish**: Updated `install.sh` to suggest `topo --help` upon successful installation, encouraging users to explore the full range of system optimization commands.
*   **Logic Decoupling**: Centralized all cleaning constants and refactored core file operations (process checks, registry) to `src/core/file_ops.py` to eliminate circular dependencies.
*   **Three-Layer Filtering**: Established a robust cleaning hierarchy: High-Precision (Predefined) → Heuristic (Pattern-based) → Orphan Detection (Binary-cross-referencing).
*   **Documentation Alignment**: Updated `README.md` with the new `assets/topo_home.png` screenshot and refreshed all terminal mocks. Optimized ASCII banner alignment in `src/ui/tui.py`.


---

# Daily Modification Report - 2026-05-27

## Project: topo (Topo) - Visual Identity & UX Perfection

Today's session focused on solidifying Topo's brand identity, achieving 100% parity with the "Mole" aesthetic, and resolving deep-level TUI interaction bugs.

### 1. Visual Identity & Branding
*   **Earth Theme Transition**: Officially adopted **Yellow4 / Earth (#8B8B00)** as the primary brand color. Updated the TUI banner and `src/core/constants.py` to reflect this "deep digging" aesthetic.
*   **Professional Logo Integration**: Added a high-quality badger logo (`assets/topo.png`) to the repository and redesigned the `README.md` header to be centered and visually stunning, matching the original Mole project.
*   **README Overhaul**: Completely rewritten the documentation to include centered badges, realistic terminal output mocks, and detailed technical advantage highlights.

### 2. Interaction & UX Refinement
*   **The "ESC" Breakthrough**: Resolved a critical low-level bug where an isolated ESC key would hang the TUI. Re-engineered `Navigator.get_key()` to use non-blocking `os.read` and `select` logic.
*   **Safety-First Exit Policy**: Removed the 'Q' key as a quit shortcut across all menus to prevent accidental character entry during system `sudo` prompts.
*   **Fast Navigation**: Implemented **Horizontal Arrow Key (←→)** support for rapid page switching in the application uninstaller.
*   **UI Compaction**: Consolidated multi-line footer hints into a single, high-density professional status line. Increased uninstaller list density to 15 items per page.
*   **Focused Highlighting**: Added **Bold Magenta** highlighting for the focused item in the Analyze Disk view, significantly improving navigation tracking.

### 3. Engine & Logic Hardening
*   **Intelligent Silence (Headers)**: Implemented a stdout buffering mechanism in the Clean runner. Category headers (e.g., "➤ System") are now intelligently hidden if no tasks within that category reclaim space, ensuring a zero-noise execution log.
*   **Accurate Reporting**: Fixed a critical result aggregation bug. All sub-task bytes (especially Cargo and DNF caches) are now perfectly accumulated, resulting in a 100% accurate "Total space freed" summary with a per-category breakdown.
*   **Cache Synchronization**: Implemented `ScanCache.clear()`. Performing a Clean, Purge, or Uninstall now automatically invalidates the Analyze Disk cache, ensuring that deleted items disappear instantly from the explorer.
*   **Strict Authentication**: Hardened `ensure_sudo_session` to force an explicit password prompt for every cleanup session (via `sudo -k`), while maintaining a bypass for users with permanent `NOPASSWD` rules.
*   **Ubuntu 24.04 Verification**: Conducted rigorous compatibility tests using Podman containers. Topo is now confirmed to be 100% functional on the latest Ubuntu LTS releases.
*   **Full Internationalization**: Translated every remaining Chinese comment and section header (including `.gitignore` and the `topo` launcher) into professional English.

---

# Daily Modification Report - 2026-05-26

## Project: topo (Topo) - Professional Distribution & Modular Refactoring

Today's session transformed Topo into a production-ready tool with a streamlined codebase, professional release workflow, and enhanced lifecycle management.

### 1. Architectural Refactoring (The "Slim main" Initiative)
*   **Decoupled CLI Entry**: Drastically reduced `src/main.py` from 410 lines to under 160 lines. Extracted heavy business logic into dedicated runners.
*   **Consolidated Cleaning Modules**: 
    -   Relocated App Uninstallation logic to `src/clean/app_manager.py`.
    -   Merged Project Purge logic into `src/clean/project.py`.
    -   Renamed Self-Uninstall to `src/manage/remove.py` for clarity.
*   **Legacy Cleanup**: Permanently removed the obsolete `topo.py` root script and purged all historical `lmole` references.

### 2. Lifecycle & Distribution
*   **One-Line Installer (`install.sh`)**: Implemented a sophisticated `curl | bash` installer that handles prerequisites, performs shallow clones, and executes "Smart Refinement" to delete non-runtime files (tests, reports, Rust sources).
*   **Automated Release Workflow**: Configured GitHub Actions to cross-compile x86_64 and ARM64 binaries on every version tag. Topo now pulls optimized engines from GitHub Release Assets rather than storing them in the Git history.
*   **Smart Update Command**: Introduced `topo update`, enabling users to refresh their entire installation (including binaries) with a single command.
*   **Installation Automation**: Added `topo link` to safely manage symbolic links in `~/.local/bin`.

### 3. Engine & Performance
*   **Multi-Arch Native Support**: Established full parity for **ARM64** (Apple Silicon, Raspberry Pi) and **x86_64**. The system dynamically detects CPU architecture and provisions the correct Rust engine.
*   **Intelligent Silence Policy**: Re-engineered all cleanup functions to adopt a "zero-gain silence" rule. If no space is freed, the task remains invisible to keep terminal output clean.
*   **Sudo Pre-authorization**: Moved administrative checks to the task start, ensuring the "One-Key Clean" process is never interrupted by password prompts once execution begins.

### 4. Visual Identity & Documentation
*   **New Identity**: Formally adopted the **Badger (`🦡`)** as Topo's mascot and updated the Cyber-Block TUI banner with capitalized branding.
*   **Documentation Overhaul**: Rewrote `README.md` to focus on the new automated installation method and highlighted advanced technical advantages like Multi-Arch and Zero-Interruption UI.

---

# Daily Modification Report - 2026-05-25

## Project: topo (Topo) - Extreme UX & Interaction Refinement

Today's session focused on advanced TUI interaction, responsive design, and perfecting the navigation flow for heavy users.

### 1. Interaction & Workflow
*   **Integrated Numeric Checkboxes**: Re-engineered the "Analyze Disk" selection UI to merge indices into the selection brackets (e.g., `[1]`, `[12]`). Numbers are dynamically replaced with a green `[✓]` upon selection, mirroring the elite `Uninstall` module.
*   **Multi-Digit Selection**: Implemented an intelligent digit buffering system. Users can now select any item (1-50) by quickly typing its index (e.g., press `1` then `4` to select item 14). The logic was further hardened with **Raw-Mode Buffering**, ensuring lightning-fast, non-blocking capture of numeric sequences across all terminals.
*   **Zero-Latency Navigation**: Optimized the directory traversal engine with a **State Snapshot Stack**. Returning to parent directories is now instantaneous and completely bypasses the Rust engine scan, as previous results are cached in memory.
*   **Selection Summary**: Added a persistent **"☉ Selected Items to Remove"** summary at the bottom of the analysis views (Main and Top Files), providing clear visibility of the removal queue in the signature Mole purple style.
*   **Auto-Back Logic**: Enhanced the cleaning workflow to automatically return to the parent directory when the current folder becomes empty after deletion, reducing manual keypresses.

### 3. Layout & Accessibility
*   **Official Rebranding**: Successfully transitioned the project name from `lmole` to **Topo** (derived from the Spanish word for Mole). This move defines Topo as a high-performance, independent Linux system optimizer.
*   **New Visual Identity**: Implemented a minimalist 'Console Ninja' ASCII banner with a non-slanted font, following the user's preference for a clean, professional terminal aesthetic.
*   **Global Code Refresh**: Performed a project-wide refactoring to update all module names, binary targets (`topo-core`), and documentation to reflect the new brand.
*   **CLI Interface Modernization**: Re-engineered the `topo --help` output with a professional, categorized sub-command structure.

 The UI dynamically detects terminal width, automatically shrinking or hiding progress bars and truncating filenames to prevent line wrapping on small screens.
*   **Navigation Stability**: Performed a deep-level stabilization of the arrow key capture logic. Refined the raw-mode input buffer to ensure 100% reliable 3-byte escape sequence capture across GNOME Terminal, xterm, and SSH sessions.
*   **Back Navigation Overhaul**: Added support for **B** and **H** (Vim-style) keys for returning to previous folders, alongside a clearer `← Back` UI hint.
*   **Search Revert**: Decoupled the experimental real-time search from the `Uninstall` module to restore rock-solid stability to the core navigation system while keeping the visual and alignment improvements.

---

# Daily Modification Report - 2026-05-24

## Project: topo (Topo) & Mole - Visual Identity & Smart Insights

This session established the modern visual identity and ported key intelligence features from macOS to the Linux ecosystem.

### 1. Visual Modernization
*   **Gemini-Style Progress Bars**: Replaced traditional block characters with the sleek `▬` character across both `topo` and `Mole`. Implemented a continuous, dual-tone style (Colored for usage, Gray for empty) for a premium dashboard look.
*   **CJK Character Alignment**: Solved the long-standing "jagged list" problem in terminals. Developed visual width detection (2 units for CJK, 1 for Latin) to ensure perfect vertical alignment of size columns regardless of filename language.
*   **Precise Formatting**: Optimized column spacing (5 spaces) and introduced human-centric units with proper spacing (e.g., `1.2 GB`) for maximum readability.

### 2. Intelligence & Insights
*   **Linux Hidden Space Insights**: Developed an automatic detection engine for Linux "disk killers." `topo` now elevates Docker data, package manager caches (Apt/Pacman/Dnf), and system logs to the root view with an `👀` icon.
*   **Smart Downloads Analysis**: Added an "Old Downloads (90d+)" smart view that isolates forgotten files in `~/Downloads` for targeted cleanup.
*   **Age Hints**: Integrated modification-time analysis (e.g., `>90d`, `>6mo`, `>1y`) next to items to help users identify dormant data.
*   **Smart SQLite Vacuuming**: Implemented fragmentation checks for browser databases, only triggering the heavy `VACUUM` operation when reclaimable space exceeds 10%, drastically reducing maintenance time.

### 3. Core Enhancements
*   **Multi-Select Engine**: Introduced batch deletion support to the analysis module, enabling users to select multiple directories and purge them in one action.
*   **Interactive Top Files**: Transformed the "Largest Files" list into a fully interactive selector with multi-selection and safe trash integration.
*   **System Status Dashboard**: Overhauled the system health monitor with visual bars and aggregated memory usage by application name (summing Brave/Chrome sub-threads).

---

# Daily Modification Report - 2026-05-23

## Project: topo (Topo) - Professional Polish Phase

Today's session focused on visual refinement, interactive fluidity, and deep system integration, reaching 100% parity with the macOS Mole experience while maintaining Linux-specific performance advantages.

### 1. Application Management (Uninstall) - Pixel Perfecting
*   **Visual Replication**: Re-engineered the execution UI to perfectly match the user's provided screenshots, including the purple `☉` app prefix and green `✓` file removal icons.
*   **Interactive Flow**: 100% replicated the original Mole's "Review & Confirm" flow. Replaced blocking confirmation windows with a sleek, single-line purple prompt (`→ Remove X apps...`).
*   **Ghost App Prevention (Enhanced)**: Integrated `flatpak kill` alongside `pkill -9` to ensure Flatpak apps are fully terminated and file handles released before uninstallation, preventing "Directory not empty" errors.
*   **Total Footprint Accuracy**: Now calculates the sum of binaries, configurations, and cache folders in real-time, displaying the true reclaimed space for every application.

### 2. System Maintenance (Optimize) - Advanced Porting
*   **Professional Tasks**: Ported high-impact maintenance routines from macOS Mole to Linux:
    *   **SQLite Vacuum**: Automated history/cookie database compression for Firefox, Chrome, Brave, and Edge.
    *   **Zombie Autostart Cleanup**: Automatically detects and removes broken `.desktop` entries in `~/.config/autostart`.
    *   **Smart Swap Management**: Implemented logic to intelligently reset swap space when RAM is plentiful, reducing system micro-stutter.

### 3. Monitoring (Status) - Real-time Hardware Dashboard
*   **Dynamic Hardware Detection**: Refactored GPU monitoring to dynamically scan for graphics cards (card0, card1, etc.), resolving issues where AMD/Intel GPUs were not detected on multi-card systems.
*   **Process Insight**: Integrated a "Top Processes" list showing the top 3 memory-consuming apps directly on the dashboard.
*   **Minimalist UI**: Streamlined the dashboard layout by removing redundant separator lines and manufacturer branding (e.g., SKHynix) for a cleaner "information-only" look.

### 4. TUI Branding & Identity
*   **New Brand Logo**: Implemented the "Linux Power" ASCII art banner with a bold capitalized **L** and zero-gap character spacing for a cohesive, professional look.
*   **Identity Integration**: Embedded the GitHub repository link and the "Deep clean and optimize your Linux" tagline directly into the boot sequence.

### 5. Stability & Quality Assurance
*   **Test Synchronization**: Updated the 30-test suite to align with new logic (ID-based selection persistence and Flatpak name changes).
*   **Safety Guardrails**: Hardened the `is_protected` whitelist logic to prevent "recursive root protection" and enforced strict Home Directory Isolation for all manual removal tasks.
*   **Bug Fixes**: Resolved multiple `UnboundLocalError`, `NameError`, and path resolution bugs discovered during iterative stress testing.

---

# Daily Modification Report - 2026-05-22

## Project: topo (Topo)

Today's session focused on transforming `topo` from a basic script collection into a professional-grade, high-performance system optimization tool for Linux.

### 1. Cleanup Engine (Clean)
*   **One-Key Execution**: Simplified the workflow to a single-action cleanup with real-time progress feedback.
*   **AI Model Support**: Added a first-of-its-kind cleanup category for Large Language Models (Ollama, Hugging Face, LM Studio), reclaiming gigabytes of dormant model data.
*   **Developer Tool Optimization**: Refactored `npm`, `pip`, and `go` cleanup to measure actual cache sizes before execution, ensuring accurate space-freed reporting.
*   **Docker Integration**: Added robust support for `docker system prune` with intelligent sudo detection.

### 2. Application Management (Uninstall)
*   **Performance Overhaul**: Implemented batched RPM querying for DNF apps, reducing scan times from seconds to milliseconds.
*   **Deep Residue Discovery**: 
    *   Implemented keyword extraction from `.desktop` files (Exec/Icon fields).
    *   Added fuzzy substring matching for configuration directories.
    *   Support for modern `~/.local/state` (XDG State) paths.
*   **Ghost App Prevention**: Added automatic process termination (using `flatpak kill` and `pkill -9`) to prevent uninstalled apps from lingering in the background.
*   **UI/UX Enhancements**:
    *   Implemented **Numeric Hotkeys** (1-0) for instant multi-selection.
    *   Added **Selection Highlighting** (Bold Magenta) and a **Vertical Selection Summary**.
    *   Created a detailed **Pre-removal Plan Preview** to show exactly which files will be deleted.
*   **Safety**: Enforced strict **Home Directory Isolation**, ensuring `topo` never touches system-level files outside the user's scope.

### 3. System Maintenance & Monitoring (Status & Optimize)
*   **Metric Expansion**: Added **CPU Temperature** sensing and **Battery Cycle Count** tracking.
*   **Advanced Optimization**:
    *   **SQLite Vacuuming**: Implemented automated database optimization for browsers (Firefox, Chrome, Brave, Edge), reclaiming space and improving startup speed.
    *   **Zombie Autostart Cleanup**: Automatically detects and removes broken startup entries in `~/.config/autostart`.
    *   **Intelligent Memory Management**: Added logic to reset Swap space when system RAM is under-utilized, improving overall system latency.
*   **Visual Polish**: Realigned the status dashboard for a cleaner, pixel-perfect look matching the original Mole aesthetic.

### 4. Technical Infrastructure & Stability
*   **Testing**: Built a comprehensive suite of **30 unit tests** using `pytest`, covering core logic, safety whitelists, and hardware parsing.
*   **Performance**: Integrated a `ScanCache` in the Analyze module, enabling instant navigation through directory trees scanned by the Rust engine.
*   **Bug Fixes**: 
    *   Resolved circular imports between the Analyze and Navigator modules.
    *   Fixed a critical infinite recursion bug in the configuration loader.
    *   Fixed `NameError` and `UnboundLocalError` regressions in the UI logic.
*   **Documentation**: Created a professional, full-English `README.md` and a clean `.gitignore` for GitHub deployment.

### 5. Repository & Licensing
*   **GitHub Ready**: Initialized Git repository, handled license considerations (MIT), and prepared the project for public release.
*   **Naming**: Drafted a professional inquiry letter to the original macOS Mole author for naming permission.

---
**Status**: The project is now stable, highly optimized, and ready for public debut on GitHub.
