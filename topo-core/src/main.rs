use std::env;
use std::path::PathBuf;
use std::time::Instant;
use topo_core::{DEFAULT_TREE_MIN_BYTES, run_single, run_stats, run_tree};

fn main() {
    let args: Vec<String> = env::args().collect();
    if args.len() < 2 {
        eprintln!("Usage: topo-core [--tree] <path> [--min-bytes N]");
        std::process::exit(1);
    }

    let tree_mode = args[1] == "--tree";
    let stats_mode = args[1] == "--stats";
    let raw_root = if tree_mode || stats_mode {
        match args.get(2) {
            Some(path) => path,
            None => {
                eprintln!("Usage: topo-core --tree <path> [--min-bytes N]");
                std::process::exit(1);
            }
        }
    } else {
        &args[1]
    };

    // --min-bytes is a trailing tree-mode flag; only run_tree honours it. Scan
    // for it strictly after the positional path, so a directory literally named
    // "--min-bytes" is scanned rather than mistaken for the flag. Python's own
    // calls never pass it, so this only hardens manual invocation.
    let flag_start = if tree_mode || stats_mode { 3 } else { 2 };
    let mut min_bytes = DEFAULT_TREE_MIN_BYTES;
    if let Some(offset) = args
        .get(flag_start..)
        .unwrap_or(&[])
        .iter()
        .position(|arg| arg == "--min-bytes")
        && let Some(value) = args.get(flag_start + offset + 1)
        && let Ok(parsed) = value.parse::<u64>()
    {
        min_bytes = parsed;
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
    } else if stats_mode {
        run_stats(&root_path);
    } else {
        run_single(&root_path);
    }
    eprintln!(
        "Scan of {:?} completed in {:?}",
        root_path,
        start_time.elapsed()
    );
}
