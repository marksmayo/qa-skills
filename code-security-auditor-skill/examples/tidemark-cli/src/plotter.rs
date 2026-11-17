//! Hands the predictions to an external plotting helper.
use std::env;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

/// The plotting helper: TIDEMARK_PLOTTER, else tools/ under the working directory, else the PATH.
fn plotter_path() -> PathBuf {
    if let Ok(p) = env::var("TIDEMARK_PLOTTER") {
        return PathBuf::from(p);
    }
    let local = env::current_dir()
        .unwrap_or_default()
        .join("tools")
        .join("tideplot.exe");
    if local.exists() {
        return local;
    }
    PathBuf::from("tideplot")
}

pub fn plot(heights: &[f64], out: &Path) -> std::io::Result<()> {
    let mut child = Command::new(plotter_path())
        .arg("--out")
        .arg(out)
        .stdin(Stdio::piped())
        .spawn()?;
    if let Some(stdin) = child.stdin.as_mut() {
        for h in heights {
            writeln!(stdin, "{h}")?;
        }
    }
    child.wait()?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use std::process::Command;

    #[test]
    fn toolchain_is_present() {
        let status = Command::new("cargo").arg("--version").status().unwrap();
        assert!(status.success());
    }
}

/// The legend renderer: TIDEMARK_LEGEND, else the copy shipped next to the tables.
pub fn legend_renderer() -> PathBuf {
    std::env::var_os("TIDEMARK_LEGEND")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("tools/legend.exe"))
}

#[cfg(all(test, feature = "plot-checks"))]
mod plot_checks;
