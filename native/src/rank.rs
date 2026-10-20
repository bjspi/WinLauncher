//! Text relevance, personalization and configured priority, as Python's `Ranker`.

use crate::matching::{
    best_possible_score, match_term, matches_folder_order, parent_parts, NameForms,
};
use crate::priority::{Decision, SearchClass};
use crate::query::{Candidate, Kind, Query};

const APP_BONUS: f64 = 12.0;
const FAVORITE_BONUS: f64 = 80.0;
const HIGH_PRIORITY_BONUS: f64 = 20.0;
const LAUNCH_BONUS_PER_DOUBLING: f64 = 4.0;
const LAUNCH_BONUS_CAP: f64 = 24.0;
const QUERY_LAUNCH_BONUS_PER_DOUBLING: f64 = 7.0;
const QUERY_LAUNCH_BONUS_CAP: f64 = 35.0;
const RECENCY_BONUS: f64 = 12.0;
const RECENCY_HALF_VALUE_DAYS: f64 = 7.0;
const SECONDS_PER_DAY: f64 = 86400.0;
/// Absorbs float rounding, so an upper bound is never a hair below the real score.
const BOUND_TOLERANCE: f64 = 1e-6;

/// Result groups sorted before score; the values are Python's `PriorityBand`.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
#[repr(u8)]
pub(crate) enum Band {
    Promoted = 0,
    Normal = 1,
    Fallback = 2,
}

#[derive(Debug)]
pub(crate) struct Ranking {
    pub score: f64,
    pub band: Band,
    pub reasons: Vec<String>,
}

pub(crate) fn band(query: &Query, priority: &Decision) -> Band {
    match priority.class {
        SearchClass::Low => Band::Fallback,
        SearchClass::High if query.combined => Band::Promoted,
        _ => Band::Normal,
    }
}

fn per_doubling(count: i64, points: f64, cap: f64) -> f64 {
    (log2(count as f64 + 1.0) * points).min(cap)
}

/// log2 exactly as the Python reference computes it: ln(mantissa) / ln 2 + exponent.
/// `f64::log2` matches only some Python versions.
fn log2(value: f64) -> f64 {
    // Counts are >= 0, so `value` >= 1 is finite and normal: mantissa * 2^exponent.
    let bits = value.to_bits();
    let exponent = ((bits >> 52) & 0x7ff) as i64 - 1023;
    let mantissa = f64::from_bits((bits & ((1 << 52) - 1)) | (1023 << 52));
    mantissa.ln() / std::f64::consts::LN_2 + exponent as f64
}

fn personalization(row: &Candidate, now: f64, reasons: &mut Vec<String>) -> f64 {
    let mut points = if row.kind == Kind::App {
        APP_BONUS
    } else {
        0.0
    };
    let has_signals =
        row.pinned || row.launches != 0 || row.query_launches != 0 || row.last_used != 0.0;
    if !has_signals {
        return points;
    }
    if row.pinned {
        points += FAVORITE_BONUS;
        reasons.push("Favorite".into());
    }
    points += per_doubling(row.launches, LAUNCH_BONUS_PER_DOUBLING, LAUNCH_BONUS_CAP);
    points += per_doubling(
        row.query_launches,
        QUERY_LAUNCH_BONUS_PER_DOUBLING,
        QUERY_LAUNCH_BONUS_CAP,
    );
    if row.query_launches != 0 {
        reasons.push("Used for this query".into());
    }
    if row.last_used != 0.0 {
        let age_days = ((now - row.last_used) / SECONDS_PER_DAY).max(0.0);
        points += RECENCY_BONUS / (1.0 + age_days / RECENCY_HALF_VALUE_DAYS);
    }
    points
}

fn configured_priority(priority: &Decision, reasons: &mut Vec<String>) -> f64 {
    let mut points = priority.boost as f64;
    if priority.boost != 0 {
        reasons.push(format!("Priority boost {:+}", priority.boost));
    }
    if priority.class == SearchClass::High {
        points += HIGH_PRIORITY_BONUS;
        reasons.push("High search priority".into());
    }
    points
}

