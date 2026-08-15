//! Filesystem scanning and aggregation for the three engine modes.
//!
//! `compute_single` / `compute_tree` / `compute_stats` do the work; the `run_*`
//! wrappers serialize their results to stdout as JSON for the Python front end.

use jwalk::WalkDir;
use serde::{Deserialize, Serialize};
use std::cmp::Ordering;
use std::collections::{BinaryHeap, HashMap};
use std::io::Write;
use std::path::Path;
use std::time::UNIX_EPOCH;

pub const DEFAULT_TREE_MIN_BYTES: u64 = 1_048_576; // 1 MiB
/// Minimum size for a file to enter a `top_files` list.
pub const TOP_FILE_MIN_BYTES: u64 = 1_048_576; // 1 MiB
// Safety list - skip virtual and system-reserved directories.
const SKIP_DIR_NAMES: [&str; 7] = ["proc", "sys", "dev", "run", "mnt", "media", "lost+found"];

#[derive(Serialize, Deserialize)]
pub struct ScanResult {
    pub path: String,
    pub total_size_bytes: u64,
    pub file_count: u64,
    pub top_files: Vec<FileInfo>,
    pub subdirs: HashMap<String, u64>,
    #[serde(default, rename = "_cache_estimated_bytes")]
    pub cache_estimated_bytes: u64,
}

#[derive(Serialize, Deserialize, Clone, Eq, PartialEq)]
pub struct FileInfo {
    pub path: String,
    pub size_bytes: u64,
}

// Per-directory aggregate emitted by `--tree` mode. It has no `path` field;
// `top_files` is populated only on the root aggregate. Keys are relative to the
// scan root so Python can rejoin them onto the original (possibly symlinked) root.
#[derive(Serialize, Deserialize, Default)]
pub struct DirAgg {
    pub total_size_bytes: u64,
    pub file_count: u64,
    pub subdirs: HashMap<String, u64>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub top_files: Vec<FileInfo>,
    #[serde(default, rename = "_cache_estimated_bytes")]
    pub cache_estimated_bytes: u64,
}

#[derive(Serialize, Deserialize, Default)]
pub struct PathStats {
    pub total_size_bytes: u64,
    pub file_count: u64,
    pub newest_activity_secs: u64,
}

// Implement custom ordering to make BinaryHeap a Min-Heap for size_bytes
impl Ord for FileInfo {
    fn cmp(&self, other: &Self) -> Ordering {
        // Reverse order: smaller size has higher priority (will be popped first)
        other
            .size_bytes
            .cmp(&self.size_bytes)
            .then_with(|| self.path.cmp(&other.path))
    }
}

