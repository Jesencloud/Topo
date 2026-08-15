use serde_json::{Value, json};
use std::fs;
use std::path::Path;
use tempfile::tempdir;
use topo_core::{DirAgg, TOP_FILE_MIN_BYTES, compute_single, compute_stats, compute_tree};

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

    let tree = compute_tree(&root);
    assert_eq!(tree["."].total_size_bytes, 2300);
    assert_eq!(tree["."].file_count, 3);
    assert_eq!(tree["."].subdirs["a"], 2000);
    assert_eq!(tree["."].subdirs["e.bin"], 300);
    assert_eq!(tree["a"].total_size_bytes, 2000);
    assert_eq!(tree["a"].file_count, 2);
    assert_eq!(tree["a"].subdirs["b"], 1500);
    assert_eq!(tree["a"].subdirs["d.bin"], 500);
    assert_eq!(tree["a/b"].total_size_bytes, 1500);
    assert_eq!(tree["a/b"].subdirs["c.bin"], 1500);
}

#[test]
fn tree_tracks_top_files_without_extra_scan() {
    let dir = tempdir().unwrap();
    let root = dir.path().canonicalize().unwrap();
    write_file(&root.join("large.bin"), (TOP_FILE_MIN_BYTES + 1) as usize);
    // Exactly at the floor is excluded: the check is a strict `>`.
    write_file(&root.join("at_floor.bin"), TOP_FILE_MIN_BYTES as usize);

    let tree = compute_tree(&root);
    assert_eq!(tree["."].top_files.len(), 1);
    assert!(tree["."].top_files[0].path.ends_with("large.bin"));
}

#[test]
fn tree_dir_with_only_subdirs_present() {
    let dir = tempdir().unwrap();
    let root = dir.path().canonicalize().unwrap();
    write_file(&root.join("a/b/c.bin"), 700);

    let tree = compute_tree(&root);
    assert!(tree.contains_key("a"));
    assert_eq!(tree["a"].total_size_bytes, 700);
    assert_eq!(tree["a"].subdirs["b"], 700);
    assert!(!tree["a"].subdirs.contains_key("c.bin"));
}

#[test]
fn tree_deep_chain_aggregates_each_directory_once() {
    let dir = tempdir().unwrap();
    let root = dir.path().canonicalize().unwrap();
    let mut deep = root.clone();
    for index in 0..128 {
        deep = deep.join(format!("d{index}"));
    }
    write_file(&deep.join("payload.bin"), 700);
    write_file(&root.join("sibling.bin"), 300);

    let tree = compute_tree(&root);
    let deep_key = deep.strip_prefix(&root).unwrap().to_string_lossy();
    assert_eq!(tree["."].total_size_bytes, 1000);
    assert_eq!(tree["."].file_count, 2);
    assert_eq!(tree["."].subdirs["d0"], 700);
    assert_eq!(tree["."].subdirs["sibling.bin"], 300);
    assert_eq!(tree["d0"].total_size_bytes, 700);
    assert_eq!(tree["d0/d1/d2/d3"].total_size_bytes, 700);
    assert_eq!(tree[deep_key.as_ref()].total_size_bytes, 700);
}

#[test]
fn tree_excludes_zero_byte_files() {
    let dir = tempdir().unwrap();
    let root = dir.path().canonicalize().unwrap();
    write_file(&root.join("z.bin"), 0);
    write_file(&root.join("x.bin"), 100);

    let tree = compute_tree(&root);
    assert_eq!(tree["."].total_size_bytes, 100);
    assert_eq!(tree["."].file_count, 1);
    assert!(!tree["."].subdirs.contains_key("z.bin"));
}

#[test]
fn tree_keys_are_relative() {
    let dir = tempdir().unwrap();
    let root = dir.path().canonicalize().unwrap();
    write_file(&root.join("a/b.bin"), 100);

    let tree = compute_tree(&root);
    assert!(tree.contains_key("."));
    for key in tree.keys() {
        assert!(!key.starts_with('/'), "key should be relative: {key}");
    }
}

#[test]
fn tree_skips_virtual_dirs() {
    let dir = tempdir().unwrap();
    let root = dir.path().canonicalize().unwrap();
    write_file(&root.join("proc/x.bin"), 1000);
    write_file(&root.join("real/y.bin"), 500);

    let tree = compute_tree(&root);
    assert!(!tree.contains_key("proc"));
    assert!(!tree["."].subdirs.contains_key("proc"));
    assert_eq!(tree["."].total_size_bytes, 500);
}

#[test]
fn tree_does_not_follow_symlinked_dir() {
    let dir = tempdir().unwrap();
    let root = dir.path().canonicalize().unwrap();
    write_file(&root.join("target/f.bin"), 1000);
    std::os::unix::fs::symlink(root.join("target"), root.join("link")).unwrap();

    let tree = compute_tree(&root);
    assert!(!tree.contains_key("link"));
    assert!(!tree["."].subdirs.contains_key("link"));
    assert_eq!(tree["."].total_size_bytes, 1000);
}

#[test]
fn single_mode_basic() {
    let dir = tempdir().unwrap();
    let root = dir.path().canonicalize().unwrap();
    write_file(&root.join("a/b.bin"), 100);
    write_file(&root.join("c.bin"), 50);

    let result = compute_single(&root);
    assert_eq!(result.total_size_bytes, 150);
    assert_eq!(result.file_count, 2);
    assert_eq!(result.subdirs["a"], 100);
    assert_eq!(result.subdirs["c.bin"], 50);
    assert!(result.cache_estimated_bytes > 0);
}

