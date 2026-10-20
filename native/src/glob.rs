//! Shell-style wildcards (`*`, `?`, `[seq]`, `[!seq]`) translated to `regex` syntax.
//!
//! This mirrors CPython's `fnmatch.translate` so that native matching agrees with
//! `fnmatch.fnmatchcase`, including its quirks:
//!
//! - `]` directly after `[` or `[!` is a member, not the end of the set.
//! - A `[` without a closing `]` is a literal bracket.
//! - `-` at either edge of a set is literal; a reversed range such as `z-a` is
//!   dropped, so a set left without members matches nothing (or, when negated,
//!   any character).
//!
//! Every set member is escaped, so characters that are special inside a Rust
//! class (`[`, `]`, `\`, `^`, `-` and the set operators `&&`, `--`, `~~`) stay
//! literal, exactly as Python's `re` reads them.

use regex::Regex;

/// Matches any single character; `(?s)` makes it include newlines.
const ANY_CHARACTER: &str = ".";
/// Matches any run of characters, like fnmatch's `*`.
const ANY_RUN: &str = ".*";
/// A class with no members: never matches (Rust's `regex` has no `(?!)`).
const NO_CHARACTER: &str = r"[^\s\S]";
const NEGATION: char = '!';
const SET_OPEN: char = '[';
const SET_CLOSE: char = ']';
const RANGE: char = '-';

/// The regex for `glob` without anchors or flags.
///
/// Callers choose how it applies: [`whole_match_regex`] matches whole strings like
/// `fnmatch.fnmatchcase`, while content search looks for it within lines.
pub fn translate(glob: &str) -> String {
    let pattern: Vec<char> = glob.chars().collect();
    let mut regex = String::with_capacity(glob.len());
    let mut position = 0;
    let mut previous_was_star = false;
    while let Some(&character) = pattern.get(position) {
        position += 1;
        let is_star = character == '*';
        match character {
            // Consecutive stars mean the same as one; emitting one keeps the regex small.
            '*' if previous_was_star => {}
            '*' => regex.push_str(ANY_RUN),
            '?' => regex.push_str(ANY_CHARACTER),
            SET_OPEN => match CharacterSet::parse(&pattern, position) {
                Some((set, next)) => {
                    set.push_regex(&mut regex);
                    position = next;
                }
                None => push_literal(&mut regex, SET_OPEN),
            },
            _ => push_literal(&mut regex, character),
        }
        previous_was_star = is_star;
    }
    regex
}

/// A regex matching whole strings that `fnmatch.fnmatchcase(text, glob)` accepts.
pub fn whole_match_regex(glob: &str) -> Result<Regex, regex::Error> {
    Regex::new(&format!(r"(?s)\A(?:{})\z", translate(glob)))
}

#[derive(Debug, PartialEq)]
enum Member {
    Character(char),
    Range(char, char),
}

/// One `[...]` set of a glob.
#[derive(Debug, PartialEq)]
struct CharacterSet {
    negated: bool,
    members: Vec<Member>,
}

impl CharacterSet {
    /// Parses the set whose `[` precedes `start`; returns it with the index after its
    /// `]`, or `None` when the set is never closed.
    fn parse(pattern: &[char], start: usize) -> Option<(Self, usize)> {
        let mut end = start;
        if pattern.get(end) == Some(&NEGATION) {
            end += 1;
        }
        // A `]` in first position is a member, so the search for the end skips it.
        if pattern.get(end) == Some(&SET_CLOSE) {
            end += 1;
        }
        end += pattern.get(end..)?.iter().position(|&c| c == SET_CLOSE)?;
        let contents = &pattern[start..end];
        let (negated, members) = match contents.split_first() {
            Some((&NEGATION, members)) => (true, members),
            _ => (false, contents),
        };
        let set = Self {
            negated,
            members: parse_members(members),
        };
        Some((set, end + 1))
    }

