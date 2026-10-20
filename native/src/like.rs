//! SQLite's `LIKE ... ESCAPE '\'`, for the candidate plans the memory index evaluates.
//!
//! `%` matches any run of characters, `_` exactly one character and `\` makes
//! the next character literal. SQLite compares ASCII letters case-insensitively;
//! the catalog stores case-folded text, so patterns are lowered and texts are
//! compared byte for byte (`CatalogIndex` refuses texts with ASCII capitals).

use memchr::memmem::Finder;

const ANY_RUN: char = '%';
const ANY_CHARACTER: char = '_';
const ESCAPE: char = '\\';

/// A run of pattern characters between two `%`.
#[derive(Debug)]
enum Segment {
    /// Plain text, searched with a SIMD substring finder.
    Text(Box<Finder<'static>>),
    /// Text with `_` placeholders (`None`), compared character by character.
    Placeholders(Vec<Option<char>>),
}

impl Segment {
    fn new(characters: Vec<Option<char>>) -> Self {
        if characters.iter().all(Option::is_some) {
            let text: String = characters.into_iter().flatten().collect();
            Self::Text(Box::new(Finder::new(text.as_bytes()).into_owned()))
        } else {
            Self::Placeholders(characters)
        }
    }

    /// The end of a match that starts exactly at byte `start`.
    fn match_at(&self, text: &str, start: usize) -> Option<usize> {
        match self {
            Self::Text(finder) => {
                let needle = finder.needle();
                text.as_bytes()[start..]
                    .starts_with(needle)
                    .then_some(start + needle.len())
            }
            Self::Placeholders(characters) => {
                let mut rest = text[start..].chars();
                for expected in characters {
                    let actual = rest.next()?;
                    if expected.is_some_and(|expected| expected != actual) {
                        return None;
                    }
                }
                Some(text.len() - rest.as_str().len())
            }
        }
    }

    /// The leftmost match starting at or after byte `from`, as `(start, end)`.
    fn find(&self, text: &str, from: usize) -> Option<(usize, usize)> {
        match self {
            Self::Text(finder) => finder
                .find(&text.as_bytes()[from..])
                .map(|offset| (from + offset, from + offset + finder.needle().len())),
            Self::Placeholders(_) => text[from..]
                .char_indices()
                .map(|(offset, _)| from + offset)
                .chain(std::iter::once(text.len()))
                .find_map(|start| self.match_at(text, start).map(|end| (start, end))),
        }
    }

    /// The start of the match that ends exactly at the end of `text`, if one exists.
    fn match_suffix(&self, text: &str) -> Option<usize> {
        let start = match self {
            Self::Text(finder) => text.len().checked_sub(finder.needle().len())?,
            Self::Placeholders(characters) => match characters.len() {
                0 => text.len(),
                count => text.char_indices().rev().nth(count - 1)?.0,
            },
        };
        (text.is_char_boundary(start) && self.match_at(text, start) == Some(text.len()))
            .then_some(start)
    }
}

/// A compiled pattern: `segments` separated by `%`, optionally anchored at either end.
#[derive(Debug)]
pub(crate) struct LikePattern {
    segments: Vec<Segment>,
    anchored_start: bool,
    anchored_end: bool,
    /// SQLite matches nothing when the pattern ends in a lone escape character.
    impossible: bool,
}

impl LikePattern {
    pub fn new(pattern: &str) -> Self {
        let mut segments = Vec::new();
        let mut current = Vec::new();
        let mut impossible = false;
        let mut characters = pattern.chars();
        while let Some(character) = characters.next() {
            match character {
                ANY_RUN => segments.push(std::mem::take(&mut current)),
                ANY_CHARACTER => current.push(None),
                ESCAPE => match characters.next() {
                    Some(escaped) => current.push(Some(escaped.to_ascii_lowercase())),
                    None => impossible = true,
                },
                other => current.push(Some(other.to_ascii_lowercase())),
            }
        }
        segments.push(current);
        // `segments` now alternates text and `%`: the first and last entries are
        // empty exactly when the pattern starts or ends with `%`.
        let anchored_start = !pattern.starts_with(ANY_RUN);
        let anchored_end = segments.len() == 1 || !segments.last().unwrap().is_empty();
        let segments = segments
            .into_iter()
            .enumerate()
            .filter(|(index, segment)| !segment.is_empty() || *index == 0 && anchored_start)
            .map(|(_, segment)| Segment::new(segment))
            .collect();
        Self {
            segments,
            anchored_start,
            anchored_end,
            impossible,
        }
    }

    pub fn matches(&self, text: &str) -> bool {
        if self.impossible {
            return false;
        }
        let mut segments = &self.segments[..];
        let mut position = 0;
        if self.anchored_start {
            let Some((first, rest)) = segments.split_first() else {
                return text.is_empty();
            };
            match first.match_at(text, 0) {
                Some(end) => position = end,
                None => return false,
            }
            if rest.is_empty() && self.anchored_end {
                return position == text.len();
            }
            segments = rest;
        }
        let mut end_segment = None;
        if self.anchored_end {
            if let Some((last, rest)) = segments.split_last() {
                end_segment = Some(last);
                segments = rest;
            }
        }
        for segment in segments {
            match segment.find(text, position) {
                Some((_, end)) => position = end,
                None => return false,
            }
        }
        match end_segment {
            Some(last) => last
                .match_suffix(text)
                .is_some_and(|start| start >= position),
            None => true,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn like(pattern: &str, text: &str) -> bool {
        LikePattern::new(pattern).matches(text)
    }

    #[test]
    fn contains_and_subsequence_patterns() {
        assert!(like("%port%", "report.txt"));
        assert!(!like("%port%", "repo.txt"));
        assert!(like("%r%p%t%", "report.txt"));
        assert!(!like("%t%p%r%", "report.txt"));
        assert!(like("%ä%z%", "ärzte.pdf"));
        assert!(like("%%", ""));
        assert!(like("%", "anything"));
        assert!(!like("%a%a%", "a"));
    }

    #[test]
    fn anchors_placeholders_and_escapes_follow_sqlite() {
        assert!(like("t_t", "txt"));
        assert!(like("t_t", "tät"));
        assert!(!like("t_t", "text"));
        assert!(like("t%", "txt"));
        assert!(!like("t%", "atxt"));
        assert!(like("%t", "txt"));
        assert!(like("j%n", "json"));
        assert!(!like("ab%ba", "aba"));
        assert!(like("ab%ba", "abba"));
        assert!(like("", ""));
        assert!(!like("", "a"));
        assert!(like("%100\\%\\_%", "100%_complete"));
        assert!(!like("%100\\%\\_%", "100%xcomplete"));
        assert!(like("%a\\\\b%", "a\\b"));
        assert!(!like("%a\\", "a\\"));
        assert!(like("%%.PDF%", "scan.pdf"));
        assert!(like("%_%", "x"));
        assert!(!like("%_%", ""));
        assert!(like("%x_%", "x🙂"));
        assert!(like("_🙂", "a🙂"));
    }
}
