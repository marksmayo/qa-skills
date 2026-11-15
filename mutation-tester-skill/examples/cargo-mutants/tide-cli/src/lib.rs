//! Tide table helpers for a harbour notice board (a synthetic sample crate).

/// Height band for a tide reading in centimetres: "high" from 300 cm.
pub fn band(height_cm: u32) -> &'static str {
    if height_cm >= 300 {
        "high"
    } else {
        "low"
    }
}

/// Minutes between two times given as minutes after midnight, wrapping past midnight.
pub fn gap_minutes(from: u32, to: u32) -> u32 {
    if to >= from {
        to - from
    } else {
        24 * 60 - from + to
    }
}

/// Whether a boat with this draught can cross the bar at this tide height.
pub fn can_cross(draught_cm: u32, height_cm: u32, pilot: bool) -> bool {
    pilot || height_cm > draught_cm + 50
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn bands() {
        assert_eq!(band(450), "high");
        assert_eq!(band(120), "low");
    }
}
