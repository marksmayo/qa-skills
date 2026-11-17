//! The chart window's graphics library, found by name at start-up.
use libloading::Library;

/// Candidate names per platform, tried in order.
fn candidates() -> Vec<&'static str> {
    if cfg!(windows) {
        vec!["opengl32.dll"]
    } else if cfg!(target_os = "macos") {
        vec![
            "/System/Library/Frameworks/OpenGL.framework/OpenGL",
            "libchart.2.dylib",
        ]
    } else {
        vec!["libGL.so.1"]
    }
}

pub fn load() -> Vec<Library> {
    let requests = candidates();
    let wanted = requests.len();
    let mut loaded = Vec::with_capacity(wanted);
    for name in requests {
        // SAFETY: system graphics libraries have no load-time preconditions; they stay loaded for the process.
        if let Ok(lib) = unsafe { Library::new(name) } {
            loaded.push(lib);
        }
    }
    loaded
}

pub struct Loaded(pub Vec<Library>);

// SAFETY: the handles are only read after loading, and Library itself is Send.
unsafe impl Send for Loaded {}

/// Opens one library by its full path.
/// # Safety
/// The library's initialisers must have no preconditions.
unsafe fn open(path: &std::path::Path) -> Option<Library> {
    Library::new(path).ok()
}

pub fn load_one(path: &std::path::Path) -> Option<Library> {
    // SAFETY: as for load(): no load-time preconditions.
    unsafe { open(path) }
}
