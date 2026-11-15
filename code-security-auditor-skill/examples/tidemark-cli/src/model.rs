//! Turns constituent tables into predicted heights.
use crate::harmonics::Table;
use std::path::Path;

const PALETTE_LEN: usize = 16;

/// Heights for the next `hours` hours, one per hour.
pub fn predict(table: &Table, hours: u32) -> Vec<f64> {
    (0..hours).map(|h| table.height(f64::from(h))).collect()
}

/// Palette entry for a height on the chart.
pub fn shade_index(height: f64, raw: usize) -> (u8, u8) {
    let byte = (height * 25.5).clamp(0.0, 255.0) as u8; // byte index into the palette
    let shade = raw.min(PALETTE_LEN - 1);
    let idx = shade as u8; // index into the palette
    (byte, idx)
}

/// Hourly slot of a fractional day.
pub fn slot(frac: f64) -> usize {
    let slot = (frac * 24.0).floor() as usize; // index into the hourly table
    slot
}

/// Record count from a station file header.
pub fn record_count(header: &[u8]) -> u16 {
    let len = u32::from_le_bytes([header[0], header[1], header[2], header[3]]);
    len as u16
}

pub fn read_table(_station: &str, _dir: Option<&Path>) -> Vec<f64> {
    Vec::new()
}

#[cfg(test)]
mod tests {
    #[test]
    fn wide_sizes_wrap() {
        let size = 300usize;
        assert_eq!(size as u8, 44);
    }
}

/// Column of a level on the chart grid (levels are f64; a float-to-integer cast saturates).
pub fn column(level: f64, width: usize) -> usize {
    let col = if level >= 0.0 { level as usize } else { 0 }; // index into the grid
    col.min(width)
}
