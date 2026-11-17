//! Finds the bundled table compiler next to the installed program, or in a checkout while developing.
use std::path::{Path, PathBuf};

/// tools/tablec.exe in the program folder, its parent or its grandparent.
pub fn table_compiler() -> Option<PathBuf> {
    let exe = std::env::current_exe().ok()?;
    exe.ancestors()
        .skip(1)
        .take(3)
        .map(|root| root.join("tools/tablec.exe"))
        .find(|p| p.is_file())
}

/// The plug-in folder: beside the program, else one level up (a development checkout).
pub fn plugin_dir() -> Option<PathBuf> {
    let exe = std::env::current_exe().ok()?;
    let above = exe.parent().and_then(Path::parent)?;
    Some(above.join("plugins"))
}

/// The program's own folder only: not a search.
pub fn own_dir() -> Option<PathBuf> {
    let exe = std::env::current_exe().ok()?;
    exe.parent().map(Path::to_path_buf)
}