impl PartialOrd for FileInfo {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

/// Byte model mirrored from `ScanCache._estimate` in `src/core/scan_cache.py`:
/// a `dict` or `list` costs 64, an `int` costs 28, and a `str` costs its UTF-8
/// length plus 49. Exporting a total built from the *same* model is what lets
/// `ScanCache.set()` skip re-walking the parsed result: the hint is the number
/// the generic estimator would have produced, so the 64 MiB budget keeps the
/// accounting it was calibrated against instead of a second, looser one.
const PY_CONTAINER_BYTES: u64 = 64;
const PY_INT_BYTES: u64 = 28;
const PY_STR_HEADER_BYTES: u64 = 49;

/// Keys of the dict Python caches for one directory. `--single` emits all of
/// these; for `--tree`, `analyze.get_rust_tree_data` adds `path` and a
/// `top_files` default before caching, so the key set is identical either way.
const CACHED_KEYS: [&str; 6] = [
    "path",
    "total_size_bytes",
    "file_count",
    "top_files",
    "subdirs",
    "_cache_estimated_bytes",
];

/// Everything in a cached directory dict except the path string, the `subdirs`
/// entries and the `top_files` entries: the dict itself, the nested `top_files`
/// list and `subdirs` dict, all six key strings, and the three integer values.
const fn scan_dict_fixed_bytes() -> u64 {
    let mut total = PY_CONTAINER_BYTES * 3 + PY_INT_BYTES * 3;
    let mut index = 0;
    while index < CACHED_KEYS.len() {
        total += PY_STR_HEADER_BYTES + CACHED_KEYS[index].len() as u64;
        index += 1;
    }
    total
}

/// A `FileInfo` becomes a two-key dict; its `path` string is charged separately.
const fn file_dict_fixed_bytes() -> u64 {
    PY_CONTAINER_BYTES
        + PY_INT_BYTES
        + (PY_STR_HEADER_BYTES + "path".len() as u64)
        + (PY_STR_HEADER_BYTES + "size_bytes".len() as u64)
}

/// Python stores a `str` as a header plus one to four bytes per character.
/// ASCII matches `_estimate` exactly, which is the overwhelmingly common case
/// for paths; non-ASCII is charged the worst case, covering the one place
/// `_estimate` is itself optimistic (it counts UTF-8 bytes, not code units).
fn estimated_str_bytes(value: &str) -> u64 {
    let payload = if value.is_ascii() {
        value.len() as u64
    } else {
        (value.chars().count() as u64).saturating_mul(4)
    };
    PY_STR_HEADER_BYTES.saturating_add(payload)
}

/// Cost of the absolute path string Python rebuilds for a `--tree` node
/// (`scan_root / relative`), charged without materializing the join.
fn estimated_joined_path_bytes(root: &str, relative: &str) -> u64 {
    if relative == "." {
        return estimated_str_bytes(root);
    }
    estimated_str_bytes(root)
        .saturating_add(estimated_str_bytes(relative))
        .saturating_sub(PY_STR_HEADER_BYTES)
        .saturating_add(1) // the path separator between the two halves
}

/// Total bytes the Python dict for one directory is expected to occupy.
/// `path_bytes` is that directory's already-computed path-string cost, because
/// `--single` owns the string while `--tree` only knows the two halves of it.
fn estimated_scan_bytes(
    path_bytes: u64,
    subdirs: &HashMap<String, u64>,
    top_files: &[FileInfo],
) -> u64 {
    let mut total = scan_dict_fixed_bytes().saturating_add(path_bytes);
    for name in subdirs.keys() {
        total = total
            .saturating_add(estimated_str_bytes(name))
            .saturating_add(PY_INT_BYTES);
    }
    for file in top_files {
        total = total
            .saturating_add(file_dict_fixed_bytes())
            .saturating_add(estimated_str_bytes(&file.path));
    }
    total
}

/// Walker used by every mode that reports *sizes*, so `--single` and `--tree`
/// necessarily agree on the skip-list / symlink / hidden-file rules. Change the
/// traversal policy here and both modes move together.
///
/// `compute_stats` deliberately does not use this: it measures one specific
/// path the user asked about (a cache or residue directory), where a child that
/// happens to be named `run` or `media` is real content and should be counted,
/// and where the virtual filesystems this list guards against cannot appear.
fn scan_walker(root_path: &Path) -> WalkDir {
    WalkDir::new(root_path)
        .skip_hidden(false)
        .follow_links(false)
        .process_read_dir(|_depth, _path, _read_dir_state, children| {
            children.retain(|child| {
                if let Ok(entry) = child {
                    let name = entry.file_name.to_string_lossy();
                    !SKIP_DIR_NAMES.iter().any(|&skip| name == skip)
                } else {
                    false
                }
            });
        })
}

/// Split a root-relative path into (parent key, own name). A top-level entry's
/// parent is the scan root, whose key is `"."`.
fn split_parent(relative: &Path) -> Option<(String, String)> {
    let name = relative.file_name()?.to_string_lossy().into_owned();
    let parent = relative
        .parent()
        .filter(|parent| !parent.as_os_str().is_empty())
        .map_or_else(
            || ".".to_string(),
            |parent| parent.to_string_lossy().into_owned(),
        );
    Some((parent, name))
}

/// Recursively walk `root_path` and invoke `on_file(path, size)` for every
/// regular file with size > 0.
fn walk_files<F: FnMut(&Path, u64)>(root_path: &Path, mut on_file: F) {
    for entry in scan_walker(root_path)
        .into_iter()
        .filter_map(|entry| entry.ok())
    {
        if entry.file_type.is_file() {
            let size = entry.metadata().map(|metadata| metadata.len()).unwrap_or(0);
            if size > 0 {
                on_file(&entry.path(), size);
            }
        }
    }
}

/// Single-level scan (default mode): total size, file count, top-100 large
/// files, and one level of immediate-child sizes.
pub fn compute_single(root_path: &Path) -> ScanResult {
    let mut total_size = 0u64;
    let mut file_count = 0u64;
    let mut top_files_heap: BinaryHeap<FileInfo> = BinaryHeap::with_capacity(101);
    let mut subdir_sizes: HashMap<String, u64> = HashMap::new();

    walk_files(root_path, |path, size| {
        total_size = total_size.saturating_add(size);
        file_count = file_count.saturating_add(1);

        if let Ok(relative) = path.strip_prefix(root_path)
            && let Some(first_component) = relative.components().next()
        {
            let name = first_component.as_os_str().to_string_lossy().into_owned();
            let entry = subdir_sizes.entry(name).or_insert(0);
            *entry = entry.saturating_add(size);
        }

        if size > TOP_FILE_MIN_BYTES {
            top_files_heap.push(FileInfo {
                path: path.to_string_lossy().into_owned(),
                size_bytes: size,
            });
            if top_files_heap.len() > 100 {
                top_files_heap.pop();
            }
        }
    });

    let mut top_files = top_files_heap.into_sorted_vec();
    top_files.reverse();
    let path = root_path.to_string_lossy().into_owned();
    let cache_estimated_bytes =
        estimated_scan_bytes(estimated_str_bytes(&path), &subdir_sizes, &top_files);
    ScanResult {
        path,
        total_size_bytes: total_size,
        file_count,
        top_files,
        subdirs: subdir_sizes,
        cache_estimated_bytes,
    }
}

/// Whole-subtree scan: in a single walk, aggregate size/file_count and the
/// immediate-children map for EVERY directory level, keyed by a path relative
/// to `root_path` ("." is the root). Drilling into any cached level then needs
/// no rescan.
///
/// Runs in O(entries + directories): files are charged only to their direct
/// parent, then each directory aggregate is folded into its parent exactly
/// once, deepest level first. Charging every ancestor per file instead would
/// cost O(files x depth), which is what deep trees used to pay.
pub fn compute_tree(root_path: &Path) -> HashMap<String, DirAgg> {
    let mut dirs: HashMap<String, DirAgg> = HashMap::new();
    let mut directory_levels: Vec<Vec<String>> = Vec::new();
    let mut top_files_heap: BinaryHeap<FileInfo> = BinaryHeap::with_capacity(101);
    dirs.entry(".".to_string()).or_default(); // root always present

    for entry in scan_walker(root_path)
        .into_iter()
        .filter_map(|entry| entry.ok())
    {
        let path = entry.path();
        if path == root_path {
            continue;
        }
        let Ok(relative) = path.strip_prefix(root_path) else {
            continue;
        };

        if entry.file_type.is_dir() {
            // Bucket by walk depth so the fold below can go deepest-first.
            let key = relative.to_string_lossy().into_owned();
            let depth = entry.depth();
            if directory_levels.len() <= depth {
                directory_levels.resize_with(depth + 1, Vec::new);
            }
            directory_levels[depth].push(key.clone());
            dirs.entry(key).or_default();
            continue;
        }
        if !entry.file_type.is_file() {
            continue;
        }

        let size = entry.metadata().map(|metadata| metadata.len()).unwrap_or(0);
        if size == 0 {
            continue;
        }
        if size > TOP_FILE_MIN_BYTES {
            top_files_heap.push(FileInfo {
                path: path.to_string_lossy().into_owned(),
                size_bytes: size,
            });
            if top_files_heap.len() > 100 {
                top_files_heap.pop();
            }
        }

        // Charge the file to its direct parent only; ancestors pick it up in the fold.
        let Some((parent_key, file_name)) = split_parent(relative) else {
            continue;
        };
        let aggregate = dirs.entry(parent_key).or_default();
        aggregate.total_size_bytes = aggregate.total_size_bytes.saturating_add(size);
        aggregate.file_count = aggregate.file_count.saturating_add(1);
        let child = aggregate.subdirs.entry(file_name).or_insert(0);
        *child = child.saturating_add(size);
    }

    // Fold deepest level first, so a directory's descendants have all landed in
    // it before it is folded into its own parent. Every directory is read once
    // and written once, which is what keeps this O(directories).
    for depth in (1..directory_levels.len()).rev() {
        for key in &directory_levels[depth] {
            let Some(child_aggregate) = dirs.get(key) else {
                continue;
            };
            let child_size = child_aggregate.total_size_bytes;
            let child_files = child_aggregate.file_count;
            // Nothing below it: stay absent from the parent's children, matching
            // a plain file-driven aggregation. Zero size implies zero files,
            // since zero-byte files never reach the aggregate above.
            if child_size == 0 {
                continue;
            }
            let Some((parent_key, child_name)) = split_parent(Path::new(key)) else {
                continue;
            };
            let parent = dirs.entry(parent_key).or_default();
            parent.total_size_bytes = parent.total_size_bytes.saturating_add(child_size);
            parent.file_count = parent.file_count.saturating_add(child_files);
            let child = parent.subdirs.entry(child_name).or_insert(0);
            *child = child.saturating_add(child_size);
        }
    }

    // Directories enumerated above but holding nothing are dropped, so the key
    // set stays exactly "directories with content", plus the root.
    dirs.retain(|key, aggregate| key == "." || aggregate.total_size_bytes > 0);
    let mut top_files = top_files_heap.into_sorted_vec();
    top_files.reverse();
    if let Some(root) = dirs.get_mut(".") {
        root.top_files = top_files;
    }
    // Python rejoins each relative key onto the scan root and caches the result
    // with that absolute `path` string, so charge it here -- the aggregate itself
    // carries no path field to measure.
    let root = root_path.to_string_lossy();
    for (relative, aggregate) in &mut dirs {
        aggregate.cache_estimated_bytes = estimated_scan_bytes(
            estimated_joined_path_bytes(&root, relative),
            &aggregate.subdirs,
            &aggregate.top_files,
        );
    }
    dirs
}

/// Size, file count and newest access/modify time for one specific path.
///
/// Uses a bare walker rather than [`scan_walker`] on purpose -- see that
/// function's note on why the skip list does not apply to this mode.
pub fn compute_stats(root_path: &Path) -> PathStats {
    let mut stats = PathStats::default();
    let walker = WalkDir::new(root_path)
        .skip_hidden(false)
        .follow_links(false);
    for entry in walker.into_iter().filter_map(|entry| entry.ok()) {
        if entry.path() == root_path {
            continue;
        }
        let Ok(metadata) = entry.metadata() else {
            continue;
        };
        for activity in [metadata.accessed(), metadata.modified()] {
            if let Ok(seconds) = activity
                .and_then(|time| {
                    time.duration_since(UNIX_EPOCH)
                        .map_err(std::io::Error::other)
                })
                .map(|duration| duration.as_secs())
            {
                stats.newest_activity_secs = stats.newest_activity_secs.max(seconds);
            }
        }
        if entry.file_type.is_file() {
            let size = metadata.len();
            if size > 0 {
                stats.total_size_bytes = stats.total_size_bytes.saturating_add(size);
                stats.file_count = stats.file_count.saturating_add(1);
            }
        }
    }
    stats
}

pub fn run_single(root_path: &Path) {
    let stdout = std::io::stdout();
    let mut output = stdout.lock();
    let _ = serde_json::to_writer(&mut output, &compute_single(root_path));
    let _ = writeln!(output);
}

/// Emit the tree aggregates as JSON, pruned by `min_bytes`.
///
/// Threshold prune: only directories >= min_bytes get their own cache node
/// (drilling into them is then an instant cache hit). The root is always
/// emitted, and every node's `subdirs` still lists all immediate children, so
/// the listing is complete; drilling into a pruned small dir falls back to a
/// cheap on-demand scan on the Python side.
pub fn run_tree(root_path: &Path, min_bytes: u64) {
    let dirs = compute_tree(root_path);
    let output: HashMap<String, &DirAgg> = dirs
        .iter()
        .filter(|(key, aggregate)| key.as_str() == "." || aggregate.total_size_bytes >= min_bytes)
        .map(|(key, aggregate)| (key.clone(), aggregate))
        .collect();
    let stdout = std::io::stdout();
    let mut writer = stdout.lock();
    let _ = serde_json::to_writer(&mut writer, &output);
    let _ = writeln!(writer);
}

pub fn run_stats(root_path: &Path) {
    let stdout = std::io::stdout();
    let mut output = stdout.lock();
    let _ = serde_json::to_writer(&mut output, &compute_stats(root_path));
    let _ = writeln!(output);
}
