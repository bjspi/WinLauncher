//! Streaming fnmatch content checks for the candidates that survived filename ranking.

use std::fs::File;
use std::io::{self, Read, Take};

use grep_matcher::LineTerminator;
use grep_regex::{RegexMatcher, RegexMatcherBuilder};
use grep_searcher::{sinks, BinaryDetection, Searcher, SearcherBuilder};

use crate::query::Candidate;
use crate::CancelFlag;

// shortcut: skip files over 64 MiB; revisit with explicit large-file search if needed.
const MAX_BYTES: u64 = 64 * 1024 * 1024;
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

#[derive(Clone)]
pub struct ContentFilter {
    matcher: RegexMatcher,
    searcher: Searcher,
}

impl ContentFilter {
    pub fn new(text: &str) -> Result<Self, grep_regex::Error> {
        let pattern = fnmatch_rs::translate(text);
        // Content patterns match substrings within lines, not a whole filename or stream.
        let pattern = pattern
            .strip_prefix(r"(?s)\A")
            .and_then(|body| body.strip_suffix(r"\z"))
            .expect("fnmatch translation has documented whole-string anchors");
        Ok(Self {
            matcher: RegexMatcherBuilder::new()
                .case_insensitive(true)
                .crlf(true)
                .build(pattern)?,
            searcher: SearcherBuilder::new()
                .line_terminator(LineTerminator::crlf())
                .binary_detection(BinaryDetection::quit(b'\0'))
                .heap_limit(Some((MAX_BYTES * 2 + 65536) as usize))
                .line_number(true)
                .build(),
        })
    }

    pub fn matches(&mut self, row: &Candidate, cancel: &CancelFlag) -> bool {
        if row.kind != "file" || row.attributes & 4096 != 0 || cancel.is_set() {
            return false;
        }
        let filename = row.file_name().to_ascii_lowercase();
        if !TEXT_EXTENSIONS.contains(&row.extension.as_str())
            && ![
                "readme",
                "license",
                "licence",
                "notice",
                "makefile",
                "dockerfile",
                ".gitignore",
                ".editorconfig",
                ".env",
            ]
            .contains(&filename.as_str())
        {
            return false;
        }
        let Ok(file) = File::open(&row.path_text) else {
            return false;
        };
        let Ok(metadata) = file.metadata() else {
            return false;
        };
        if !metadata.is_file() || metadata.len() > MAX_BYTES {
            return false;
        }
        let reader = ContentReader {
            file: file.take(MAX_BYTES),
            cancel,
        };
        let mut found = false;
        let searched = self.searcher.search_reader(
            &self.matcher,
            reader,
            sinks::UTF8(|_, _| {
                found = true;
                Ok(false)
            }),
        );
        searched.is_ok() && found && !cancel.is_set()
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
        let length = buffer.len().min(65536);
        self.file.read(&mut buffer[..length])
    }
}

#[cfg(test)]
mod tests {
    use super::*;

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
            (r"\z", r"value=\z", true),
        ] {
            let mut filter = ContentFilter::new(pattern).unwrap();
            let mut found = false;
            filter
                .searcher
                .search_reader(
                    &filter.matcher,
                    text.as_bytes(),
                    sinks::UTF8(|_, _| {
                        found = true;
                        Ok(false)
                    }),
                )
                .unwrap();
            assert_eq!(found, expected, "{pattern:?} in {text:?}");
        }
    }
}
