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
- `Cargo.lock` pins crates with RustSec advisories (a vulnerability and an unmaintained crate) for the dependency step.
- `.github/workflows/ci.yml`: a checkout in a read-only job is not a persisted-credentials candidate; one in a job
  with `contents: write` is.
- `scripts/fetch_tables.py` validates archive members on the line before extracting (no candidate); the
  unvalidated extraction below it is one.
