use std::sync::{Arc, OnceLock};

use serde::Deserialize;

static ENGLISH_10K: &str = include_str!("../assets/english_10k.json");

#[derive(Deserialize)]
struct Raw {
    words: Vec<String>,
}

pub struct WordList {
    pub name: &'static str,
    pub words: Vec<String>,
}

impl WordList {
    /// A uniformly random word and its rank (0 = most frequent).
    pub fn random(&self) -> (usize, &str) {
        let i = rand::random_range(0..self.words.len());
        (i, &self.words[i])
    }
}

pub fn english_10k() -> Arc<WordList> {
    static LIST: OnceLock<Arc<WordList>> = OnceLock::new();
    LIST.get_or_init(|| {
        let raw: Raw = serde_json::from_str(ENGLISH_10K).expect("vendored word list is valid JSON");
        Arc::new(WordList { name: "english_10k", words: raw.words })
    })
    .clone()
}
