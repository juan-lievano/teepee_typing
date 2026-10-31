use std::sync::Arc;

use serde::Deserialize;

/// Monkeytype's English lists, vendored (see assets/README.md).
static LISTS: &[(&str, &str, &str)] = &[
    ("200", "english_200", include_str!("../assets/english.json")),
    ("1k", "english_1k", include_str!("../assets/english_1k.json")),
    ("5k", "english_5k", include_str!("../assets/english_5k.json")),
    ("10k", "english_10k", include_str!("../assets/english_10k.json")),
];

pub const LIST_NAMES: &[&str] = &["200", "1k", "5k", "10k"];

#[derive(Deserialize)]
struct Raw {
    words: Vec<String>,
}

pub struct WordList {
    /// Stored in configs and source refs, e.g. `english_1k`.
    pub name: &'static str,
    pub words: Vec<String>,
    /// False means every word was lowercased at load, as Monkeytype does
    /// with punctuation off.
    pub keep_case: bool,
}

impl WordList {
    /// A uniformly random word and its rank (0 = most frequent).
    pub fn random(&self) -> (usize, &str) {
        let i = rand::random_range(0..self.words.len());
        (i, &self.words[i])
    }
}

/// `english_10k` for `10k`: the name used in configs, source refs and `sources`.
pub fn full_name(short: &str) -> &'static str {
    LISTS.iter().find(|(s, _, _)| *s == short).map_or("", |(_, name, _)| name)
}

/// `short` is one of LIST_NAMES.
pub fn list(short: &str, keep_case: bool) -> Option<Arc<WordList>> {
    let (_, name, raw) = LISTS.iter().find(|(s, _, _)| *s == short)?;
    let raw: Raw = serde_json::from_str(raw).expect("vendored word list is valid JSON");
    let words = if keep_case { raw.words } else { raw.words.into_iter().map(|w| w.to_lowercase()).collect() };
    Some(Arc::new(WordList { name, words, keep_case }))
}
