//! Python-compatible Unicode 15 search normalization and character properties.

use unicode_normalization::UnicodeNormalization;

mod data {
    include!("unicode_data.rs");
}

pub const UNICODE_VERSION: &str = "15.0.0";

/// The catalog normalizes to NFC before full case folding; do not reverse these.
pub fn normalize(value: &str) -> String {
    if value.is_ascii() {
        return value.to_ascii_lowercase();
    }
    let mut result = String::with_capacity(value.len());
    for character in value.nfc() {
        push_casefold(&mut result, character);
    }
    result
}

/// Sort ties use Python's casefold directly, without NFC normalization.
pub fn casefold(value: &str) -> String {
    if value.is_ascii() {
        return value.to_ascii_lowercase();
    }
    let mut result = String::with_capacity(value.len());
    for character in value.chars() {
        push_casefold(&mut result, character);
    }
    result
}

fn push_casefold(result: &mut String, character: char) {
    match data::CASEFOLD.binary_search_by_key(&(character as u32), |entry| entry.0) {
        Ok(index) => result.push_str(data::CASEFOLD[index].1),
        Err(_) => result.push(character),
    }
}

pub fn isalnum(character: char) -> bool {
    if character.is_ascii() {
        return character.is_ascii_alphanumeric();
    }
    in_ranges(character, data::ALNUM)
}

pub fn isupper(character: char) -> bool {
    if character.is_ascii() {
        return character.is_ascii_uppercase();
    }
    in_ranges(character, data::UPPER)
}

pub fn islower(character: char) -> bool {
    if character.is_ascii() {
        return character.is_ascii_lowercase();
    }
    in_ranges(character, data::LOWER)
}

fn in_ranges(character: char, ranges: &[(u32, u32)]) -> bool {
    let code = character as u32;
    let index = ranges.partition_point(|range| range.1 < code);
    ranges.get(index).is_some_and(|range| range.0 <= code)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn nfc_and_full_casefold_match_the_reference() {
        assert_eq!(unicode_normalization::UNICODE_VERSION, (15, 0, 0));
        assert_eq!(normalize("Straße ẞ"), "strasse ss");
        assert_eq!(normalize("Cafe\u{301}"), "café");
        assert_eq!(normalize("ǅΣςİﬃ"), "ǆσσi\u{307}ffi");
        assert_eq!(normalize("\u{1100}\u{1161}"), "가");
        assert_eq!(normalize("\u{1e9b}\u{323}"), "\u{1e61}\u{323}");
        assert_eq!(casefold("Cafe\u{301}"), "cafe\u{301}");
    }

    #[test]
    fn character_classes_follow_python_including_numeric_and_other_case() {
        assert!(isalnum('²'));
        assert!(isalnum('½'));
        assert!(isalnum('Ⅻ'));
        assert!(!isalnum('\u{345}'));
        assert!(!isalnum('_'));
        assert!(isupper('Ⅻ'));
        assert!(islower('ª'));
        assert!(islower('\u{345}'));
        assert!(!isupper('ǅ'));
        assert!(!islower('ǅ'));
        // These letters were introduced after Unicode 15 and remain unassigned here.
        assert!(!isalnum('\u{1c89}'));
        assert!(!isupper('\u{1c89}'));
        assert!(!islower('\u{1c8a}'));
    }

    #[test]
    fn every_scalar_matches_python_generated_class_and_casefold_checksums() {
        const OFFSET: u64 = 14695981039346656037;
        const PRIME: u64 = 1099511628211;
        let mut classes_hash = OFFSET;
        let mut fold_hash = OFFSET;
        for character in (0..=0x10ffff).filter_map(char::from_u32) {
            let flags = u8::from(isalnum(character))
                | (u8::from(isupper(character)) << 1)
                | (u8::from(islower(character)) << 2);
            classes_hash = (classes_hash ^ u64::from(flags)).wrapping_mul(PRIME);
            for byte in casefold(&character.to_string()).bytes().chain([0]) {
                fold_hash = (fold_hash ^ u64::from(byte)).wrapping_mul(PRIME);
            }
        }
        assert_eq!(classes_hash, data::CLASSES_HASH);
        assert_eq!(fold_hash, data::CASEFOLD_HASH);
    }
}
