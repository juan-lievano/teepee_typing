//! A book, one sentence per drill. The sentence is shown alone; think time
//! (shown → first key) and typing time are both recoverable from the data.
//! Wrong words are let through (lenient strictness); when the sentence is
//! done each word that had a wrong key in it is practised, then the sentence
//! is repeated if its accuracy was below `MIN_ACCURACY`, else the book moves on.

use std::collections::HashSet;
use std::sync::Arc;

use serde_json::{Value, json};

use crate::mode::{Followup, Mode, Outcome, Strictness, Target, TargetKind};
use crate::modes::practice::Practice;

/// A sentence typed below this accuracy (percent of character keys accepted)
/// is repeated after its wrong words have been practised.
pub const MIN_ACCURACY: f64 = 95.0;

pub struct Literature {
    book: String,
    sentences: Arc<Vec<String>>,
    /// Index of the sentence this drill shows.
    start: usize,
}

impl Literature {
    pub fn new(book: String, sentences: Arc<Vec<String>>, start: usize) -> Literature {
        Literature { book, sentences, start }
    }

    fn from(&self, start: usize) -> Box<dyn Mode> {
        Box::new(Literature::new(self.book.clone(), self.sentences.clone(), start))
    }
}

impl Mode for Literature {
    fn name(&self) -> &'static str {
        "literature"
    }

    fn title(&self) -> String {
        format!("{} · line {} of {}", self.book, self.start + 1, self.sentences.len())
    }

    fn config(&self) -> Value {
        json!({ "book": self.book, "start": self.start, "min_accuracy": MIN_ACCURACY })
    }

    fn targets(&mut self) -> Vec<Target> {
        vec![Target {
            text: self.sentences[self.start].clone(),
            kind: TargetKind::Sentence,
            source_ref: Some(format!("{}#{}", self.book, self.start)),
        }]
    }

    fn strictness(&self) -> Strictness {
        Strictness::Lenient
    }

    fn after(&self, outcome: &Outcome) -> Vec<Followup> {
        match outcome {
            // Practise every missed word (once each), then repeat the
            // sentence if it fell below the accuracy bar, else go on.
            Outcome::Completed { results, .. } => {
                let mut out = Vec::new();
                let mut seen = HashSet::new();
                for r in results {
                    for w in &r.wrong_words {
                        if seen.insert(w.clone()) {
                            let source_ref = Some(format!("{}#{}", self.book, self.start));
                            out.push(Followup { mode: Box::new(Practice::new(w.clone(), source_ref)), child: true });
                        }
                    }
                }
                let repeat = results.iter().any(|r| r.accuracy() < MIN_ACCURACY);
                let next = if repeat { self.start } else { self.start + 1 };
                if next < self.sentences.len() {
                    out.push(Followup { mode: self.from(next), child: false });
                }
                out
            }
            // Pause mid-sentence: redo it, nothing to practise.
            Outcome::Afk => vec![Followup { mode: self.from(self.start), child: false }],
            Outcome::Failed { .. } | Outcome::Aborted => vec![],
        }
    }
}
