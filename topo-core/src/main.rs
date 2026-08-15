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

    let mut min_bytes = DEFAULT_TREE_MIN_BYTES;
    if let Some(position) = args.iter().position(|arg| arg == "--min-bytes")
        && let Some(value) = args.get(position + 1)
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