    fn push_regex(&self, regex: &mut String) {
        if self.members.is_empty() {
            let empty = if self.negated {
                ANY_CHARACTER
            } else {
                NO_CHARACTER
            };
            regex.push_str(empty);
            return;
        }
        regex.push(SET_OPEN);
        if self.negated {
            regex.push('^');
        }
        for member in &self.members {
            match *member {
                Member::Character(character) => push_literal(regex, character),
                Member::Range(first, last) => {
                    push_literal(regex, first);
                    regex.push(RANGE);
                    push_literal(regex, last);
                }
            }
        }
        regex.push(SET_CLOSE);
    }
}

/// Splits set contents into characters and ranges with fnmatch's chunk rules.
///
/// Like CPython, the contents are cut at each range `-` into chunks; the last
/// character of one chunk and the first of the next form a range. A `-` in first
/// position, directly after a range, or in last position is literal.
fn parse_members(contents: &[char]) -> Vec<Member> {
    if !contents.contains(&RANGE) {
        return contents.iter().copied().map(Member::Character).collect();
    }
    let mut chunks: Vec<Vec<char>> = Vec::new();
    let mut chunk_start = 0;
    // The first character is never a range start's hyphen, so the search begins after it.
    let mut search_from = 1;
    while let Some(offset) = contents
        .get(search_from..)
        .and_then(|rest| rest.iter().position(|&c| c == RANGE))
    {
        let hyphen = search_from + offset;
        chunks.push(contents[chunk_start..hyphen].to_vec());
        chunk_start = hyphen + 1;
        // Skip the range's last character; a hyphen right after it is literal.
        search_from = hyphen + 3;
    }
    let tail = &contents[chunk_start..];
    match chunks.last_mut() {
        Some(last) if tail.is_empty() => last.push(RANGE),
        _ => chunks.push(tail.to_vec()),
    }
    drop_reversed_ranges(&mut chunks);
    members_from_chunks(&chunks)
}

/// Removes ranges such as `z-a` with their endpoints, from the end like CPython.
fn drop_reversed_ranges(chunks: &mut Vec<Vec<char>>) {
    for index in (1..chunks.len()).rev() {
        if chunks[index - 1].last() > chunks[index].first() {
            let following = chunks.remove(index);
            let merged = &mut chunks[index - 1];
            merged.pop();
            merged.extend_from_slice(&following[1..]);
        }
    }
}

/// Joins chunks back into members: each boundary between two chunks is a range.
///
/// Chunks between others keep at least two characters (a range end and a range
/// start), so the slices below never cross.
fn members_from_chunks(chunks: &[Vec<char>]) -> Vec<Member> {
    let mut members = Vec::new();
    for (index, chunk) in chunks.iter().enumerate() {
        let next = chunks.get(index + 1);
        // The first character closes the previous range; the last opens the next one.
        let literal_start = usize::from(index > 0);
        let literal_end = chunk.len() - usize::from(next.is_some());
        let literals = chunk[literal_start..literal_end].iter().copied();
        members.extend(literals.map(Member::Character));
        if let Some(next) = next {
            members.push(Member::Range(chunk[chunk.len() - 1], next[0]));
        }
    }
    members
}

