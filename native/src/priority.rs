//! Priority rules, with explicit fallback when Python regex parity is uncertain.

use std::collections::HashMap;

use fancy_regex::Regex;
use pyo3::exceptions::{PyOverflowError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyDict;

use crate::query::{required, Candidate};

#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd)]
enum SearchClass {
    Normal,
    High,
    Low,
    Ignored,
}

impl SearchClass {
    fn parse(value: &str) -> PyResult<Self> {
        match value {
            "normal" => Ok(Self::Normal),
            "high" => Ok(Self::High),
            "low" => Ok(Self::Low),
            "ignored" => Ok(Self::Ignored),
            _ => Err(PyValueError::new_err("Invalid search priority class")),
        }
    }

    fn as_str(self) -> &'static str {
        match self {
            Self::Normal => "normal",
            Self::High => "high",
            Self::Low => "low",
            Self::Ignored => "ignored",
        }
    }
}

#[derive(Debug)]
struct Rule {
    weight: i64,
    class: SearchClass,
}

impl Rule {
    fn new(weight: i64, class: &str) -> PyResult<Self> {
        Ok(Self {
            weight,
            class: SearchClass::parse(class)?,
        })
    }
}

#[derive(Debug)]
struct FolderRule {
    key: String,
    path_length: usize,
    rule: Rule,
}

#[derive(Debug)]
struct NameRule {
    regex: Regex,
    files: bool,
    folders: bool,
    rule: Rule,
}

#[derive(Debug, Eq, PartialEq)]
pub struct Decision {
    pub search: String,
    pub boost: i64,
}

#[derive(Debug, Default)]
pub struct Policy {
    folders: Vec<FolderRule>,
    names: Vec<NameRule>,
    extensions: HashMap<String, Vec<Rule>>,
    attributes: Vec<(i64, Rule)>,
}

impl Policy {
    pub fn has_name_rules(&self) -> bool {
        !self.names.is_empty()
    }

    pub fn validate_candidate(&self, candidate: &Candidate) -> PyResult<()> {
        // Surface regex backtracking failures before any exclusion callback runs.
        self.evaluate(candidate).map(|_| ())
    }

    fn validate_filename(&self, file_name: &str, kind: &str) -> PyResult<()> {
        if self.has_name_rules()
            && !file_name.is_ascii()
            && self.names.iter().any(|name| {
                if kind == "folder" {
                    name.folders
                } else {
                    name.files
                }
            })
        {
            // shortcut: Unicode regex uses Python, replace when native casing/classes agree.
            return Err(regex_fallback("non-ASCII filename"));
        }
        Ok(())
    }

    pub fn from_dict(value: &Bound<'_, PyDict>) -> PyResult<Self> {
        let folders: Vec<(String, usize, i64, String)> = required(value, "folders")?;
        let names: Vec<(String, i64, String, bool, bool)> = required(value, "names")?;
        let extensions: Vec<(String, i64, String)> = required(value, "extensions")?;
        let attributes: Vec<(i64, i64, String)> = required(value, "attributes")?;
        let mut policy = Self::default();
        for (key, path_length, weight, class) in folders {
            policy.folders.push(FolderRule {
                key,
                path_length,
                rule: Rule::new(weight, &class)?,
            });
        }
        for (pattern, weight, class, files, folders) in names {
            policy.names.push(NameRule {
                regex: compile_name_pattern(&pattern)?,
                files,
                folders,
                rule: Rule::new(weight, &class)?,
            });
        }
        for (extension, weight, class) in extensions {
            policy
                .extensions
                .entry(extension)
                .or_default()
                .push(Rule::new(weight, &class)?);
        }
        for (mask, weight, class) in attributes {
            policy.attributes.push((mask, Rule::new(weight, &class)?));
        }
        Ok(policy)
    }

    pub fn evaluate(&self, candidate: &Candidate) -> PyResult<Decision> {
        if self.folders.is_empty()
            && self.names.is_empty()
            && self.extensions.is_empty()
            && self.attributes.is_empty()
        {
            return Ok(Decision {
                search: "normal".to_owned(),
                boost: 0,
            });
        }
        self.evaluate_fields(
            &candidate.identity,
            candidate.file_name(),
            &candidate.kind,
            &candidate.extension,
            candidate.attributes,
        )
    }

    fn evaluate_fields(
        &self,
        identity: &str,
        file_name: &str,
        kind: &str,
        extension: &str,
        attributes: i64,
    ) -> PyResult<Decision> {
        let mut class = SearchClass::Normal;
        let mut boost = 0;
        let mut most_specific: Option<&FolderRule> = None;
        for folder in &self.folders {
            if identity.starts_with(&folder.key)
                && most_specific.is_none_or(|found| folder.path_length > found.path_length)
            {
                most_specific = Some(folder);
            }
        }
        if let Some(folder) = most_specific {
            add_rule(&folder.rule, &mut class, &mut boost)?;
        }
        let is_folder = kind == "folder";
        if self.has_name_rules() {
            self.validate_filename(file_name, kind)?;
            // fancy-regex compares backreferences literally; fold ASCII to match Python.
            let folded_name = file_name.to_ascii_lowercase();
            for name in &self.names {
                if !(if is_folder { name.folders } else { name.files }) {
                    continue;
                }
                if name
                    .regex
                    .is_match(&folded_name)
                    .map_err(|error| regex_fallback(&error.to_string()))?
                {
                    add_rule(&name.rule, &mut class, &mut boost)?;
                }
            }
        }
        if !is_folder {
            if let Some(rules) = self.extensions.get(extension) {
                for rule in rules {
                    add_rule(rule, &mut class, &mut boost)?;
                }
            }
        }
        for (mask, rule) in &self.attributes {
            if attributes & mask == *mask {
                add_rule(rule, &mut class, &mut boost)?;
            }
        }
        Ok(Decision {
            search: class.as_str().to_owned(),
            boost,
        })
    }
}

