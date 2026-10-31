//! Monkeytype "words" mode at normal difficulty: random words, wrong keys
//! are let through and nothing is practised. For going after a personal
//! best; a fresh drill follows each one.

use std::sync::Arc;

use serde_json::{Value, json};

use crate::mode::{Followup, Mode, Outcome, Strictness, Target, TargetKind};
use crate::words::WordList;

pub struct Sprint {
    list: Arc<WordList>,
    words: usize,
}

impl Sprint {
    pub fn new(list: Arc<WordList>, words: usize) -> Sprint {
        Sprint { list, words }
    }
}

impl Mode for Sprint {
    fn name(&self) -> &'static str {
        "sprint"
    }

    fn title(&self) -> String {
        format!("sprint · {} · {} words", self.list.name, self.words)
    }

    fn config(&self) -> Value {
        json!({ "list": self.list.name, "words": self.words })
    }

    fn targets(&mut self) -> Vec<Target> {
        (0..self.words)
            .map(|_| {
                let (rank, w) = self.list.random();
                Target {
                    text: w.to_string(),
                    kind: TargetKind::Word,
                    source_ref: Some(format!("{}#{}", self.list.name, rank)),
                }
            })
            .collect()
    }

    fn strictness(&self) -> Strictness {
        Strictness::Lenient
    }

    fn after(&self, outcome: &Outcome) -> Vec<Followup> {
        match outcome {
            Outcome::Aborted => vec![],
            _ => vec![Followup { mode: Box::new(Sprint::new(self.list.clone(), self.words)), child: false }],
        }
    }
}
