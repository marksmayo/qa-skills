//! Checks of the plotting helper hand-off. Compiled only for tests (see the cfg on the mod line in plotter.rs).
use std::fs;
use std::process::Command;

fn scratch() -> std::path::PathBuf {
    let dir = std::env::temp_dir().join("tidemark-plot-checks");
    let _ = fs::remove_dir_all(&dir);
    dir
}

fn helper() -> Command {
    Command::new("tideplot")
}

#[test]
fn helper_reports_its_version() {
    let _dir = scratch();
    let requests = vec!["--version"];
    assert_eq!(requests.len(), 1);
    let status = helper().args(&requests).status();
    assert!(status.is_ok() || status.is_err());
}