#[test]
fn tree_exports_cache_estimates_for_every_node() {
    let dir = tempdir().unwrap();
    let root = dir.path().canonicalize().unwrap();
    write_file(&root.join("a/b.bin"), 100);

    let tree = compute_tree(&root);

    assert!(
        tree.values()
            .all(|aggregate| aggregate.cache_estimated_bytes > 0)
    );
}

/// Independent reimplementation of `ScanCache._estimate` (src/core/scan_cache.py)
/// over the JSON the Python side actually parses. Kept deliberately naive and
/// separate from the exported constants so the two models are compared, not
/// shared: a drift in either one shows up as a failure here.
fn python_estimate(value: &Value) -> u64 {
    match value {
        Value::Null => 0,
        Value::String(text) => 49 + text.len() as u64,
        Value::Number(_) | Value::Bool(_) => 28,
        Value::Array(items) => 64 + items.iter().map(python_estimate).sum::<u64>(),
        Value::Object(entries) => {
            64 + entries
                .iter()
                .map(|(key, nested)| 49 + key.len() as u64 + python_estimate(nested))
                .sum::<u64>()
        }
    }
}

/// The dict `analyze.get_rust_tree_data` caches for one `--tree` node: the
/// aggregate plus the absolute `path` it rejoins and a `top_files` default.
fn cached_tree_node(root: &Path, relative: &str, aggregate: &DirAgg) -> Value {
    let node = if relative == "." {
        root.to_path_buf()
    } else {
        root.join(relative)
    };
    let mut cached = serde_json::Map::new();
    cached.insert("path".into(), json!(node.to_string_lossy()));
    cached.insert("top_files".into(), json!([]));
    let Value::Object(fields) = serde_json::to_value(aggregate).unwrap() else {
        panic!("DirAgg must serialize to an object");
    };
    cached.extend(fields);
    Value::Object(cached)
}

#[test]
fn cache_estimate_matches_the_python_model_for_ascii_paths() {
    let dir = tempdir().unwrap();
    let root = dir.path().canonicalize().unwrap();
    write_file(&root.join("large.bin"), (TOP_FILE_MIN_BYTES + 1) as usize);
    for index in 0..40 {
        write_file(
            &root.join(format!("child-{index:03}-longish-name/inner/f.bin")),
            64,
        );
    }

    let single = compute_single(&root);
    let single_json = serde_json::to_value(&single).unwrap();
    assert_eq!(
        single.cache_estimated_bytes,
        python_estimate(&single_json),
        "--single hint must equal what ScanCache._estimate would compute"
    );

    let tree = compute_tree(&root);
    assert!(tree.len() > 40, "fixture should produce many nodes");
    for (relative, aggregate) in &tree {
        assert_eq!(
            aggregate.cache_estimated_bytes,
            python_estimate(&cached_tree_node(&root, relative, aggregate)),
            "--tree hint mismatch at {relative:?}; the path Python rejoins is charged here"
        );
    }
}

#[test]
fn cache_estimate_stays_conservative_for_non_ascii_paths() {
    let dir = tempdir().unwrap();
    let root = dir.path().canonicalize().unwrap();
    for index in 0..20 {
        write_file(&root.join(format!("目录-{index}-中文名称/f.bin")), 64);
    }

    // `_estimate` counts UTF-8 bytes while CPython stores up to 4 bytes per
    // character, so here the exported hint must come out strictly higher.
    let single = compute_single(&root);
    assert!(
        single.cache_estimated_bytes > python_estimate(&serde_json::to_value(&single).unwrap())
    );
    for (relative, aggregate) in &compute_tree(&root) {
        assert!(
            aggregate.cache_estimated_bytes
                >= python_estimate(&cached_tree_node(&root, relative, aggregate)),
            "non-ASCII node {relative:?} must never be under-charged"
        );
    }
}

#[test]
fn stats_reports_size_and_activity() {
    let dir = tempdir().unwrap();
    let root = dir.path().canonicalize().unwrap();
    write_file(&root.join("a/b.bin"), 100);

    let stats = compute_stats(&root);
    assert_eq!(stats.total_size_bytes, 100);
    assert_eq!(stats.file_count, 1);
    assert!(stats.newest_activity_secs > 0);
}

#[test]
fn stats_counts_named_content_that_size_scans_intentionally_skip() {
    let dir = tempdir().unwrap();
    let root = dir.path().canonicalize().unwrap();
    write_file(&root.join("run/session.bin"), 100);
    write_file(&root.join("media/cache.bin"), 200);
    write_file(&root.join("regular.bin"), 300);

    let stats = compute_stats(&root);
    let tree = compute_tree(&root);

    // --stats measures a specific cache/residue path, where these names are
    // ordinary content rather than virtual filesystem mount points.
    assert_eq!(stats.total_size_bytes, 600);
    assert_eq!(stats.file_count, 3);

    // Size/tree scans retain the existing safety policy for broad scan roots.
    assert_eq!(tree["."].total_size_bytes, 300);
    assert_eq!(tree["."].file_count, 1);
    assert!(!tree["."].subdirs.contains_key("run"));
    assert!(!tree["."].subdirs.contains_key("media"));
}
