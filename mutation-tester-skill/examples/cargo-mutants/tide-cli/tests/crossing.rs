use tide_cli::{can_cross, gap_minutes};

#[test]
fn pilot_always_crosses() {
    assert!(can_cross(400, 100, true));
}

#[test]
fn gap_same_day() {
    assert_eq!(gap_minutes(60, 180), 120);
}