fn push_literal(regex: &mut String, character: char) {
    regex.push_str(&regex::escape(character.encode_utf8(&mut [0; 4])));
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fnmatch(glob: &str, text: &str) -> bool {
        whole_match_regex(glob)
            .unwrap_or_else(|error| panic!("{glob:?} must compile: {error}"))
            .is_match(text)
    }

    #[test]
    fn wildcards_match_like_fnmatchcase() {
        // Expectations taken from CPython 3.12 `fnmatch.fnmatchcase(text, glob)`.
        let cases: &[(&str, &str, bool)] = &[
            ("*", "", true),
            ("*", "any\nthing", true),
            ("a*b", "ab", true),
            ("a*b", "a\nb", true),
            ("a**b", "axyb", true),
            ("a*b", "abc", false),
            ("?", "é", true),
            ("?", "🙂", true),
            ("?", "\n", true),
            ("?", "", false),
            ("??", "🙂", false),
            ("*.json", "report.json", true),
            ("*.json", "report.jsonx", false),
            ("a.c", "abc", false),
            ("(a|b)+{1}$^", "(a|b)+{1}$^", true),
            ("a|b", "a", false),
            ("\\", "\\", true),
            ("\\d", "\\d", true),
            ("\\d", "1", false),
            ("[0-9]", "5", true),
            ("[0-9]", "x", false),
            ("[!0-9]", "x", true),
            ("[!0-9]", "5", false),
            ("[!0-9]", "\n", true),
            ("[]]", "]", true),
            ("[!]]", "]", false),
            ("[!]]", "a", true),
            ("[]a]", "a", true),
            ("[a[]", "[", true),
            ("[a[]", "a", true),
            ("[!![]", "[", false),
            ("[!![]", "a", true),
            ("x[ab[]", "x[", true),
            ("[[]ok][*]", "[ok]*", true),
            ("[]-a]", "^", true),
            ("[]-a]", "-", false),
            ("[!]-a]", "-", true),
            ("[!]-a]", "_", false),
            ("[z-a]", "z", false),
            ("[z-a]", "-", false),
            ("[!z-a]", "z", true),
            ("[!z-a]", "-", true),
            ("[az-a]", "a", true),
            ("[az-a]", "z", false),
            ("[a-cz-a]", "b", true),
            ("[-a]", "-", true),
            ("[a-]", "-", true),
            ("[!-]", "-", false),
            ("[a-b-c]", "-", true),
            ("[a-b-c]", "c", true),
            ("[a--b]", "b", true),
            ("[a--b]", "a", false),
            ("[--a]", "-", true),
            ("[!]", "[!]", true),
            ("[]", "[]", true),
            ("[ab", "[ab", true),
            ("[ab", "a", false),
            ("[!", "[!", true),
            ("[", "[", true),
            ("[\\]", "\\", true),
            ("[\\-a]", "]", true),
            ("[^a]", "^", true),
            ("[^a]", "b", false),
            ("[&&a]", "&", true),
            ("[a&&b]", "b", true),
            ("[a~~b]", "~", true),
            ("[a||b]", "|", true),
            ("[a--z]", "-", false),
            ("[é🙂]", "🙂", true),
            ("[é-ë]", "ê", true),
            ("[!é-ë]", "ê", false),
            ("[🙂-🙃]", "🙃", true),
            ("[ä-a]", "ä", false),
        ];
        for &(glob, text, expected) in cases {
            assert_eq!(fnmatch(glob, text), expected, "{glob:?} vs {text:?}");
        }
    }

    #[test]
    fn sets_parse_into_members_with_python_chunk_rules() {
        use Member::{Character, Range};
        let set = |glob: &str| {
            let pattern: Vec<char> = glob.chars().collect();
            CharacterSet::parse(&pattern, 1).map(|(set, next)| (set.negated, set.members, next))
        };
        assert_eq!(set("[]-a]"), Some((false, vec![Range(']', 'a')], 5)));
        assert_eq!(set("[!]-z]"), Some((true, vec![Range(']', 'z')], 6)));
        assert_eq!(
            set("[a-b-c]"),
            Some((
                false,
                vec![Range('a', 'b'), Character('-'), Character('c')],
                7
            ))
        );
        assert_eq!(set("[z-a]"), Some((false, vec![], 5)));
        assert_eq!(set("[!z-a]"), Some((true, vec![], 6)));
        assert_eq!(
            set("[a-]"),
            Some((false, vec![Character('a'), Character('-')], 4))
        );
        assert_eq!(set("[!]"), None);
        assert_eq!(set("[]"), None);
    }

    #[test]
    fn output_is_an_unanchored_body_without_flags() {
        assert_eq!(translate("a*?"), "a.*.");
        assert_eq!(translate("**"), ".*");
        assert_eq!(translate("*?*"), ".*..*");
        assert_eq!(translate("[!a-c]"), "[^a-c]");
        assert_eq!(translate("[a[]"), r"[a\[]");
        assert_eq!(translate("[z-a]"), NO_CHARACTER);
        assert_eq!(translate("[!z-a]"), ANY_CHARACTER);
        assert_eq!(translate("[ab"), r"\[ab");
    }
}
