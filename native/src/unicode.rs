//! Python-compatible search normalization (NFC + full case folding) and character
//! classes, for each Unicode version a supported Python release uses.
//!
//! One interpreter has exactly one Unicode version, so the tables are selected
//! once per process by `select` before any search runs.

use std::sync::OnceLock;

#[path = "unicode_tables/v14_0.rs"]
mod v14_0;
#[path = "unicode_tables/v15_0.rs"]
mod v15_0;
#[path = "unicode_tables/v15_1.rs"]
mod v15_1;
#[path = "unicode_tables/v16_0.rs"]
mod v16_0;

/// Python 3.11, 3.12, 3.13 and 3.14.
static ALL_TABLES: [&Tables; 4] = [
    &v14_0::TABLES,
    &v15_0::TABLES,
    &v15_1::TABLES,
    &v16_0::TABLES,
];
static ACTIVE: OnceLock<&'static Tables> = OnceLock::new();

const HANGUL_SYLLABLE_BASE: u32 = 0xAC00;
const HANGUL_LEADING_BASE: u32 = 0x1100;
const HANGUL_VOWEL_BASE: u32 = 0x1161;
const HANGUL_TRAILING_BASE: u32 = 0x11A7;
const HANGUL_VOWEL_COUNT: u32 = 21;
const HANGUL_TRAILING_COUNT: u32 = 28;
const HANGUL_LEADING_COUNT: u32 = 19;
const HANGUL_BLOCK: u32 = HANGUL_VOWEL_COUNT * HANGUL_TRAILING_COUNT;
const HANGUL_SYLLABLE_COUNT: u32 = HANGUL_LEADING_COUNT * HANGUL_BLOCK;

/// Generated per Unicode version by `native/tools/generate_unicode.py`.
pub struct Tables {
    pub version: &'static str,
    pub casefold: &'static [(u32, &'static str)],
    pub alnum: &'static [(u32, u32)],
    pub upper: &'static [(u32, u32)],
    pub lower: &'static [(u32, u32)],
    pub combining_classes: &'static [(u32, u32, u8)],
    pub decompositions: &'static [(u32, &'static str)],
    pub compositions: &'static [(u32, u32, u32)],
    #[allow(dead_code)] // Only the tests rebuild Python's NFC samples from it.
    pub nfc_sample_pool: &'static [u32],
}

#[cfg(test)]
pub struct TestHashes {
    pub classes: u64,
    pub casefold: u64,
    pub nfc: u64,
}

pub fn supported_versions() -> Vec<&'static str> {
    ALL_TABLES.iter().map(|tables| tables.version).collect()
}

/// Use the tables of the interpreter's Unicode version for the rest of the process.
pub fn select(version: &str) -> Result<(), String> {
    let tables = ALL_TABLES
        .iter()
        .copied()
        .find(|tables| tables.version == version)
        .ok_or_else(|| format!("Unicode {version} is not supported"))?;
    let active = ACTIVE.get_or_init(|| tables);
    if active.version == version {
        Ok(())
    } else {
        Err(format!("Unicode {} is already in use", active.version))
    }
}

pub fn is_selected() -> bool {
    ACTIVE.get().is_some()
}

fn active() -> &'static Tables {
    #[cfg(test)]
    return ACTIVE.get_or_init(|| &v15_0::TABLES);
    #[cfg(not(test))]
    ACTIVE
        .get()
        .expect("Unicode tables are selected before any search")
}

/// Search normalization: NFC first, then full case folding (as `normalize` in Python).
pub fn normalize(value: &str) -> String {
    active().normalize(value)
}

/// Sort ties use Python's `casefold` directly, without NFC normalization.
pub fn casefold(value: &str) -> String {
    active().casefold(value)
}

pub fn isalnum(character: char) -> bool {
    active().isalnum(character)
}

pub fn isupper(character: char) -> bool {
    active().isupper(character)
}

pub fn islower(character: char) -> bool {
    active().islower(character)
}

impl Tables {
    pub fn normalize(&self, value: &str) -> String {
        if value.is_ascii() {
            return value.to_ascii_lowercase();
        }
        self.casefold(&self.nfc(value))
    }

    pub fn casefold(&self, value: &str) -> String {
        if value.is_ascii() {
            return value.to_ascii_lowercase();
        }
        let mut result = String::with_capacity(value.len());
        for character in value.chars() {
            match lookup(self.casefold, character) {
                Some(folded) => result.push_str(folded),
                None => result.push(character),
            }
        }
        result
    }

    pub fn isalnum(&self, character: char) -> bool {
        if character.is_ascii() {
            return character.is_ascii_alphanumeric();
        }
        in_ranges(character, self.alnum)
    }

