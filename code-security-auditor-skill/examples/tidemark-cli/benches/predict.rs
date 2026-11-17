//! Bench harness for the prediction loop. Not shipped.
use std::fs;
use std::hint::black_box;

fn main() {
    let dir = std::env::temp_dir().join("tidemark-bench");
    let _ = fs::remove_dir_all(&dir);
    let mut total = 0.0f64;
    for hour in 0..10_000u32 {
        total += black_box(f64::from(hour)).sin();
    }
    println!("{total}");
}
