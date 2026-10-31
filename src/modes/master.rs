//! Monkeytype "master" difficulty: random words, the first wrong key ends the
//! drill. A failure spawns a practice drill of the missed word, then a fresh
//! master drill, so the stream never needs a menu.

use std::sync::Arc;

use serde_json::{Value, json};

use crate::mode::{Followup, Mode, Strictness, Outcome, Target, TargetKind};
use crate::modes::practice::Practice;
use crate::words::WordList;

pub struct Master {
    list: Arc<WordList>,
    words: usize,
}

impl Master {
    pub fn new(list: Arc<WordList>, words: usize) -> Master {
        Master { list, words }
    }

    fn fresh(&self) -> Box<dyn Mode> {
        Box::new(Master::new(self.list.clone(), self.words))
    }
}

impl Mode for Master {
    fn name(&self) -> &'static str {
        "master"
    }

    fn title(&self) -> String {
        format!("master · {} · {} words", self.list.name, self.words)
    }

    fn config(&self) -> Value {
        json!({ "list": self.list.name, "keep_case": self.list.keep_case, "words": self.words })
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
        Strictness::Master
    }

    fn after(&self, outcome: &Outcome) -> Vec<Followup> {
        match outcome {
            Outcome::Completed { .. } | Outcome::Afk => vec![Followup { mode: self.fresh(), child: false }],
            Outcome::Failed { target, .. } => vec![
                Followup { mode: Box::new(Practice::new(target.text.clone(), target.source_ref.clone())), child: true },
                Followup { mode: self.fresh(), child: false },
            ],
            Outcome::Aborted => vec![],
        }
    }
}
