//! The reference match ladder, with lazy preparation of name words and initials.

use std::borrow::Cow;
use std::cell::OnceCell;

use regex::Regex;

use crate::fuzzy::fuzzy_score;
use crate::unicode::{isalnum, normalize};

pub const EXACT_NAME_SCORE: f64 = 150.0;
pub const NAME_PREFIX_SCORE: f64 = 110.0;
pub const WORD_PREFIX_SCORE: f64 = 90.0;
pub const NAME_CONTAINS_SCORE: f64 = 75.0;
pub const NAME_CONTAINS_POSITION_PENALTY: f64 = 0.05;
pub const ABBREVIATION_SCORE: f64 = 65.0;
pub const PATH_CONTAINS_SCORE: f64 = 35.0;
pub const MIN_FUZZY_TERM_LENGTH: usize = 2;

pub struct Term {
    pub text: String,
    pattern: Option<Regex>,
}

impl Term {
    pub fn new(text: String) -> Result<Self, regex::Error> {
        let pattern = if text.contains(['*', '?', '[']) {
            Some(Regex::new(&fnmatch_rs::translate(
                &text.replace('\\', "/"),
            ))?)
        } else {
            None
        };
        Ok(Self { text, pattern })
    }
}

pub struct NameForms<'a> {
    pub original: &'a str,
    pub normalized: Cow<'a, str>,
    without_extension_end: usize,
    words: OnceCell<Vec<String>>,
    initials: OnceCell<String>,
}

impl<'a> NameForms<'a> {
    pub fn new(original: &'a str, normalized: Option<&'a str>) -> Self {
        let normalized = normalized.map_or_else(|| Cow::Owned(normalize(original)), Cow::Borrowed);
        let without_extension_end = normalized.rfind('.').unwrap_or(normalized.len());
        Self {
            original,
            normalized,
            without_extension_end,
            words: OnceCell::new(),
            initials: OnceCell::new(),
        }
    }

    pub fn without_extension(&self) -> &str {
        &self.normalized[..self.without_extension_end]
    }

    pub fn words(&self) -> &[String] {
        self.words.get_or_init(|| name_words(self.original))
    }

    pub fn initials(&self) -> &str {
        self.initials.get_or_init(|| {
            self.words()
                .iter()
                .filter_map(|word| word.chars().next())
                .collect()
        })
    }
}

/// Python's [^\W_]+ words after inserting ASCII camel-case boundaries.
fn name_words(original: &str) -> Vec<String> {
    let mut words = Vec::new();
    let mut start = None;
    let mut previous = '\0';
    for (position, character) in original.char_indices() {
        let alphanumeric = isalnum(character);
        let camel_boundary = (previous.is_ascii_lowercase() || previous.is_ascii_digit())
            && character.is_ascii_uppercase();
        if (!alphanumeric || camel_boundary) && start.is_some() {
            words.push(normalize(&original[start.take().unwrap()..position]));
        }
        if alphanumeric && start.is_none() {
            start = Some(position);
        }
        previous = character;
    }
    if let Some(position) = start {
        words.push(normalize(&original[position..]));
    }
    words
}

pub fn match_term(
    term: &Term,
    name: &NameForms<'_>,
    path: Option<&str>,
    fuzzy: bool,
) -> Option<(f64, &'static str)> {
    if let Some(pattern) = &term.pattern {
        if pattern.is_match(&name.normalized.replace('\\', "/")) {
            return Some((WORD_PREFIX_SCORE, "Name wildcard"));
        }
        if let Some(path) = path {
            let filename = path.rsplit(['/', '\\']).next().unwrap_or("");
            if pattern.is_match(filename) {
                return Some((WORD_PREFIX_SCORE, "Name wildcard"));
            }
            if term.text.contains(['/', '\\']) && pattern.is_match(&path.replace('\\', "/")) {
                return Some((PATH_CONTAINS_SCORE, "Path wildcard"));
            }
        }
        return None;
    }
    let term = term.text.as_str();
    if term == name.normalized.as_ref() || term == name.without_extension() {
        return Some((EXACT_NAME_SCORE, "Exact name"));
    }
    if name.normalized.starts_with(term) {
        return Some((NAME_PREFIX_SCORE, "Name prefix"));
    }
    if name.words().iter().any(|word| word.starts_with(term)) {
        return Some((WORD_PREFIX_SCORE, "Word prefix"));
    }
    if let Some(position) = name.normalized.find(term) {
        let character_position = name.normalized[..position].chars().count();
        return Some((
            NAME_CONTAINS_SCORE - character_position as f64 * NAME_CONTAINS_POSITION_PENALTY,
            "Name contains query",
        ));
    }
    let fuzzy_term = fuzzy && term.chars().count() >= MIN_FUZZY_TERM_LENGTH;
    if fuzzy_term && name.initials().starts_with(term) {
        return Some((ABBREVIATION_SCORE, "Abbreviation"));
    }
    if path.is_some_and(|path| path.contains(term)) {
        return Some((PATH_CONTAINS_SCORE, "Path contains query"));
    }
    if fuzzy_term {
        return fuzzy_score(term, name.original).map(|score| (score, "Fuzzy match"));
    }
    None
}

