use jwalk::WalkDir;
use serde::{Deserialize, Serialize};
use std::cmp::Ordering;
use std::collections::{BinaryHeap, HashMap};
use std::env;
use std::path::{Path, PathBuf};
use std::time::Instant;

const DEFAULT_TREE_MIN_BYTES: u64 = 1_048_576; // 1 MiB

#[derive(Serialize, Deserialize)]
struct ScanResult {
    path: String,
    total_size_bytes: u64,
    file_count: u64,
    top_files: Vec<FileInfo>,
    subdirs: HashMap<String, u64>,
}

#[derive(Serialize, Deserialize, Clone, Eq, PartialEq)]
struct FileInfo {
    path: String,
    size_bytes: u64,
}

// Per-directory aggregate emitted by `--tree` mode. A strict subset of
// ScanResult (no `path`/`top_files`), keyed by a path relative to the scan
// root so Python can rejoin it onto the original (possibly symlinked) root.
#[derive(Serialize, Deserialize, Default)]
struct DirAgg {
    total_size_bytes: u64,
    file_count: u64,
    subdirs: HashMap<String, u64>,
}

// Implement custom ordering to make BinaryHeap a Min-Heap for size_bytes
impl Ord for FileInfo {
    fn cmp(&self, other: &Self) -> Ordering {
        // Reverse order: smaller size has higher priority (will be popped first)
        other.size_bytes.cmp(&self.size_bytes)
            .then_with(|| self.path.cmp(&other.path))
    }
}

