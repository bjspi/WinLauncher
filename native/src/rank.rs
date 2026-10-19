use crate::matching::{
    best_possible_score, match_term, matches_folder_order, parent_parts, NameForms,
};
use crate::priority::Decision;
use crate::query::{Candidate, Query};

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
const BOUND_TOLERANCE: f64 = 1e-6;

#[derive(Debug)]
pub(crate) struct Ranking {
    pub score: f64,
    pub band: u8,
    pub reasons: Vec<String>,
}

pub(crate) fn band(query: &Query, priority: &Decision) -> u8 {
    match priority.search.as_str() {
        "low" => 2,
        "high" if query.combined => 0,
        _ => 1,
    }
}

fn personalization(row: &Candidate, now: f64, reasons: &mut Vec<String>) -> f64 {
    let mut points = if row.kind == "app" { APP_BONUS } else { 0.0 };
    if !(row.pinned || row.launches != 0 || row.query_launches != 0 || row.last_used != 0.0) {
        return points;
    }
    if row.pinned {
        points += FAVORITE_BONUS;
        reasons.push("Favorite".into());
    }
    points +=
        ((row.launches as f64 + 1.0).log2() * LAUNCH_BONUS_PER_DOUBLING).min(LAUNCH_BONUS_CAP);
    points += ((row.query_launches as f64 + 1.0).log2() * QUERY_LAUNCH_BONUS_PER_DOUBLING)
        .min(QUERY_LAUNCH_BONUS_CAP);
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
    if priority.search == "high" {
        points += HIGH_PRIORITY_BONUS;
        reasons.push("High search priority".into());
    }
    points
}

fn sum_parts(parts: [f64; 3]) -> f64 {
    // Python 3.12 sum uses compensated addition; preserve exact score ties.
    let (mut high, mut low) = (0.0_f64, 0.0_f64);
    for value in parts {
        let total = high + value;
        low += if high.abs() >= value.abs() {
            (high - total) + value
        } else {
            (value - total) + high
        };
        high = total;
    }
    high + low
}

pub(crate) fn bound(query: &Query, row: &Candidate, priority: &Decision) -> f64 {
    let mut score: f64 = query
        .terms
        .iter()
        .map(|term| best_possible_score(&term.text, &row.search_name))
        .sum();
    if query.combined {
        score += personalization(row, query.now, &mut Vec::new());
        score += configured_priority(priority, &mut Vec::new());
    }
    score + BOUND_TOLERANCE
}

pub(crate) fn rank(query: &Query, row: &Candidate, priority: &Decision) -> Option<Ranking> {
    if !query.folder_parts.is_empty()
        && !matches_folder_order(
            &query.folder_parts,
            &parent_parts(&row.path_text),
            query.fuzzy,
        )
    {
        return None;
    }
    let (original, normalized, path) = if query.basename_only {
        (row.stem(), None, None)
    } else {
        (
            row.name(),
            Some(row.search_name.as_str()),
            Some(row.search_path.as_str()),
        )
    };
    let name = NameForms::new(original, normalized);
    let mut reasons = Vec::new();
    let mut score = 0.0;
    for term in &query.terms {
        let (points, reason) = match_term(term, &name, path, query.fuzzy)?;
        score += points;
        reasons.push(reason.into());
    }
    if !query.folder_parts.is_empty() {
        reasons.push("Ordered folder path".into());
    }
    if query.combined {
        let personal = personalization(row, query.now, &mut reasons);
        let configured = configured_priority(priority, &mut reasons);
        score = sum_parts([score, personal, configured]);
    } else {
        reasons = vec!["Text relevance only".into()];
    }
    Some(Ranking {
        score,
        band: band(query, priority),
        reasons,
    })
}

#[cfg(test)]
mod tests {
    #[test]
    fn score_part_sum_keeps_python_312_compensation() {
        assert_eq!(super::sum_parts([1e16, 1.0, -1e16]), 1.0);
    }
}
