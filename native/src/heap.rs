//! Sort keys and the bounded best-hits heap, as Python's `ResultOrder` and `_BestHits`.

use std::cmp::Ordering;
use std::collections::BinaryHeap;

use crate::priority::Decision;
use crate::query::{Candidate, Kind, Query, SortOrder};
use crate::rank::{self, Band, Ranking};
use crate::unicode::casefold;

#[derive(Debug, PartialEq)]
enum Part {
    Number(f64),
    Text(String),
}

impl PartialOrd for Part {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

impl Eq for Part {}

impl Ord for Part {
    fn cmp(&self, other: &Self) -> Ordering {
        match (self, other) {
            (Self::Number(a), Self::Number(b)) => a.partial_cmp(b).expect("finite sort key"),
            (Self::Text(a), Self::Text(b)) => a.cmp(b),
            _ => unreachable!("a query always uses the same key layout"),
        }
    }
}

/// Compared lexicographically; smaller keys rank first. A shorter key that equals
/// the start of a longer one sorts first, which the score bound relies on.
#[derive(Debug, Eq, PartialEq, Ord, PartialOrd)]
pub(crate) struct Key(Vec<Part>);

fn flag(value: bool) -> Part {
    Part::Number(if value { 1.0 } else { 0.0 })
}

fn is_direct_entry(query: &Query, row: &Candidate) -> bool {
    let Some(scope) = &query.scope_key else {
        return true;
    };
    let identity = row.identity.strip_suffix('/').unwrap_or(&row.identity);
    let parent = identity.rsplit_once('/').map_or("", |(parent, _)| parent);
    scope.strip_suffix('/') == Some(parent)
}

/// Explorer lists folders first; then the priority band and direct entries.
fn groups(query: &Query, row: &Candidate, band: Band) -> Vec<Part> {
    let mut parts = Vec::with_capacity(6);
    if query.explorer {
        parts.push(flag(row.kind != Kind::Folder));
    }
    parts.push(Part::Number(f64::from(band as u8)));
    parts.push(flag(!is_direct_entry(query, row)));
    parts
}

fn unscored_key(query: &Query, row: &Candidate, band: Band) -> Key {
    let mut parts = Vec::with_capacity(4);
    if query.explorer {
        parts.push(flag(row.kind != Kind::Folder));
    }
    parts.push(flag(band == Band::Fallback));
    if query.sort == SortOrder::Name {
        parts.push(Part::Text(casefold(row.name())));
    } else {
        parts.push(Part::Number(-row.modified));
    }
    parts.push(Part::Text(row.path_text.clone()));
    Key(parts)
}

pub(crate) fn key(query: &Query, row: &Candidate, ranking: &Ranking) -> Key {
    if query.sort != SortOrder::Score {
        return unscored_key(query, row, ranking.band);
    }
    let mut parts = groups(query, row, ranking.band);
    parts.push(Part::Number(-ranking.score));
    parts.push(Part::Text(casefold(row.name())));
    parts.push(Part::Text(row.path_text.clone()));
    Key(parts)
}

/// A key no worse than the candidate's real key, computed without matching text.
pub(crate) fn best_possible_key(query: &Query, row: &Candidate, priority: &Decision) -> Key {
    let band = rank::band(query, priority);
    if query.sort != SortOrder::Score {
        return unscored_key(query, row, band);
    }
    let mut parts = groups(query, row, band);
    parts.push(Part::Number(-rank::bound(query, row, priority)));
    Key(parts)
}

#[derive(Debug)]
pub(crate) struct Kept {
    pub key: Key,
    pub index: usize,
    pub row: Candidate,
    pub ranking: Ranking,
}

impl PartialEq for Kept {
    fn eq(&self, other: &Self) -> bool {
        self.key == other.key
    }
}
impl Eq for Kept {}
impl PartialOrd for Kept {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}
impl Ord for Kept {
    fn cmp(&self, other: &Self) -> Ordering {
        self.key.cmp(&other.key)
    }
}

pub(crate) struct BestHits {
    limit: usize,
    heap: BinaryHeap<Kept>,
}

impl BestHits {
    pub fn new(limit: usize) -> Self {
        Self {
            limit,
            heap: BinaryHeap::new(),
        }
    }
    pub fn could_keep(&self, key: &Key) -> bool {
        self.heap.len() < self.limit || *key <= self.heap.peek().unwrap().key
    }
    pub fn would_keep(&self, key: &Key) -> bool {
        self.heap.len() < self.limit || *key < self.heap.peek().unwrap().key
    }
    pub fn add(&mut self, kept: Kept) {
        if self.heap.len() == self.limit {
            self.heap.pop();
        }
        self.heap.push(kept);
    }
    pub fn ordered(self) -> Vec<Kept> {
        self.heap.into_sorted_vec()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn bound_prefix_sorts_before_a_full_key_and_unicode_uses_codepoint_order() {
        let prefix = Key(vec![
            Part::Number(1.0),
            Part::Number(0.0),
            Part::Number(-150.0),
        ]);
        let full = Key(vec![
            Part::Number(1.0),
            Part::Number(0.0),
            Part::Number(-150.0),
            Part::Text("a".into()),
        ]);
        assert!(prefix < full);
        assert!(Part::Text("z".into()) < Part::Text("ä".into()));
        assert_eq!(Part::Number(-0.0), Part::Number(0.0));
    }

    #[test]
    fn heap_replaces_only_strictly_worse_hits_and_returns_best_first() {
        let mut hits = BestHits::new(2);
        for score in [1.0, 3.0, 2.0, 2.0, 0.0] {
            let key = Key(vec![Part::Number(-score)]);
            if !hits.would_keep(&key) {
                continue;
            }
            let row = Candidate {
                path_text: "C:/test.txt".into(),
                identity: "C:/TEST.TXT/".into(),
                kind: Kind::File,
                search_name: "test.txt".into(),
                search_path: "c:/test.txt".into(),
                extension: "txt".into(),
                size: 0,
                modified: 0.0,
                attributes: 0,
                launches: 0,
                last_used: 0.0,
                query_launches: 0,
                pinned: false,
                application_source: false,
            };
            hits.add(Kept {
                key,
                index: 0,
                row,
                ranking: Ranking {
                    score,
                    band: Band::Normal,
                    reasons: vec![],
                },
            });
        }
        assert!(!hits.would_keep(&Key(vec![Part::Number(-2.0)])));
        assert!(hits.could_keep(&Key(vec![Part::Number(-2.0)])));
        assert_eq!(
            hits.ordered()
                .iter()
                .map(|hit| hit.ranking.score)
                .collect::<Vec<_>>(),
            [3.0, 2.0]
        );
    }
}
