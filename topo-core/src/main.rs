use jwalk::WalkDir;
use serde::{Deserialize, Serialize};
use std::env;
use std::path::{Path, PathBuf};
use std::collections::HashMap;
use std::time::Instant;
use std::ffi::OsString;

#[derive(Serialize, Deserialize)]
struct ScanResult {
    path: String,
    total_size_bytes: u64,
    file_count: u64,
    top_files: Vec<FileInfo>,
    subdirs: HashMap<String, u64>,
}

#[derive(Serialize, Deserialize, Clone)]
struct FileInfo {
    path: String,
    size_bytes: u64,
}

fn main() {
    let args: Vec<String> = env::args().collect();
    if args.len() < 2 {
        eprintln!("Usage: lmo-core <path>");
        std::process::exit(1);
    }

    let root_path = PathBuf::from(&args[1]).canonicalize().unwrap_or_else(|_| PathBuf::from(&args[1]));
    if !root_path.exists() {
        eprintln!("Error: Path does not exist");
        std::process::exit(1);
    }

    let start_time = Instant::now();
    let mut total_size = 0u64;
    let mut file_count = 0u64;
    let mut all_files: Vec<FileInfo> = Vec::with_capacity(5000);
    let mut subdir_sizes: HashMap<OsString, u64> = HashMap::new();

    let root_components_count = root_path.components().count();

    // Safety list - skip virtual and system-reserved directories
    let skip_list = [
        "proc", "sys", "dev", "run", "mnt", "media", "lost+found"
    ];

    let walker = WalkDir::new(&root_path)
        .skip_hidden(true)
        .follow_links(false)
        .process_read_dir(move |_depth, _path, _read_dir_state, children| {
            children.retain(|child| {
                if let Ok(entry) = child {
                    let name = entry.file_name.to_string_lossy();
                    !skip_list.iter().any(|&s| name == s)
                } else { false }
            });
        });

    for entry in walker.into_iter().filter_map(|e| e.ok()) {
        if entry.file_type.is_file() {
            let size = entry.metadata().map(|m| m.len()).unwrap_or(0);
            if size > 0 {
                total_size += size;
                file_count += 1;

                // Attribute size to immediate subdirectory
                // Optimization: Use depth and parent_path to avoid full path allocation
                let depth = entry.depth;
                if depth == 1 {
                    *subdir_sizes.entry(entry.file_name.clone()).or_insert(0) += size;
                } else if depth > 1 {
                    if let Some(first_comp) = entry.parent_path.components().nth(root_components_count) {
                        *subdir_sizes.entry(first_comp.as_os_str().to_owned()).or_insert(0) += size;
                    }
                }

                if size > 1_000_000 {
                    all_files.push(FileInfo {
                        path: entry.path().to_string_lossy().into_owned(),
                        size_bytes: size,
                    });
                }
            }
        }
    }

    all_files.sort_by(|a, b| b.size_bytes.cmp(&a.size_bytes));
    let top_files = all_files.into_iter().take(100).collect();

    // Convert OsString keys to String for JSON serialization
    let subdirs_final: HashMap<String, u64> = subdir_sizes
        .into_iter()
        .map(|(k, v)| (k.to_string_lossy().into_owned(), v))
        .collect();

    let result = ScanResult {
        path: root_path.to_string_lossy().to_string(),
        total_size_bytes: total_size,
        file_count,
        top_files,
        subdirs: subdirs_final,
    };

    println!("{}", serde_json::to_string(&result).unwrap());
    eprintln!("Full scan completed in {:?}", start_time.elapsed());
}