fn add_rule(rule: &Rule, class: &mut SearchClass, boost: &mut i64) -> PyResult<()> {
    *class = (*class).max(rule.class);
    *boost = boost
        .checked_add(rule.weight)
        .ok_or_else(|| PyOverflowError::new_err("Priority boost exceeds native integer range"))?;
    Ok(())
}

fn regex_fallback(detail: &str) -> PyErr {
    PyValueError::new_err(format!(
        "Filename priority expression requires Python regex: {detail}"
    ))
}

fn compile_name_pattern(pattern: &str) -> PyResult<Regex> {
    let translated = translate_pattern(pattern).map_err(|detail| regex_fallback(&detail))?;
    // Builder flags do not reach fancy-regex's lookaround/backreference compiler.
    Regex::new(&format!("(?i:{translated})")).map_err(|error| regex_fallback(&error.to_string()))
}

/// Translate the ASCII subset whose search semantics agree with Python `re`.
/// Unsupported constructs are surfaced to the adapter, never ignored.
fn translate_pattern(pattern: &str) -> Result<String, String> {
    if !pattern.is_ascii() {
        return Err("non-ASCII pattern".to_owned());
    }
    let bytes = pattern.as_bytes();
    let mut translated = String::with_capacity(pattern.len());
    let mut position = 0;
    let mut in_class = false;
    let mut class_has_content = false;
    let mut class_can_negate = false;
    while position < bytes.len() {
        let current = bytes[position];
        if current == b'\\' {
            position += 1;
            let escaped = *bytes
                .get(position)
                .ok_or_else(|| "trailing backslash".to_owned())?;
            match escaped {
                b'Z' if !in_class => translated.push_str(r"\z"),
                b's' => {
                    if in_class {
                        translated.push_str(r"\t\n\x0b\x0c\r \x1c-\x1f");
                    } else {
                        translated.push_str(r"[\t\n\x0b\x0c\r \x1c-\x1f]");
                    }
                }
                b'S' => {
                    if in_class {
                        translated.push_str(r"\x00-\x08\x0e-\x1b\x21-\x7f");
                    } else {
                        translated.push_str(r"[\x00-\x08\x0e-\x1b\x21-\x7f]");
                    }
                }
                b'b' if in_class => translated.push_str(r"\x08"),
                b'x' => {
                    let digits = bytes
                        .get(position + 1..position + 3)
                        .ok_or_else(|| "incomplete hex escape".to_owned())?;
                    let hex = std::str::from_utf8(digits).unwrap();
                    let code =
                        u8::from_str_radix(hex, 16).map_err(|_| "invalid hex escape".to_owned())?;
                    if !code.is_ascii() {
                        return Err("non-ASCII hex escape".to_owned());
                    }
                    translated.push_str(r"\x");
                    translated.push_str(hex);
                    position += 2;
                }
                b'1'..=b'9' if !in_class => {
                    // Three octal digits are a character in Python, not a backref.
                    if bytes
                        .get(position..position + 3)
                        .is_some_and(|digits| digits.iter().all(|d| matches!(d, b'0'..=b'7')))
                    {
                        return Err("octal escape".to_owned());
                    }
                    translated.push('\\');
                    translated.push(escaped as char);
                }
                b'a' | b'b' | b'f' | b'n' | b'r' | b't' | b'v' | b'd' | b'D' | b'w' | b'W'
                | b'A' => {
                    translated.push('\\');
                    translated.push(escaped as char);
                }
                _ if !escaped.is_ascii_alphanumeric() => {
                    // Escaped punctuation is literal in Python, but fancy-regex reserves \< and \>.
                    translated.push_str(&format!(r"\x{escaped:02x}"));
                }
                _ => return Err(format!("unsupported escape \\{}", escaped as char)),
            }
            if in_class {
                class_has_content = true;
                class_can_negate = false;
            }
        } else if in_class {
            if current == b'[' {
                return Err("nested character class".to_owned());
            }
            if matches!(current, b'&' | b'|' | b'~' | b'-')
                && bytes.get(position + 1) == Some(&current)
            {
                return Err("character class set operation".to_owned());
            }
            if current == b']' && class_has_content {
                in_class = false;
            } else if current != b'^' || !class_can_negate {
                class_has_content = true;
            }
            class_can_negate = false;
            translated.push(current as char);
        } else {
            match current {
                b'[' => {
                    in_class = true;
                    class_has_content = false;
                    class_can_negate = true;
                    translated.push('[');
                }
                b'$' => translated.push_str(r"(?=\n?\z)"),
                b'(' if bytes.get(position + 1) == Some(&b'?') => {
                    let suffix = &pattern[position..];
                    if !["(?:", "(?=", "(?!", "(?<=", "(?<!", "(?P<", "(?P="]
                        .iter()
                        .any(|prefix| suffix.starts_with(prefix))
                    {
                        return Err("inline flags or unsupported group".to_owned());
                    }
                    translated.push('(');
                }
                _ => translated.push(current as char),
            }
        }
        position += 1;
    }
    Ok(translated)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn rule(weight: i64, class: SearchClass) -> Rule {
        Rule { weight, class }
    }

    #[test]
    fn priorities_sum_all_rules_but_only_the_first_most_specific_folder() {
        let mut policy = Policy {
            folders: vec![
                FolderRule {
                    key: "C:/WORK/".into(),
                    path_length: 7,
                    rule: rule(80, SearchClass::Ignored),
                },
                FolderRule {
                    key: "C:/WORK/DEEP/".into(),
                    path_length: 12,
                    rule: rule(5, SearchClass::High),
                },
                FolderRule {
                    key: "C:/WORK/DEEP/".into(),
                    path_length: 12,
                    rule: rule(100, SearchClass::Ignored),
                },
            ],
            ..Policy::default()
        };
        policy.names.push(NameRule {
            regex: compile_name_pattern(r"(?<=report)\.txt$").unwrap(),
            files: true,
            folders: false,
            rule: rule(3, SearchClass::Normal),
        });
        policy.extensions.insert(
            "txt".into(),
            vec![rule(7, SearchClass::High), rule(-2, SearchClass::Normal)],
        );
        policy.attributes = vec![
            (2, rule(-10, SearchClass::Low)),
            (6, rule(100, SearchClass::Ignored)),
            (0, rule(1, SearchClass::Normal)),
        ];
        let decision = policy
            .evaluate_fields("C:/WORK/DEEP/REPORT.TXT/", "REPORT.TXT", "file", "txt", 2)
            .unwrap();
        assert_eq!(decision.search, "low");
        assert_eq!(decision.boost, 4);
        let folder = policy
            .evaluate_fields("C:/WORK/DEEP/REPORT.TXT/", "REPORT.TXT", "folder", "txt", 2)
            .unwrap();
        assert_eq!(folder.search, "low");
        assert_eq!(folder.boost, -4);
        let ignored = policy
            .evaluate_fields("C:/WORK/DEEP/REPORT.TXT/", "REPORT.TXT", "app", "txt", 6)
            .unwrap();
        assert_eq!(ignored.search, "ignored");
        assert_eq!(ignored.boost, 104);
        assert_eq!(
            Policy::default()
                .evaluate_fields("", "Straße", "file", "", 0)
                .unwrap(),
            Decision {
                search: "normal".into(),
                boost: 0
            }
        );
    }

    #[test]
    fn regex_translation_keeps_python_ascii_search_semantics() {
        for (pattern, name, matches) in [
            (r"report$", "REPORT\n", true),
            (r"report\Z", "report\n", false),
            (r"report\Z", "report", true),
            (r"\s", "\u{1c}", true),
            (r"\S", "\u{1c}", false),
            (r"[\s]", "\u{1f}", true),
            (r"[^\s]", "\u{1f}", false),
            (r"[\S]", "x", true),
            (r"[\b]", "\u{08}", true),
            (r"[^^]$", "a\n", true),
            (r"\<report\>", "<Report>", true),
            (r"\<report\>", "report", false),
            (r"\b[a-z]+\.txt$", "Report.TXT", true),
            (r"(?<=annual )report(?=\.txt)", "Annual Report.txt", true),
            (r"(?<=Annual )Report(?=\.txt$)", "Annual Report.txt", true),
            (r"(?<=Annual )Report", "Annual Report.txt", true),
            (r"Report(?=\.txt$)", "Annual Report.txt", true),
            (r"([a-z]+)-\1", "Report-REPORT", true),
            (r"(?P<name>[a-z]+)-(?P=name)", "Report-REPORT", true),
        ] {
            assert_eq!(
                compile_name_pattern(pattern)
                    .unwrap()
                    .is_match(&name.to_ascii_lowercase())
                    .unwrap(),
                matches,
                "{pattern:?} on {name:?}"
            );
        }
        for pattern in [r"(?m)report$", r"[a&&b]", r"\B", r"\u0130", "Café"] {
            assert!(translate_pattern(pattern).is_err(), "{pattern}");
        }
        let policy = Policy {
            names: vec![NameRule {
                regex: compile_name_pattern("report").unwrap(),
                files: false,
                folders: true,
                rule: rule(0, SearchClass::High),
            }],
            ..Policy::default()
        };
        assert!(policy.evaluate_fields("", "Café", "folder", "", 0).is_err());
        assert_eq!(
            policy
                .evaluate_fields("", "Café", "file", "", 0)
                .unwrap()
                .search,
            "normal"
        );
    }
}
