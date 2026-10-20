//! Priority rules, evaluated natively where Rust matches Python exactly and by
//! Python's own `re` everywhere else (non-ASCII names, unsupported patterns).

use std::collections::HashMap;

use fancy_regex::Regex;
use pyo3::exceptions::{PyOverflowError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyDict;

use crate::query::{required, Candidate, Kind};

/// Ordered so that combining two classes keeps the stronger demotion:
/// ignored > low > high > normal.
#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd)]
pub(crate) enum SearchClass {
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

// Rule specifications as sent by Python's `priority_spec`.

#[derive(FromPyObject)]
struct FolderSpec {
    #[pyo3(item)]
    key: String,
    #[pyo3(item)]
    path_length: usize,
    #[pyo3(item)]
    weight: i64,
    #[pyo3(item("class"))]
    class: String,
}

#[derive(FromPyObject)]
struct NameSpec {
    #[pyo3(item)]
    pattern: String,
    /// The compiled Python pattern's `search` method.
    #[pyo3(item)]
    python_search: Py<PyAny>,
    #[pyo3(item)]
    weight: i64,
    #[pyo3(item("class"))]
    class: String,
    #[pyo3(item)]
    files: bool,
    #[pyo3(item)]
    folders: bool,
}

#[derive(FromPyObject)]
struct ExtensionSpec {
    #[pyo3(item)]
    extension: String,
    #[pyo3(item)]
    weight: i64,
    #[pyo3(item("class"))]
    class: String,
}

#[derive(FromPyObject)]
struct AttributeSpec {
    #[pyo3(item)]
    mask: i64,
    #[pyo3(item)]
    weight: i64,
    #[pyo3(item("class"))]
    class: String,
}

#[derive(Debug)]
struct FolderRule {
    key: String,
    path_length: usize,
    rule: Rule,
}

#[derive(Debug)]
struct NameRule {
    native: Option<Regex>,
    python_search: Option<Py<PyAny>>,
    files: bool,
    folders: bool,
    rule: Rule,
}

impl NameRule {
    fn applies_to(&self, kind: Kind) -> bool {
        if kind == Kind::Folder {
            self.folders
        } else {
            self.files
        }
    }

    fn matches(&self, file_name: &str) -> PyResult<bool> {
        if let (Some(regex), true) = (&self.native, file_name.is_ascii()) {
            // fancy-regex compares backreferences literally; fold ASCII to match Python.
            if let Ok(found) = regex.is_match(&file_name.to_ascii_lowercase()) {
                return Ok(found);
            }
            // A backtracking limit was hit; Python decides instead.
        }
        let search = self.python_search.as_ref().ok_or_else(|| {
            PyValueError::new_err("Filename priority rule needs its Python pattern")
        })?;
        Python::with_gil(|py| search.call1(py, (file_name,))?.is_truthy(py))
    }
}

#[derive(Debug, Eq, PartialEq)]
pub struct Decision {
    pub class: SearchClass,
    pub boost: i64,
}

const NO_PRIORITY: Decision = Decision {
    class: SearchClass::Normal,
    boost: 0,
};

#[derive(Debug, Default)]
pub struct Policy {
    folders: Vec<FolderRule>,
    names: Vec<NameRule>,
    extensions: HashMap<String, Vec<Rule>>,
    attributes: Vec<(i64, Rule)>,
}

impl Policy {
    pub fn from_dict(value: &Bound<'_, PyDict>) -> PyResult<Self> {
        let folders: Vec<FolderSpec> = required(value, "folders")?;
        let names: Vec<NameSpec> = required(value, "names")?;
        let extensions: Vec<ExtensionSpec> = required(value, "extensions")?;
        let attributes: Vec<AttributeSpec> = required(value, "attributes")?;
        let mut policy = Self::default();
        for spec in folders {
            policy.folders.push(FolderRule {
                key: spec.key,
                path_length: spec.path_length,
                rule: Rule::new(spec.weight, &spec.class)?,
            });
        }
        for spec in names {
            policy.names.push(NameRule {
                native: compile_name_pattern(&spec.pattern),
                python_search: Some(spec.python_search),
                files: spec.files,
                folders: spec.folders,
                rule: Rule::new(spec.weight, &spec.class)?,
            });
        }
        for spec in extensions {
            let rule = Rule::new(spec.weight, &spec.class)?;
            policy
                .extensions
                .entry(spec.extension)
                .or_default()
                .push(rule);
        }
        for spec in attributes {
            policy
                .attributes
                .push((spec.mask, Rule::new(spec.weight, &spec.class)?));
        }
        Ok(policy)
    }

    fn is_empty(&self) -> bool {
        self.folders.is_empty()
            && self.names.is_empty()
            && self.extensions.is_empty()
            && self.attributes.is_empty()
    }

    pub fn evaluate(&self, candidate: &Candidate) -> PyResult<Decision> {
        if self.is_empty() {
            return Ok(NO_PRIORITY); // Searches evaluate every candidate; skip the work.
        }
        self.evaluate_fields(
            &candidate.identity,
            candidate.file_name(),
            candidate.kind,
            &candidate.extension,
            candidate.attributes,
        )
    }

    fn evaluate_fields(
        &self,
        identity: &str,
        file_name: &str,
        kind: Kind,
        extension: &str,
        attributes: i64,
    ) -> PyResult<Decision> {
        let mut decision = NO_PRIORITY;
        if let Some(folder) = self.most_specific_folder(identity) {
            add_rule(&folder.rule, &mut decision)?;
        }
        for name in &self.names {
            if name.applies_to(kind) && name.matches(file_name)? {
                add_rule(&name.rule, &mut decision)?;
            }
        }
        if kind != Kind::Folder {
            for rule in self.extensions.get(extension).into_iter().flatten() {
                add_rule(rule, &mut decision)?;
            }
        }
        for (mask, rule) in &self.attributes {
            if attributes & mask == *mask {
                add_rule(rule, &mut decision)?;
            }
        }
        Ok(decision)
    }

    /// The first rule with the longest folder path among those containing the item.
    fn most_specific_folder(&self, identity: &str) -> Option<&FolderRule> {
        let mut found: Option<&FolderRule> = None;
        for folder in &self.folders {
            let longer = found.is_none_or(|best| folder.path_length > best.path_length);
            if longer && identity.starts_with(&folder.key) {
                found = Some(folder);
            }
        }
        found
    }
}

fn add_rule(rule: &Rule, decision: &mut Decision) -> PyResult<()> {
    decision.class = decision.class.max(rule.class);
    decision.boost = decision
        .boost
        .checked_add(rule.weight)
        .ok_or_else(|| PyOverflowError::new_err("Priority boost exceeds native integer range"))?;
    Ok(())
}

/// A native regex when the pattern is in the ASCII subset whose semantics match
/// Python `re`; otherwise None, and the rule is evaluated by Python.
fn compile_name_pattern(pattern: &str) -> Option<Regex> {
    let translated = translate_pattern(pattern).ok()?;
    // Builder flags do not reach fancy-regex's lookaround/backreference compiler.
    Regex::new(&format!("(?i:{translated})")).ok()
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

    fn native_rule(pattern: &str, files: bool, folders: bool, weight: i64) -> NameRule {
        NameRule {
            native: compile_name_pattern(pattern),
            python_search: None,
            files,
            folders,
            rule: rule(weight, SearchClass::Normal),
        }
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
        policy
            .names
            .push(native_rule(r"(?<=report)\.txt$", true, false, 3));
        policy.extensions.insert(
            "txt".into(),
            vec![rule(7, SearchClass::High), rule(-2, SearchClass::Normal)],
        );
        policy.attributes = vec![
            (2, rule(-10, SearchClass::Low)),
            (6, rule(100, SearchClass::Ignored)),
            (0, rule(1, SearchClass::Normal)),
        ];
        let identity = "C:/WORK/DEEP/REPORT.TXT/";
        let evaluate = |kind, attributes| {
            policy
                .evaluate_fields(identity, "REPORT.TXT", kind, "txt", attributes)
                .unwrap()
        };
        assert_eq!(
            evaluate(Kind::File, 2),
            Decision {
                class: SearchClass::Low,
                boost: 4
            }
        );
        assert_eq!(
            evaluate(Kind::Folder, 2),
            Decision {
                class: SearchClass::Low,
                boost: -4
            }
        );
        assert_eq!(
            evaluate(Kind::App, 6),
            Decision {
                class: SearchClass::Ignored,
                boost: 104
            }
        );
        let empty = Policy::default()
            .evaluate_fields("", "Straße", Kind::File, "", 0)
            .unwrap();
        assert_eq!(empty, NO_PRIORITY);
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
            let found = native_rule(pattern, true, true, 0).matches(name).unwrap();
            assert_eq!(found, matches, "{pattern:?} on {name:?}");
        }
        for pattern in [r"(?m)report$", r"[a&&b]", r"\B", r"\u0130", "Café"] {
            assert!(translate_pattern(pattern).is_err(), "{pattern}");
            assert!(compile_name_pattern(pattern).is_none(), "{pattern}");
        }
    }

    #[test]
    fn non_ascii_names_and_python_only_patterns_are_left_to_python() {
        let rule = native_rule("report", true, true, 0);
        // Without its Python pattern the rule must refuse rather than guess.
        assert!(rule.matches("Größe-report.txt").is_err());
        assert!(rule.matches("report.txt").unwrap());
        assert!(native_rule("Café", true, true, 0).matches("cafe").is_err());
    }
}