    pub fn isupper(&self, character: char) -> bool {
        if character.is_ascii() {
            return character.is_ascii_uppercase();
        }
        in_ranges(character, self.upper)
    }

    pub fn islower(&self, character: char) -> bool {
        if character.is_ascii() {
            return character.is_ascii_lowercase();
        }
        in_ranges(character, self.lower)
    }

    /// Unicode Normalization Form C (UAX #15): decompose, order marks, compose.
    pub fn nfc(&self, value: &str) -> String {
        let mut characters = Vec::with_capacity(value.len());
        for character in value.chars() {
            self.decompose(character, &mut characters);
        }
        self.order_marks(&mut characters);
        self.compose(&characters).into_iter().collect()
    }

    fn decompose(&self, character: char, output: &mut Vec<char>) {
        let code = character as u32;
        if (HANGUL_SYLLABLE_BASE..HANGUL_SYLLABLE_BASE + HANGUL_SYLLABLE_COUNT).contains(&code) {
            decompose_hangul(code - HANGUL_SYLLABLE_BASE, output);
            return;
        }
        match lookup(self.decompositions, character) {
            Some(decomposed) => output.extend(decomposed.chars()),
            None => output.push(character),
        }
    }

    fn combining_class(&self, character: char) -> u8 {
        let code = character as u32;
        let index = self
            .combining_classes
            .partition_point(|range| range.1 < code);
        match self.combining_classes.get(index) {
            Some(&(first, _, class)) if first <= code => class,
            _ => 0,
        }
    }

    /// Stable sort of each run of combining marks by combining class.
    fn order_marks(&self, characters: &mut [char]) {
        let mut start = 0;
        while start < characters.len() {
            if self.combining_class(characters[start]) == 0 {
                start += 1;
                continue;
            }
            let end = start
                + characters[start..]
                    .iter()
                    .take_while(|&&character| self.combining_class(character) != 0)
                    .count();
            characters[start..end].sort_by_key(|&character| self.combining_class(character));
            start = end;
        }
    }

    fn compose(&self, characters: &[char]) -> Vec<char> {
        let mut output: Vec<char> = Vec::with_capacity(characters.len());
        let mut starter: Option<usize> = None;
        let mut last_class = 0u8;
        for &character in characters {
            let class = self.combining_class(character);
            if let Some(index) = starter {
                let adjacent = index + 1 == output.len();
                // A mark is blocked when an earlier mark has the same or a higher class.
                let unblocked = adjacent || (last_class != 0 && last_class < class);
                if unblocked {
                    if let Some(composite) = self.composite(output[index], character) {
                        output[index] = composite;
                        continue;
                    }
                }
            }
            if class == 0 {
                starter = Some(output.len());
            }
            last_class = class;
            output.push(character);
        }
        output
    }

    fn composite(&self, first: char, second: char) -> Option<char> {
        if let Some(syllable) = compose_hangul(first as u32, second as u32) {
            return char::from_u32(syllable);
        }
        let key = (first as u32, second as u32);
        let index = self
            .compositions
            .binary_search_by_key(&key, |&(a, b, _)| (a, b))
            .ok()?;
        char::from_u32(self.compositions[index].2)
    }
}

fn decompose_hangul(offset: u32, output: &mut Vec<char>) {
    let leading = HANGUL_LEADING_BASE + offset / HANGUL_BLOCK;
    let vowel = HANGUL_VOWEL_BASE + (offset % HANGUL_BLOCK) / HANGUL_TRAILING_COUNT;
    let trailing = offset % HANGUL_TRAILING_COUNT;
    output.extend([leading, vowel].into_iter().filter_map(char::from_u32));
    if trailing != 0 {
        output.extend(char::from_u32(HANGUL_TRAILING_BASE + trailing));
    }
}

fn compose_hangul(first: u32, second: u32) -> Option<u32> {
    let leading = first.checked_sub(HANGUL_LEADING_BASE)?;
    if leading < HANGUL_LEADING_COUNT {
        let vowel = second.checked_sub(HANGUL_VOWEL_BASE)?;
        return (vowel < HANGUL_VOWEL_COUNT).then(|| {
            HANGUL_SYLLABLE_BASE + (leading * HANGUL_VOWEL_COUNT + vowel) * HANGUL_TRAILING_COUNT
        });
    }
    let syllable = first.checked_sub(HANGUL_SYLLABLE_BASE)?;
    let trailing = second.checked_sub(HANGUL_TRAILING_BASE)?;
    let is_lv = syllable < HANGUL_SYLLABLE_COUNT && syllable % HANGUL_TRAILING_COUNT == 0;
    (is_lv && (1..HANGUL_TRAILING_COUNT).contains(&trailing)).then_some(first + trailing)
}

