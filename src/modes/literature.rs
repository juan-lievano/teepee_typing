//! A book, one sentence at a time. The sentence is shown alone; think time
//! (shown → first key) and typing time are both recoverable from the data.
//! Strictness is a parameter: under `expert` (the default) a wrong word
//! spawns a practice drill and the sentence starts over.

use std::sync::Arc;

use serde_json::{Value, json};

use crate::mode::{Followup, Mode, Outcome, Strictness, Target, TargetKind};
use crate::modes::practice::Practice;

pub struct Literature {
    book: String,
    sentences: Arc<Vec<String>>,
    /// Index of the first sentence this drill will show.
    start: usize,
    per_drill: usize,
    strictness: Strictness,
}

impl Literature {
    pub fn new(
        book: String,
        sentences: Arc<Vec<String>>,
        start: usize,
        per_drill: usize,
        strictness: Strictness,
    ) -> Literature {
        Literature { book, sentences, start, per_drill, strictness }
    }

    fn end(&self) -> usize {
        (self.start + self.per_drill).min(self.sentences.len())
    }

    fn from(&self, start: usize) -> Box<dyn Mode> {
        Box::new(Literature::new(
            self.book.clone(),
            self.sentences.clone(),
            start,
            self.per_drill,
            self.strictness,
        ))
    }
}

impl Mode for Literature {
    fn name(&self) -> &'static str {
        "literature"
    }

    fn title(&self) -> String {
        format!(
            "{} · {} · sentences {}–{} of {}",
            self.book,
            self.strictness.as_str(),
            self.start + 1,
            self.end(),
            self.sentences.len()
        )
    }

    fn config(&self) -> Value {
        json!({
            "book": self.book,
            "start": self.start,
            "per_drill": self.per_drill,
            "strictness": self.strictness.as_str(),
        })
    }

    fn targets(&mut self) -> Vec<Target> {
        (self.start..self.end())
            .map(|i| Target {
                text: self.sentences[i].clone(),
                kind: TargetKind::Sentence,
                source_ref: Some(format!("{}#{}", self.book, i)),
            })
            .collect()
    }

    fn strictness(&self) -> Strictness {
        self.strictness
    }

    fn after(&self, outcome: &Outcome) -> Vec<Followup> {
        match outcome {
            Outcome::Completed if self.end() < self.sentences.len() => {
                vec![Followup { mode: self.from(self.end()), child: false }]
            }
            // Practice the word, then pick the book up at the failed sentence.
            Outcome::Failed { target_idx, word, .. } => vec![
                Followup { mode: Box::new(Practice::new(word.clone())), child: true },
                Followup { mode: self.from(self.start + target_idx), child: false },
            ],
            // Pause mid-sentence: redo that sentence, nothing to practise.
            Outcome::Afk { target_idx } => {
                vec![Followup { mode: self.from(self.start + target_idx), child: false }]
            }
            _ => vec![],
        }
    }
}