pub fn best_possible_score(term: &str, normalized_name: &str) -> f64 {
    if normalized_name.starts_with(term) {
        EXACT_NAME_SCORE
    } else {
        WORD_PREFIX_SCORE
    }
}

pub fn matches_folder_order(parts: &[Term], folders: &[String], fuzzy: bool) -> bool {
    let mut start = 0;
    for part in parts {
        let position = folders[start..].iter().position(|folder| {
            match_term(part, &NameForms::new(folder, None), None, fuzzy).is_some()
        });
        match position {
            Some(position) => start += position + 1,
            None => return false,
        }
    }
    true
}

/// Python 3.12 PureWindowsPath(path).parent.parts, without filesystem access.
pub fn parent_parts(path: &str) -> Vec<String> {
    let path = path.replace('/', "\\");
    let (drive, mut rooted, relative) = split_windows_root(&path);
    if !rooted && drive.starts_with('\\') && !drive.ends_with('\\') {
        let components: Vec<&str> = drive.split('\\').collect();
        rooted = (components.len() == 4 && !["", "?", "."].contains(&components[2]))
            || components.len() == 6;
    }
    let mut tail: Vec<String> = relative
        .split('\\')
        .filter(|component| !component.is_empty() && *component != ".")
        .map(String::from)
        .collect();
    tail.pop();
    if !drive.is_empty() || rooted {
        let anchor = format!("{drive}{}", if rooted { "\\" } else { "" });
        tail.insert(0, anchor);
    }
    tail
}