fn lookup(table: &'static [(u32, &'static str)], character: char) -> Option<&'static str> {
    let index = table
        .binary_search_by_key(&(character as u32), |entry| entry.0)
        .ok()?;
    Some(table[index].1)
}

fn in_ranges(character: char, ranges: &[(u32, u32)]) -> bool {
    let code = character as u32;
    let index = ranges.partition_point(|range| range.1 < code);
    ranges.get(index).is_some_and(|range| range.0 <= code)
}

#[cfg(test)]
mod tests {
    use super::*;

    const OFFSET: u64 = 14695981039346656037;
    const PRIME: u64 = 1099511628211;
    const LCG_MULTIPLIER: u64 = 6364136223846793005;
    const LCG_INCREMENT: u64 = 1442695040888963407;
    const NFC_SAMPLE_SEED: u64 = 20261019;
    const NFC_SAMPLE_COUNT: usize = 60_000;
    const NFC_SAMPLE_MAX_LENGTH: u64 = 6;

    fn hash(value: u64, bytes: impl IntoIterator<Item = u8>) -> u64 {
        bytes.into_iter().fold(value, |hash, byte| {
            (hash ^ u64::from(byte)).wrapping_mul(PRIME)
        })
    }

    fn hash_text(value: u64, text: &str) -> u64 {
        hash(value, text.bytes().chain([0]))
    }

    /// The same deterministic strings `generate_unicode.py` normalized with Python.
    fn nfc_samples(pool: &[u32]) -> Vec<String> {
        let mut state = NFC_SAMPLE_SEED;
        let mut next = || {
            state = state
                .wrapping_mul(LCG_MULTIPLIER)
                .wrapping_add(LCG_INCREMENT);
            state >> 33
        };
        (0..NFC_SAMPLE_COUNT)
            .map(|_| {
                let length = 1 + next() % NFC_SAMPLE_MAX_LENGTH;
                (0..length)
                    .map(|_| char::from_u32(pool[(next() % pool.len() as u64) as usize]).unwrap())
                    .collect()
            })
            .collect()
    }

    fn check(tables: &Tables, expected: &TestHashes) {
        let (mut classes, mut casefold, mut nfc) = (OFFSET, OFFSET, OFFSET);
        for character in (0..=0x10ffff).filter_map(char::from_u32) {
            let flags = u8::from(tables.isalnum(character))
                | (u8::from(tables.isupper(character)) << 1)
                | (u8::from(tables.islower(character)) << 2);
            classes = hash(classes, [flags]);
            let text = character.to_string();
            casefold = hash_text(casefold, &tables.casefold(&text));
            nfc = hash_text(nfc, &tables.nfc(&text));
        }
        for sample in nfc_samples(tables.nfc_sample_pool) {
            nfc = hash_text(nfc, &tables.nfc(&sample));
        }
        assert_eq!(classes, expected.classes, "classes {}", tables.version);
        assert_eq!(casefold, expected.casefold, "casefold {}", tables.version);
        assert_eq!(nfc, expected.nfc, "NFC {}", tables.version);
    }

    #[test]
    fn unicode_14_matches_python_3_11() {
        check(&v14_0::TABLES, &v14_0::HASHES);
    }

    #[test]
    fn unicode_15_matches_python_3_12() {
        check(&v15_0::TABLES, &v15_0::HASHES);
    }

    #[test]
    fn unicode_15_1_matches_python_3_13() {
        check(&v15_1::TABLES, &v15_1::HASHES);
    }

    #[test]
    fn unicode_16_matches_python_3_14() {
        check(&v16_0::TABLES, &v16_0::HASHES);
    }

    #[test]
    fn normalization_examples() {
        let tables = &v15_0::TABLES;
        assert_eq!(tables.normalize("Straße ẞ"), "strasse ss");
        assert_eq!(tables.normalize("Cafe\u{301}"), "café");
        assert_eq!(tables.normalize("ǅΣςİﬃ"), "ǆσσi\u{307}ffi");
        assert_eq!(tables.normalize("\u{1100}\u{1161}"), "가");
        assert_eq!(tables.normalize("\u{1e9b}\u{323}"), "\u{1e61}\u{323}");
        assert_eq!(tables.casefold("Cafe\u{301}"), "cafe\u{301}");
    }

    #[test]
    fn selection_accepts_only_supported_versions() {
        assert!(supported_versions().contains(&"16.0.0"));
        assert!(select("1.0.0").is_err());
    }
}
