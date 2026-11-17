# tidemark (synthetic fixture)

A small tide-table command-line tool, written for `scripts/self_test.py` of the code security auditor. It is never
built or run. It plants the Rust patterns the pattern scan and recon must report or set aside:

- `src/plotter.rs` finds its plotting helper under the working directory (`env::current_dir()` joined with
  `tools/tideplot.exe`) or from `TIDEMARK_PLOTTER`: an executable search path an attacker who controls the
  working directory can plant (CWE-427). Its `#[cfg(test)]` module spawns a process, which is not a candidate.
- `src/harmonics.rs` loads a native library from a path relative to the working directory (`lib/libharmonics.so`,
  or `TIDEMARK_HARMONICS_LIB`) and a Windows DLL by bare name (DLL search order). Its `unsafe` FFI calls carry
  `// SAFETY:` comments and are grouped into one low-confidence candidate; an `unsafe { set_len }` without one is not.
- `src/model.rs` narrows integers: casts of values clamped or bounded just before, float-to-integer casts (which
  saturate) and casts in test code are not candidates; an unchecked `u32 -> u16` of a header length is.
- `src/bundled.rs` looks for its table compiler in the program folder, its parent and grandparent
  (`current_exe()` then `.ancestors()`), and for plug-ins one level up: both are search paths (CWE-427).
  Its `own_dir()` (the program folder only) is not.
- `src/gl.rs` names `opengl32.dll`, `libchart.2.dylib` and `libGL.so.1` without a directory in a list that
  reaches `Library::new(name)`; the absolute framework path is not a candidate. Its documented `unsafe impl
  Send`, `unsafe fn` wrapper and the call to it join the file's grouped FFI candidate. A local variable named
  `requests` is not an outbound HTTP call.
- `src/plotter/plot_checks.rs` is compiled only for tests (`#[cfg(all(test, feature = "plot-checks"))] mod
  plot_checks;` in `src/plotter.rs`), and `benches/predict.rs` is a bench harness: neither raises a candidate.
- `Cargo.lock` pins crates with RustSec advisories (a vulnerability and an unmaintained crate) for the dependency step.
- `.github/workflows/ci.yml`: a checkout in a read-only job is not a persisted-credentials candidate; one in a job
  with `contents: write` is, and that job's artifact download is a recon review lead.
- `scripts/fetch_tables.py` validates archive members on the line before extracting (no candidate); the
  unvalidated extraction below it is one.
