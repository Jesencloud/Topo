//! Scanning engine behind the `topo-core` binary.
//!
//! The binary is a thin argument-parsing shell; everything it does lives in
//! [`scanner`] and is re-exported here so integration tests can drive the same
//! entry points the binary uses.

mod scanner;

pub use scanner::{
    DEFAULT_TREE_MIN_BYTES, DirAgg, FileInfo, PathStats, ScanResult, TOP_FILE_MIN_BYTES,
    compute_single, compute_stats, compute_tree, run_single, run_stats, run_tree,
};
