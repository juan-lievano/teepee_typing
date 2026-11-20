use std::sync::Arc;

use serde::Deserialize;

/// Short name (typed after `--list`), stored name, origin, the list. The english lists are
/// Monkeytype's, vendored; the literary ones are built from classic books by `teepee-literary`
/// (see assets/README.md).
static LISTS: &[(&str, &str, &str, &str)] = &[
    ("200", "english_200", "monkeytype", include_str!("../assets/english.json")),
    ("1k", "english_1k", "monkeytype", include_str!("../assets/english_1k.json")),
    ("5k", "english_5k", "monkeytype", include_str!("../assets/english_5k.json")),
    ("10k", "english_10k", "monkeytype", include_str!("../assets/english_10k.json")),
    ("literary-200", "literary_200", "teepee-literary", include_str!("../assets/literary_200.json")),
    ("literary-1k", "literary_1k", "teepee-literary", include_str!("../assets/literary_1k.json")),
    ("literary-5k", "literary_5k", "teepee-literary", include_str!("../assets/literary_5k.json")),
    ("literary-10k", "literary_10k", "teepee-literary", include_str!("../assets/literary_10k.json")),
    ("literary-25k", "literary_25k", "teepee-literary", include_str!("../assets/literary_25k.json")),
];

pub const LIST_NAMES: &[&str] =
    &["200", "1k", "5k", "10k", "literary-200", "literary-1k", "literary-5k", "literary-10k", "literary-25k"];

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
    LISTS.iter().find(|(s, ..)| *s == short).map_or("", |(_, name, ..)| name)
}

/// Where a list came from, by its stored name: `monkeytype` or `teepee-literary`.
pub fn origin(full: &str) -> &'static str {
    LISTS.iter().find(|(_, name, ..)| *name == full).map_or("", |(_, _, origin, _)| origin)
}

/// `10k` for `english_10k`: back from a stored name to the one typed on the command line.
pub fn short_name(full: &str) -> Option<&'static str> {
    LISTS.iter().find(|(_, name, ..)| *name == full).map(|(short, ..)| *short)
}

/// `short` is one of LIST_NAMES.
pub fn list(short: &str, keep_case: bool) -> Option<Arc<WordList>> {
    let (_, name, _, raw) = LISTS.iter().find(|(s, ..)| *s == short)?;
    let raw: Raw = serde_json::from_str(raw).expect("vendored word list is valid JSON");
    let words = if keep_case { raw.words } else { raw.words.into_iter().map(|w| w.to_lowercase()).collect() };
    Some(Arc::new(WordList { name, words, keep_case }))
}
