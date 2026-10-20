//! Streaming fnmatch content checks for the candidates that survived filename ranking.

use std::fs::File;
use std::io::{self, Read, Take};

use grep_matcher::LineTerminator;
use grep_regex::{RegexMatcher, RegexMatcherBuilder};
use grep_searcher::{BinaryDetection, Searcher, SearcherBuilder, Sink, SinkMatch};

use crate::glob;
use crate::query::{Candidate, Kind};
use crate::CancelFlag;

/// Larger files are skipped; content search is for documents and source files.
const MAX_FILE_BYTES: u64 = 64 * 1024 * 1024;
/// A longer single line counts as no match instead of growing memory per worker.
const MAX_LINE_BYTES: usize = 8 * 1024 * 1024;
/// Cancellation is checked between reads of this size.
const READ_CHUNK_BYTES: usize = 64 * 1024;
/// Windows attributes of cloud placeholders whose reading would download content.
const FILE_ATTRIBUTE_OFFLINE: u32 = 0x0000_1000;
const FILE_ATTRIBUTE_RECALL_ON_OPEN: u32 = 0x0004_0000;
const FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS: u32 = 0x0040_0000;
const NOT_LOCALLY_AVAILABLE: u32 =
    FILE_ATTRIBUTE_OFFLINE | FILE_ATTRIBUTE_RECALL_ON_OPEN | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS;

const TEXT_EXTENSIONS: &[&str] = &[
    "txt",
    "text",
    "md",
    "markdown",
    "rst",
    "log",
    "json",
    "jsonc",
    "jsonl",
    "ndjson",
    "csv",
    "tsv",
    "toml",
    "yaml",
    "yml",
    "ini",
    "cfg",
    "conf",
    "config",
    "properties",
    "xml",
    "html",
    "htm",
    "css",
    "scss",
    "less",
    "svg",
    "tex",
    "sql",
    "js",
    "jsx",
    "mjs",
    "cjs",
    "ts",
    "tsx",
    "py",
    "pyi",
    "rs",
    "go",
    "java",
    "kt",
    "kts",
    "c",
    "h",
    "cc",
    "cpp",
    "hpp",
    "cs",
    "php",
    "rb",
    "sh",
    "bash",
    "ps1",
    "bat",
    "cmd",
];
/// Text files that conventionally have no extension.
const TEXT_FILE_NAMES: &[&str] = &[
    "readme",
    "license",
    "licence",
    "notice",
    "makefile",
    "dockerfile",
    ".gitignore",
    ".editorconfig",
    ".env",
];

#[derive(Clone)]
pub struct ContentFilter {
    matcher: RegexMatcher,
    searcher: Searcher,
}

impl ContentFilter {
    pub fn new(text: &str) -> Result<Self, grep_regex::Error> {
        // Unanchored: content patterns match substrings within lines, not whole files.
        let pattern = glob::translate(text);
        Ok(Self {
            matcher: RegexMatcherBuilder::new()
                .case_insensitive(true)
                .crlf(true)
                .build(&pattern)?,
            searcher: SearcherBuilder::new()
                .line_terminator(LineTerminator::crlf())
                .binary_detection(BinaryDetection::quit(b'\0'))
                .heap_limit(Some(MAX_LINE_BYTES))
                .build(),
        })
    }

    pub fn matches(&mut self, row: &Candidate, cancel: &CancelFlag) -> bool {
        if row.kind != Kind::File || cancel.is_set() || !is_text_file(row) {
            return false;
        }
        if !is_locally_available(&row.path_text) {
            return false;
        }
        let Ok(file) = File::open(&row.path_text) else {
            return false;
        };
        let Ok(metadata) = file.metadata() else {
            return false;
        };
        if !metadata.is_file() || metadata.len() > MAX_FILE_BYTES {
            return false;
        }
        let reader = ContentReader {
            file: file.take(MAX_FILE_BYTES),
            cancel,
        };
        let mut sink = FirstMatch::default();
        let searched = self
            .searcher
            .search_reader(&self.matcher, reader, &mut sink);
        searched.is_ok() && sink.found && !cancel.is_set()
    }
}

