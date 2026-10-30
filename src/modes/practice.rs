//! One word, repeated until at least `chars` characters of it have been
//! asked for (so a long word gets few reps and a short one many). Same fail
//! rule as master; a failure restarts the drill.

use serde_json::{Value, json};

use crate::mode::{Followup, Mode, Strictness, Outcome, Target, TargetKind};

pub struct Practice {
    word: String,
    chars: usize,
    reps: usize,
}

impl Practice {
    pub fn new(word: String, chars: usize) -> Practice {
        let len = word.chars().count().max(1);
        let reps = chars.div_ceil(len).max(1);
        Practice { word, chars, reps }
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
        json!({ "word": self.word, "practice_chars": self.chars, "reps": self.reps })
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
                mode: Box::new(Practice::new(self.word.clone(), self.chars)),
                child: true,
            }],
            Outcome::Completed | Outcome::Aborted => vec![],
        }
    }
}
