//! The Python O(term length × name length) fuzzy alignment dynamic program.

use std::collections::HashSet;

use crate::unicode::{isalnum, islower, isupper, normalize};

pub const FUZZY_FIRST_LETTER: f64 = 22.0;
pub const FUZZY_FIRST_LETTER_OFFSET_PENALTY: f64 = 0.5;
pub const FUZZY_CONSECUTIVE_LETTER: f64 = 10.0;
pub const FUZZY_LETTER_AFTER_GAP: f64 = 4.0;
pub const FUZZY_GAP_PENALTY: f64 = 0.7;
pub const FUZZY_WORD_START_BONUS: f64 = 8.0;
pub const FUZZY_SCORE_OFFSET: f64 = 25.0;
pub const FUZZY_MIN_SCORE: f64 = 5.0;
pub const FUZZY_MAX_SCORE: f64 = 60.0;

pub fn fuzzy_score(term: &str, original: &str) -> Option<f64> {
    let name: Vec<char> = normalize(original).chars().collect();
    let term: Vec<char> = term.chars().collect();
    if term.is_empty() || term.len() > name.len() {
        return None;
    }
    let letters: HashSet<char> = name.iter().copied().collect();
    if !term.iter().all(|letter| letters.contains(letter)) {
        return None;
    }
    let starts = word_starts(original);
    let mut previous = vec![f64::NEG_INFINITY; name.len()];
    let mut current = vec![f64::NEG_INFINITY; name.len()];
    for (letter_index, letter) in term.iter().enumerate() {
        current.fill(f64::NEG_INFINITY);
        let mut best_before = f64::NEG_INFINITY;
        for (position, candidate) in name.iter().enumerate() {
            if position > 0 {
                let gap_adjusted =
                    previous[position - 1] + (position - 1) as f64 * FUZZY_GAP_PENALTY;
                best_before = best_before.max(gap_adjusted);
            }
            if candidate != letter {
                continue;
            }
            let bonus = if starts.contains(&position) {
                FUZZY_WORD_START_BONUS
            } else {
                0.0
            };
            if letter_index == 0 {
                let first =
                    FUZZY_FIRST_LETTER - position as f64 * FUZZY_FIRST_LETTER_OFFSET_PENALTY;
                current[position] = first + bonus;
                continue;
            }
            // Python permits position == 0 here: its previous[-1] equivalent
            // for the gap is still impossible. Keep the signed -1 arithmetic.
            let after_gap =
                best_before - (position as f64 - 1.0) * FUZZY_GAP_PENALTY + FUZZY_LETTER_AFTER_GAP;
            let consecutive = if position > 0 {
                previous[position - 1] + FUZZY_CONSECUTIVE_LETTER
            } else {
                f64::NEG_INFINITY
            };
            current[position] = after_gap.max(consecutive) + bonus;
        }
        std::mem::swap(&mut current, &mut previous);
    }
    let best = previous.into_iter().fold(f64::NEG_INFINITY, f64::max);
    if !best.is_finite() {
        return None;
    }
    let average = best / term.len() as f64 + FUZZY_SCORE_OFFSET;
    Some(average.clamp(FUZZY_MIN_SCORE, FUZZY_MAX_SCORE))
}

/// Boundaries use normalized original prefixes, including casefold expansion.
pub fn word_starts(original: &str) -> HashSet<usize> {
    let mut starts = HashSet::from([0]);
    let mut previous = None;
    for (byte_position, character) in original.char_indices() {
        if let Some(before) = previous {
            if !isalnum(before) || (isupper(character) && islower(before)) {
                starts.insert(normalize(&original[..byte_position]).chars().count());
            }
        }
        previous = Some(character);
    }
    starts
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn alignment_rewards_consecutive_letters_and_word_starts() {
        assert_eq!(fuzzy_score("abc", "abc.txt"), Some(41.66666666666667));
        assert_eq!(
            fuzzy_score("abc", "a___b___c.txt"),
            Some(41.599999999999994)
        );
        assert_eq!(
            fuzzy_score("bdg", "Budget Report.txt"),
            Some(39.43333333333333)
        );
        assert_eq!(fuzzy_score("aa", "a"), None);
        assert_eq!(fuzzy_score("ba", "ab"), None);
        assert_eq!(fuzzy_score("zz", "report.txt"), None);
        assert_eq!(fuzzy_score("", "name"), None);
        assert_eq!(fuzzy_score("a", ""), None);
    }

    #[test]
    fn fuzzy_boundaries_use_python_character_properties_and_normalized_offsets() {
        assert_eq!(word_starts("StraßeCode"), HashSet::from([0, 7]));
        assert_eq!(word_starts("Cafe\u{301}X"), HashSet::from([0, 4]));
        assert_eq!(word_starts("x²A"), HashSet::from([0]));
        assert_eq!(word_starts("xªA"), HashSet::from([0, 2]));
        assert_eq!(word_starts("ǅA"), HashSet::from([0]));
        assert_eq!(word_starts("x_🙂A"), HashSet::from([0, 2, 3]));
        assert_eq!(fuzzy_score("ssc", "StraßeCode"), Some(39.166666666666664));
    }

    #[test]
    fn distant_alignment_is_clamped_to_minimum() {
        assert_eq!(
            fuzzy_score("ab", &format!("{}ab", "x".repeat(1000))),
            Some(5.0)
        );
    }

    #[test]
    fn unicode_alignment_agrees_with_python_reference_fixtures() {
        let cases = [
            ("ss", "ß", Some(45.0)),
            ("ffi", "ﬃ", Some(41.66666666666667)),
            ("ex", "e\u{301}_X", Some(45.65)), // The parser removes typed accents.
            ("σς", "Σς", None),                // Query terms are already folded by the parser.
            ("i", "İ", Some(55.0)),
            ("12", "x¹_²", None),
            ("ab", "ǅª_B", None),
            ("cfx", "Cafe\u{301}X", Some(39.86666666666667)),
            ("sac", "Straße_ArchiveCode", Some(39.96666666666667)),
            ("αβ", "Α_Β.txt", Some(45.65)),
            ("ǆb", "ǅ_A_B", Some(44.95)),
            ("aa", "aaa", Some(45.0)),
            ("ba", "aababa", Some(40.5)),
            ("abc", "abcABCabc", Some(41.66666666666667)),
        ];
        for (term, original, expected) in cases {
            assert_eq!(
                fuzzy_score(term, original),
                expected,
                "{term:?} in {original:?}"
            );
        }
    }
}
