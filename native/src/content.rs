//! Streaming literal content checks for the candidates that survived filename ranking.

use std::fs::File;
use std::io::{self, Read, Take};

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

pub struct ContentFilter {
    matcher: RegexMatcher,
    searcher: Searcher,
}

impl ContentFilter {
    pub fn new(text: &str) -> Result<Self, grep_regex::Error> {
        Ok(Self {
            matcher: RegexMatcherBuilder::new()
                .fixed_strings(true)
                .case_insensitive(true)
                .build(text)?,
            searcher: SearcherBuilder::new()
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
    fn literal_text_matches_case_insensitively_in_a_stream() {
        let mut filter = ContentFilter::new("API_KEY=[ok]*").unwrap();
        let mut found = false;
        filter
            .searcher
            .search_reader(
                &filter.matcher,
                &b"api_key=[ok]*"[..],
                sinks::UTF8(|_, _| {
                    found = true;
                    Ok(false)
                }),
            )
            .unwrap();
        assert!(found);
    }
}
