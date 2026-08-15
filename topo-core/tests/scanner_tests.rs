use std::fs;
use std::path::Path;
use tempfile::tempdir;
use topo_core::{TOP_FILE_MIN_BYTES, compute_single, compute_stats, compute_tree};

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
