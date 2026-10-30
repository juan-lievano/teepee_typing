//! One word, repeated until at least `chars` characters of it have been
//! asked for (so a long word gets few reps and a short one many). Same fail
//! rule as master; a failure restarts the drill.

use serde_json::{Value, json};

use crate::mode::{Followup, Mode, Strictness, Outcome, Target, TargetKind};

/// How much of a missed word to practise, in characters. The word is
/// repeated until at least this many of its letters have been asked for.
pub const PRACTICE_CHARS: usize = 50;

pub struct Practice {
    word: String,
    reps: usize,
}

impl Practice {
    pub fn new(word: String) -> Practice {
        let len = word.chars().count().max(1);
        let reps = PRACTICE_CHARS.div_ceil(len).max(1);
        Practice { word, reps }
    }
}

impl Mode for Practice {
    fn name(&self) -> &'static str {
        "practice"
    }

    fn title(&self) -> String {
        format!("practice · {} ×{}", self.word, self.reps)
    }

    fn config(&self) -> Value {
        json!({ "word": self.word, "reps": self.reps })
    }

    fn targets(&mut self) -> Vec<Target> {
        (0..self.reps)
            .map(|_| Target { text: self.word.clone(), kind: TargetKind::Word, source_ref: None })
            .collect()
    }

    fn strictness(&self) -> Strictness {
        Strictness::Master
    }

    fn after(&self, outcome: &Outcome) -> Vec<Followup> {
        match outcome {
            Outcome::Failed { .. } | Outcome::Afk { .. } => vec![Followup {
                mode: Box::new(Practice::new(self.word.clone())),
                child: true,
            }],
            Outcome::Completed | Outcome::Aborted => vec![],
        }
    }
}