impl PartialOrd for FileInfo {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

/// Recursively walk `root_path` and invoke `on_file(path, size)` for every
/// regular file with size > 0. Shared by both scan modes so they apply the
/// exact same skip-list / symlink / hidden-file rules.
fn walk_files<F: FnMut(&Path, u64)>(root_path: &Path, mut on_file: F) {
    // Safety list - skip virtual and system-reserved directories
    let skip_list = ["proc", "sys", "dev", "run", "mnt", "media", "lost+found"];

    let walker = WalkDir::new(root_path)
        .skip_hidden(false)
        .follow_links(false)
        .process_read_dir(move |_depth, _path, _read_dir_state, children| {
            children.retain(|child| {
                if let Ok(entry) = child {
                    let name = entry.file_name.to_string_lossy();
                    !skip_list.iter().any(|&s| name == s)
                } else {
                    false
                }
            });
        });

    for entry in walker.into_iter().filter_map(|e| e.ok()) {
        if entry.file_type.is_file() {
            let size = entry.metadata().map(|m| m.len()).unwrap_or(0);
            if size > 0 {
                on_file(&entry.path(), size);
            }
        }
    }
}

/// Single-level scan (default mode): total size, file count, top-100 large
/// files, and one level of immediate-child sizes. Output is unchanged from the
/// original implementation so other callers keep working.
fn compute_single(root_path: &Path) -> ScanResult {
    let mut total_size = 0u64;
    let mut file_count = 0u64;

    // Use a Min-Heap to track the top 100 largest files efficiently
    let mut top_files_heap: BinaryHeap<FileInfo> = BinaryHeap::with_capacity(101);
    let mut subdir_sizes: HashMap<String, u64> = HashMap::new();

    walk_files(root_path, |path, size| {
        total_size += size;
        file_count += 1;

        // 1. Attribute size to top-level subdirectory
        if let Ok(rel_path) = path.strip_prefix(root_path) {
            if let Some(first_comp) = rel_path.components().next() {
                let subdir_name = first_comp.as_os_str().to_string_lossy().into_owned();
                *subdir_sizes.entry(subdir_name).or_insert(0) += size;
            }
        }

        // 2. Track top 100 files (> 1MB)
        if size > 1_000_000 {
            let info = FileInfo {
                path: path.to_string_lossy().into_owned(),
                size_bytes: size,
            };
            top_files_heap.push(info);
            if top_files_heap.len() > 100 {
                top_files_heap.pop();
            }
        }
    });

    // Convert heap to sorted vector (Largest first)
    let mut top_files: Vec<FileInfo> = top_files_heap.into_sorted_vec();
    top_files.reverse();

    ScanResult {
        path: root_path.to_string_lossy().into_owned(),
        total_size_bytes: total_size,
        file_count,
        top_files,
        subdirs: subdir_sizes,
    }
}

/// Whole-subtree scan: in a single walk, aggregate size/file_count and the
/// immediate-children map for EVERY directory level, keyed by a path relative
/// to `root_path` ("." is the root). Drilling into any cached level then needs
/// no rescan.
fn compute_tree(root_path: &Path) -> HashMap<String, DirAgg> {
    let mut dirs: HashMap<String, DirAgg> = HashMap::new();
    dirs.entry(".".to_string()).or_default(); // root always present

    walk_files(root_path, |path, size| {
        let rel = match path.strip_prefix(root_path) {
            Ok(r) => r,
            Err(_) => return,
        };
        let comps: Vec<String> = rel
            .components()
            .map(|c| c.as_os_str().to_string_lossy().into_owned())
            .collect();
        if comps.is_empty() {
            return;
        }
        let n = comps.len();

        // Root "." gets the file's size/count, with comps[0] as its child.
        let root_agg = dirs.entry(".".to_string()).or_default();
        root_agg.total_size_bytes += size;
        root_agg.file_count += 1;
        *root_agg.subdirs.entry(comps[0].clone()).or_insert(0) += size;

        // Descend through each intermediate directory: comps[0], comps[0]/comps[1],
        // ... comps[0]/.../comps[n-2]. Each gets the size and a child entry toward
        // the file (the last such child is the filename itself).
        let mut prefix = String::new();
        for i in 0..(n - 1) {
            if prefix.is_empty() {
                prefix = comps[i].clone();
            } else {
                prefix.push('/');
                prefix.push_str(&comps[i]);
            }
            let agg = dirs.entry(prefix.clone()).or_default();
            agg.total_size_bytes += size;
            agg.file_count += 1;
            *agg.subdirs.entry(comps[i + 1].clone()).or_insert(0) += size;
        }
    });

    dirs
}

fn run_single(root_path: &Path) {
    let result = compute_single(root_path);
    if let Ok(json) = serde_json::to_string(&result) {
        println!("{}", json);
    }
}

fn run_tree(root_path: &Path, min_bytes: u64) {
    let dirs = compute_tree(root_path);
    // Threshold prune: only directories >= min_bytes get their own cache node
    // (drilling into them is then an instant cache hit). The root is always
    // emitted, and every node's `subdirs` still lists all immediate children,
    // so the listing is complete; drilling into a pruned small dir falls back
    // to a cheap on-demand scan on the Python side.
    let out: HashMap<String, &DirAgg> = dirs
        .iter()
        .filter(|(k, v)| k.as_str() == "." || v.total_size_bytes >= min_bytes)
        .map(|(k, v)| (k.clone(), v))
        .collect();
    if let Ok(json) = serde_json::to_string(&out) {
        println!("{}", json);
    }
}

fn main() {
    let args: Vec<String> = env::args().collect();
    if args.len() < 2 {
        eprintln!("Usage: topo-core [--tree] <path> [--min-bytes N]");
        std::process::exit(1);
    }

    let tree_mode = args[1] == "--tree";
    let raw_root = if tree_mode {
        match args.get(2) {
            Some(p) => p,
            None => {
                eprintln!("Usage: topo-core --tree <path> [--min-bytes N]");
                std::process::exit(1);
            }
        }
    } else {
        &args[1]
    };

    // Optional `--min-bytes N` (tree mode); defaults to 1 MiB.
    let mut min_bytes = DEFAULT_TREE_MIN_BYTES;
    if let Some(pos) = args.iter().position(|a| a == "--min-bytes") {
        if let Some(v) = args.get(pos + 1) {
            if let Ok(n) = v.parse::<u64>() {
                min_bytes = n;
            }
        }
    }

    let root_path = PathBuf::from(raw_root)
        .canonicalize()
        .unwrap_or_else(|_| PathBuf::from(raw_root));

    if !root_path.exists() {
        eprintln!("Error: Path does not exist");
        std::process::exit(1);
    }

    let start_time = Instant::now();

    if tree_mode {
        run_tree(&root_path, min_bytes);
    } else {
        run_single(&root_path);
    }

    eprintln!("Scan of {:?} completed in {:?}", root_path, start_time.elapsed());
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::path::Path;
    use tempfile::tempdir;

    fn write_file(path: &Path, bytes: usize) {
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(path, vec![b'x'; bytes]).unwrap();
    }

    #[test]
    fn tree_totals_file_counts_and_subdirs() {
        let dir = tempdir().unwrap();
        let root = dir.path().canonicalize().unwrap();
        write_file(&root.join("a/b/c.bin"), 1500);
        write_file(&root.join("a/d.bin"), 500);
        write_file(&root.join("e.bin"), 300);

        let t = compute_tree(&root);

        // Root aggregates everything; immediate children are `a` and `e.bin`.
        assert_eq!(t["."].total_size_bytes, 2300);
        assert_eq!(t["."].file_count, 3);
        assert_eq!(t["."].subdirs["a"], 2000);
        assert_eq!(t["."].subdirs["e.bin"], 300);

        // `a` has a subdir `b` and a direct file `d.bin`.
        assert_eq!(t["a"].total_size_bytes, 2000);
        assert_eq!(t["a"].file_count, 2);
        assert_eq!(t["a"].subdirs["b"], 1500);
        assert_eq!(t["a"].subdirs["d.bin"], 500);

        // Deepest dir lists the file as its child.
        assert_eq!(t["a/b"].total_size_bytes, 1500);
        assert_eq!(t["a/b"].subdirs["c.bin"], 1500);
    }

    #[test]
    fn tree_dir_with_only_subdirs_present() {
        let dir = tempdir().unwrap();
        let root = dir.path().canonicalize().unwrap();
        write_file(&root.join("a/b/c.bin"), 700);

        let t = compute_tree(&root);

        assert!(t.contains_key("a"));
        assert_eq!(t["a"].total_size_bytes, 700);
        // `a`'s only immediate child is the dir `b`, not the deep file.
        assert_eq!(t["a"].subdirs["b"], 700);
        assert!(!t["a"].subdirs.contains_key("c.bin"));
    }

    #[test]
    fn tree_excludes_zero_byte_files() {
        let dir = tempdir().unwrap();
        let root = dir.path().canonicalize().unwrap();
        write_file(&root.join("z.bin"), 0);
        write_file(&root.join("x.bin"), 100);

        let t = compute_tree(&root);

        assert_eq!(t["."].total_size_bytes, 100);
        assert_eq!(t["."].file_count, 1);
        assert!(!t["."].subdirs.contains_key("z.bin"));
    }

    #[test]
    fn tree_keys_are_relative() {
        let dir = tempdir().unwrap();
        let root = dir.path().canonicalize().unwrap();
        write_file(&root.join("a/b.bin"), 100);

        let t = compute_tree(&root);

        assert!(t.contains_key("."));
        for k in t.keys() {
            assert!(!k.starts_with('/'), "key should be relative: {k}");
        }
    }

    #[test]
    fn tree_skips_virtual_dirs() {
        let dir = tempdir().unwrap();
        let root = dir.path().canonicalize().unwrap();
        write_file(&root.join("proc/x.bin"), 1000);
        write_file(&root.join("real/y.bin"), 500);

        let t = compute_tree(&root);

        assert!(!t.contains_key("proc"));
        assert!(!t["."].subdirs.contains_key("proc"));
        assert_eq!(t["."].total_size_bytes, 500);
    }

    #[test]
    fn tree_does_not_follow_symlinked_dir() {
        let dir = tempdir().unwrap();
        let root = dir.path().canonicalize().unwrap();
        write_file(&root.join("target/f.bin"), 1000);
        std::os::unix::fs::symlink(root.join("target"), root.join("link")).unwrap();

        let t = compute_tree(&root);

        assert!(!t.contains_key("link"));
        assert!(!t["."].subdirs.contains_key("link"));
        // Only the real directory is counted.
        assert_eq!(t["."].total_size_bytes, 1000);
    }

    #[test]
    fn single_mode_basic() {
        let dir = tempdir().unwrap();
        let root = dir.path().canonicalize().unwrap();
        write_file(&root.join("a/b.bin"), 100);
        write_file(&root.join("c.bin"), 50);

        let r = compute_single(&root);

        assert_eq!(r.total_size_bytes, 150);
        assert_eq!(r.file_count, 2);
        assert_eq!(r.subdirs["a"], 100);
        assert_eq!(r.subdirs["c.bin"], 50);
    }
}