/// An upper bound for `rank(...).score`, without matching any text.
pub(crate) fn bound(query: &Query, row: &Candidate, priority: &Decision) -> f64 {
    let mut score = 0.0;
    for term in &query.terms {
        score += best_possible_score(&term.text, &row.search_name);
    }
    if query.combined {
        score += personalization(row, query.now, &mut Vec::new());
        score += configured_priority(priority, &mut Vec::new());
    }
    score + BOUND_TOLERANCE
}

/// Rank an eligible candidate; None if a term or the folder order does not match.
pub(crate) fn rank(query: &Query, row: &Candidate, priority: &Decision) -> Option<Ranking> {
    let (text, mut reasons) = text_relevance(query, row)?;
    let score = if query.combined {
        let personal = personalization(row, query.now, &mut reasons);
        let configured = configured_priority(priority, &mut reasons);
        // Left-to-right addition, exactly as Python's `Ranker.rank`.
        text + personal + configured
    } else {
        reasons = vec!["Text relevance only".into()];
        text
    };
    Some(Ranking {
        score,
        band: band(query, priority),
        reasons,
    })
}

fn text_relevance(query: &Query, row: &Candidate) -> Option<(f64, Vec<String>)> {
    if !query.folder_parts.is_empty() {
        let folders = parent_parts(&row.path_text);
        if !matches_folder_order(&query.folder_parts, &folders, query.fuzzy) {
            return None;
        }
    }
    let (name, path) = if query.basename_only {
        (NameForms::new(row.stem(), None), None)
    } else {
        let name = NameForms::new(row.name(), Some(&*row.search_name));
        (name, Some(query.matching_path(row)))
    };
    let mut score = 0.0;
    let mut reasons = Vec::with_capacity(query.terms.len() + 1);
    for term in &query.terms {
        let fuzzy = query.fuzzy && !term.exact;
        let path = path.filter(|_| term.matches_path);
        let (points, reason) = match_term(term, &name, path, fuzzy)?;
        score += points;
        reasons.push(reason.into());
    }
    if !query.folder_parts.is_empty() {
        reasons.push("Ordered folder path".into());
    }
    Some((score, reasons))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::priority::Decision;
    use crate::query::testing::{file, query};

    const NO_PRIORITY: Decision = Decision {
        class: SearchClass::Normal,
        boost: 0,
    };

    /// The match reason of a one-term query, or None if it does not match.
    fn reason(term: &str, exact: bool, path: &str) -> Option<String> {
        let mut query = query(&[term]);
        query.terms[0].exact = exact;
        query.combined = true;
        let ranking = rank(&query, &file(path), &NO_PRIORITY)?;
        ranking.reasons.into_iter().next()
    }

    #[test]
    fn exact_terms_match_contiguous_text_but_never_initials_or_subsequences() {
        let path = "C:/Work/VisualStudioCode.exe";
        let found = |term, exact| reason(term, exact, path);
        assert_eq!(found("vsc", false).as_deref(), Some("Abbreviation"));
        assert_eq!(found("vsc", true), None);
        assert_eq!(found("vsd", false).as_deref(), Some("Fuzzy match"));
        assert_eq!(found("vsd", true), None);
        assert_eq!(
            found("sualstu", true).as_deref(),
            Some("Name contains query")
        );
        assert_eq!(found("work", true).as_deref(), Some("Path contains query"));
    }

    #[test]
    fn short_terms_match_names_but_never_parent_folders() {
        let path = "C:/Work/Do/report.txt";
        assert_eq!(reason("do", false, path), None);
        assert_eq!(reason("wo", false, path), None);
        assert_eq!(
            reason("wor", false, path).as_deref(),
            Some("Path contains query")
        );
        assert_eq!(reason("rp", false, path).as_deref(), Some("Fuzzy match"));
        assert_eq!(
            reason("r*.txt", false, path).as_deref(),
            Some("Name wildcard")
        );
    }
}