fn is_text_file(row: &Candidate) -> bool {
    let file_name = row.file_name().to_ascii_lowercase();
    TEXT_EXTENSIONS.contains(&row.extension.as_str())
        || TEXT_FILE_NAMES.contains(&file_name.as_str())
}

/// Checks current attributes, not the catalog's: placeholders change after indexing.
#[cfg(windows)]
fn is_locally_available(path: &str) -> bool {
    use std::os::windows::fs::MetadataExt;
    // Reading attributes never hydrates a placeholder; opening it may.
    std::fs::metadata(path)
        .is_ok_and(|metadata| metadata.file_attributes() & NOT_LOCALLY_AVAILABLE == 0)
}

#[cfg(not(windows))]
fn is_locally_available(_path: &str) -> bool {
    true
}

/// Stops at the first matching line; bytes are never decoded, so any encoding works.
#[derive(Default)]
struct FirstMatch {
    found: bool,
}

impl Sink for FirstMatch {
    type Error = io::Error;

    fn matched(&mut self, _searcher: &Searcher, _match: &SinkMatch<'_>) -> io::Result<bool> {
        self.found = true;
        Ok(false)
    }
}

struct ContentReader<'a> {
    file: Take<File>,
    cancel: &'a CancelFlag,
}

impl Read for ContentReader<'_> {
    fn read(&mut self, buffer: &mut [u8]) -> io::Result<usize> {
        if self.cancel.is_set() {
            return Ok(0);
        }
        let length = buffer.len().min(READ_CHUNK_BYTES);
        self.file.read(&mut buffer[..length])
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn found(filter: &mut ContentFilter, bytes: &[u8]) -> bool {
        let mut sink = FirstMatch::default();
        filter
            .searcher
            .search_reader(&filter.matcher, bytes, &mut sink)
            .unwrap();
        sink.found
    }

    #[test]
    fn content_patterns_match_substrings_within_lines_case_insensitively() {
        for (pattern, text, expected) in [
            ("API_KEY=(ok)+.{}", "before\napi_key=(ok)+.{}\nafter", true),
            ("API_KEY=(ok)+.{}", "API_KEY=okkkkk", false),
            ("gsk_*", "prefix GSK_abcd suffix", true),
            ("gsk_*", "prefix gsk suffix", false),
            ("gsk_?", "gsk_Ä", true),
            ("gsk_?", "gsk_", false),
            ("gsk_[0-9]", "prefix gsk_7 suffix", true),
            ("gsk_[0-9]", "prefix gsk_x suffix", false),
            ("gsk_[!0-9]", "gsk_ä", true),
            ("gsk_[!0-9]", "gsk_7", false),
            ("API_KEY=[[]ok][*]", "api_key=[ok]*", true),
            ("start*end", "start\nend", false),
            ("start*end", "start\r\nend", false),
            ("gsk_?", "gsk_\r\nend", false),
            ("[!x]", "\n", false),
            ("[z-a]", "z", false),
            ("x[ab[]", "see X[ here", true),
            ("[!]-a]", "-", true),
            (r"\z", r"value=\z", true),
        ] {
            let mut filter = ContentFilter::new(pattern).unwrap();
            assert_eq!(
                found(&mut filter, text.as_bytes()),
                expected,
                "{pattern:?} in {text:?}"
            );
        }
    }

    #[test]
    fn legacy_encodings_are_searched_without_decoding() {
        let mut filter = ContentFilter::new("invoice").unwrap();
        // "Grüße invoice" in Windows-1252: 0xFC and 0xDF are not valid UTF-8.
        assert!(found(&mut filter, b"Gr\xfc\xdfe invoice\r\n"));
        assert!(!found(&mut filter, b"Gr\xfc\xdfe receipt\r\n"));
    }
}
