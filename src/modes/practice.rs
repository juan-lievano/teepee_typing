//! One word, repeated. Same fail rule as master; a failure restarts the drill.

use serde_json::{Value, json};

use crate::mode::{Followup, Mode, Strictness, Outcome, Target, TargetKind};

pub struct Practice {
    word: String,
    reps: usize,
}

impl Practice {
    pub fn new(word: String, reps: usize) -> Practice {
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
                mode: Box::new(Practice::new(self.word.clone(), self.reps)),
                child: true,
            }],
            Outcome::Completed | Outcome::Aborted => vec![],
        }
    }
}