fn split_windows_root(path: &str) -> (&str, bool, &str) {
    if path.starts_with("\\\\") {
        let start = if path
            .get(..8)
            .is_some_and(|prefix| prefix.eq_ignore_ascii_case("\\\\?\\UNC\\"))
        {
            8
        } else {
            2
        };
        if let Some(first) = path[start..].find('\\').map(|position| start + position) {
            if let Some(second) = path[first + 1..]
                .find('\\')
                .map(|position| first + 1 + position)
            {
                return (&path[..second], true, &path[second + 1..]);
            }
        }
        return (path, false, "");
    }
    if let Some(relative) = path.strip_prefix('\\') {
        return ("", true, relative);
    }
    // ntpath checks the second code point for ':', even for non-ASCII drives.
    if let Some((position, ':')) = path.char_indices().nth(1) {
        let end = position + 1;
        let relative = &path[end..];
        return match relative.strip_prefix('\\') {
            Some(relative) => (&path[..end], true, relative),
            None => (&path[..end], false, relative),
        };
    }
    ("", false, path)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn matching(term: &str, original: &str, fuzzy: bool) -> Option<(f64, &'static str)> {
        match_term(
            &Term::new(term.into()).unwrap(),
            &NameForms::new(original, None),
            None,
            fuzzy,
        )
    }

    fn terms(texts: &[&str]) -> Vec<Term> {
        texts
            .iter()
            .map(|text| Term::new((*text).into()).unwrap())
            .collect()
    }

    #[test]
    fn ladder_preserves_scores_and_reason_order() {
        assert_eq!(
            matching("report", "Report.txt", true),
            Some((150.0, "Exact name"))
        );
        assert_eq!(
            matching("report", "Report-final.txt", true),
            Some((110.0, "Name prefix"))
        );
        assert_eq!(
            matching("report", "Annual Report.txt", true),
            Some((90.0, "Word prefix"))
        );
        assert_eq!(
            matching("port", "Report.txt", true),
            Some((74.9, "Name contains query"))
        );
        assert_eq!(
            matching("vsc", "VisualStudioCode.exe", true),
            Some((65.0, "Abbreviation"))
        );
        assert_eq!(matching("vsc", "VisualStudioCode.exe", false), None);
        assert_eq!(
            matching("rpt", "Report.txt", true).unwrap().1,
            "Fuzzy match"
        );
        assert_eq!(matching("zzz", "Report.txt", true), None);
        let name = NameForms::new("Report.txt", None);
        assert_eq!(
            match_term(&terms(&["rpt"])[0], &name, Some("c:/rpt/report.txt"), true),
            Some((35.0, "Path contains query"))
        );
        let abbreviation = NameForms::new("VisualStudioCode.exe", None);
        assert_eq!(
            match_term(&terms(&["vsc"])[0], &abbreviation, Some("c:/vsc"), true),
            Some((65.0, "Abbreviation"))
        );
    }

    #[test]
    fn positions_are_code_points_and_words_follow_python_regex() {
        assert_eq!(
            matching("port", "é🙂Report.txt", false),
            Some((74.8, "Name contains query"))
        );
        let name = NameForms::new("éUpper lowerUpper 9Code ²frac_ǅName Cafe\u{301}X.txt", None);
        assert_eq!(
            name.words(),
            ["éupper", "lower", "upper", "9", "code", "²frac", "ǆname", "cafe", "x", "txt"]
        );
        assert_eq!(name.initials(), "élu9c²ǆcxt");
    }

    #[test]
    fn exact_and_prefix_do_not_prepare_words() {
        let name = NameForms::new("Report.txt", Some("report.txt"));
        assert_eq!(
            match_term(&terms(&["report"])[0], &name, None, true)
                .unwrap()
                .0,
            150.0
        );
        assert_eq!(
            match_term(&terms(&["rep"])[0], &name, None, true)
                .unwrap()
                .0,
            110.0
        );
        assert!(name.words.get().is_none());
        assert!(name.initials.get().is_none());
    }

    #[test]
    fn extension_removal_and_empty_terms_match_the_reference() {
        assert_eq!(
            matching("archive.tar", "archive.tar.gz", false).unwrap().0,
            150.0
        );
        assert_eq!(matching("file", "file.", false).unwrap().0, 150.0);
        assert_eq!(matching("", ".hidden", false).unwrap().0, 150.0);
        assert_eq!(matching("", "file", false).unwrap().0, 110.0);
        assert_eq!(matching("strasse", "Straße.txt", false).unwrap().0, 150.0);
    }

    #[test]
    fn folder_parts_need_distinct_increasing_parent_positions() {
        let folders: Vec<String> = ["C:\\", "Projects", "Other", "AnnualReports"]
            .map(String::from)
            .into();
        assert!(matches_folder_order(
            &terms(&["proj", "rep"]),
            &folders,
            false
        ));
        assert!(!matches_folder_order(
            &terms(&["rep", "proj"]),
            &folders,
            true
        ));
        assert!(!matches_folder_order(
            &terms(&["proj", "proj"]),
            &folders,
            true
        ));
        assert!(matches_folder_order(&[], &folders, true));
        assert!(matches_folder_order(&terms(&["c:"]), &folders, false));
        assert_eq!(best_possible_score("rep", "report.txt"), 150.0);
        assert_eq!(best_possible_score("rep", "annual report.txt"), 90.0);
    }

    #[test]
    fn windows_parents_preserve_anchors_and_remove_only_the_last_component() {
        let cases: &[(&str, &[&str])] = &[
            ("C:/Work/Reports/file.txt", &["C:\\", "Work", "Reports"]),
            ("C:/", &["C:\\"]),
            ("C:folder/file.txt", &["C:", "folder"]),
            ("C:", &["C:"]),
            ("/Root/Sub/file.txt", &["\\", "Root", "Sub"]),
            (
                "//server/share/Folder/file.txt",
                &["\\\\server\\share\\", "Folder"],
            ),
            ("//server/share", &["\\\\server\\share\\"]),
            ("//server/share/", &["\\\\server\\share\\"]),
            (
                "//?/UNC/server/share/Folder/file.txt",
                &["\\\\?\\UNC\\server\\share\\", "Folder"],
            ),
            ("//?/C:/Folder/file.txt", &["\\\\?\\C:\\", "Folder"]),
            ("//./C:/Folder/file.txt", &["\\\\.\\C:\\", "Folder"]),
            ("//foo", &["\\\\foo"]),
            ("///server/share/file.txt", &["\\\\\\server\\", "share"]),
            ("C:/a/./b/../file.txt", &["C:\\", "a", "b", ".."]),
            ("C:/a/b/", &["C:\\", "a"]),
            ("C:/a/b/.", &["C:\\", "a"]),
            ("relative/file.txt", &["relative"]),
            ("file.txt", &[]),
            ("", &[]),
            ("🙂:/Folder/file.txt", &["🙂:\\", "Folder"]),
        ];
        for (path, expected) in cases {
            assert_eq!(parent_parts(path), *expected, "{path:?}");
        }
    }
}
