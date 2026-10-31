//! One word, repeated until at least `PRACTICE_CHARS` keystrokes of it have been
//! asked for (so a long word gets few reps and a short one more), never more
//! than `MAX_REPS`. Same fail rule as master; a failure restarts the drill.

use serde_json::{Value, json};

use crate::mode::{Followup, Mode, Strictness, Outcome, Target, TargetKind};

/// How much of a missed word to practise, in characters. The word is
/// repeated until at least this many of its letters have been asked for.
pub const PRACTICE_CHARS: usize = 25;
/// Reps are capped here so a short word ("to") is not asked for 25 times.
pub const MAX_REPS: usize = 10;

pub struct Practice {
    word: String,
    reps: usize,
    /// Source ref of the target the word was missed in (`english_10k#412`,
    /// or the sentence `quijote#88`), so practice keystrokes keep their
    /// language and origin.
    source_ref: Option<String>,
}

impl Practice {
    pub fn new(word: String, source_ref: Option<String>) -> Practice {
        // Keystrokes, not characters: an accented letter is a dead key plus
        // a vowel, so "más" costs four keys and gets fewer reps than "mas".
        let keys: usize = word.chars().map(|c| if c.is_ascii() { 1 } else { 2 }).sum::<usize>().max(1);
        let reps = PRACTICE_CHARS.div_ceil(keys).clamp(1, MAX_REPS);
        Practice { word, reps, source_ref }
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
            .map(|_| Target { text: self.word.clone(), kind: TargetKind::Word, source_ref: self.source_ref.clone() })
            .collect()
    }

    fn strictness(&self) -> Strictness {
        Strictness::Master
    }

    fn after(&self, outcome: &Outcome) -> Vec<Followup> {
        match outcome {
            Outcome::Failed { .. } | Outcome::Afk => vec![Followup {
                mode: Box::new(Practice::new(self.word.clone(), self.source_ref.clone())),
                child: true,
            }],
            Outcome::Completed { .. } | Outcome::Aborted => vec![],
        }
    }
}
