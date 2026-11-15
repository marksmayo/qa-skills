//! Harmonic constituents, summed by a native library when one is installed.
use libloading::{Library, Symbol};
use std::env;
use std::path::Path;

/// The native library, relative to the working directory.
const NATIVE_LIB: &str = "lib/libharmonics.so";

type SumFn = unsafe extern "C" fn(*const f64, usize, f64) -> f64;

pub struct Table {
    pub amplitudes: Vec<f64>,
    lib: Option<Library>,
}

impl Table {
    pub fn load(station: &str, data_dir: Option<&Path>) -> Table {
        let path = env::var("TIDEMARK_HARMONICS_LIB").unwrap_or_else(|_| NATIVE_LIB.to_string());
        // SAFETY: the library's initialisers have no preconditions; it stays loaded while the table lives.
        let lib = unsafe { Library::new(&path) }.ok();
        let amplitudes = crate::model::read_table(station, data_dir);
        Table { amplitudes, lib }
    }

    pub fn height(&self, t: f64) -> f64 {
        let Some(lib) = &self.lib else { return 0.0 };
        // SAFETY: tide_sum has the SumFn signature in the library's header.
        let sum: Symbol<SumFn> = match unsafe { lib.get(b"tide_sum\0") } {
            Ok(f) => f,
            Err(_) => return 0.0,
        };
        // SAFETY: the pointer and length come from self.amplitudes, which outlives the call.
        unsafe { sum(self.amplitudes.as_ptr(), self.amplitudes.len(), t) }
    }
}

/// Windows builds: the vendor's DLL, by name.
#[cfg(windows)]
pub fn load_vendor() -> Option<Library> {
    // SAFETY: loading has no other preconditions.
    unsafe { Library::new("harmonics.dll") }.ok()
}

/// Reuses a buffer for a station record of n bytes.
pub fn reuse(buf: &mut Vec<u8>, n: usize) {
    buf.reserve(n);
    unsafe { buf.set_len(n) }
}

const STATION_LIB_ENV: &str = "TIDEMARK_STATION_LIB";

/// A station's own correction library, when one is configured.
pub fn station_library() -> Option<Library> {
    let path = env::var_os(STATION_LIB_ENV)?;
    // SAFETY: loading has no other preconditions.
    unsafe { Library::new(path) }.ok()
}
