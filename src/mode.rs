//! The contract between the kernel and a mode.
//!
//! A mode decides *what* to type and *how strict* to be. The kernel owns the
//! terminal, the clock, and the database, and never asks a mode about any of
//! those. Adding a mode means implementing [`Mode`] and nothing else.

use serde_json::Value;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum TargetKind {
    /// A single word. The user must type it followed by a space.
    Word,
    /// A whole sentence shown on its own. Done when the last character is correct.
    Sentence,
}

impl TargetKind {
    pub fn as_str(self) -> &'static str {
        match self {
            TargetKind::Word => "word",
            TargetKind::Sentence => "sentence",
        }
    }
}

#[derive(Clone, Debug)]
pub struct Target {
    pub text: String,
    pub kind: TargetKind,
    /// Where this came from, e.g. `english_10k#412` or `pride#1037`.
    pub source_ref: Option<String>,
}

impl Target {
    /// The exact key sequence that completes this target.
    pub fn expected(&self) -> Vec<char> {
        let mut v: Vec<char> = self.text.chars().collect();
        if self.kind == TargetKind::Word {
            v.push(' ');
        }
        v
    }
}

/// How the kernel treats mistakes: Monkeytype's "normal" and "master"
/// difficulties. The kernel implements both, a mode picks one.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Strictness {
    /// Wrong keys show in red; a space moves on to the next word whether or
    /// not the word was right. Missed words are reported to the mode in the
    /// outcome, which decides what to do about them.
    Lenient,
    /// The first wrong key ends the drill.
    Master,
}

/// What happened on one target, reported to the mode when a drill completes.
#[derive(Clone, Debug, Default)]
pub struct TargetResult {
    /// Character keystrokes on the target, and how many the kernel accepted.
    pub chars: usize,
    pub correct: usize,
    /// Words submitted wrong (lenient strictness only), in order.
    pub wrong_words: Vec<String>,
}

impl TargetResult {
    /// Percent of character keys accepted; 100 when nothing was typed.
    pub fn accuracy(&self) -> f64 {
        if self.chars == 0 { 100.0 } else { 100.0 * self.correct as f64 / self.chars as f64 }
    }
}

#[derive(Clone, Debug)]
pub enum Outcome {
    /// One result per target, in order, and the drill's words per minute
    /// (Monkeytype's: characters of the words that ended up right, the space
    /// after each included, / 5, over the time from first to last key).
    Completed { results: Vec<TargetResult>, wpm: f64 },
    /// `word` is the word that caused the failure: the target itself for a
    /// word target, the offending word for a sentence.
    Failed { target: Target, word: String },
    /// A long pause mid-drill; the drill is void and its mode restarts.
    Afk,
    Aborted,
}

impl Outcome {
    pub fn as_str(&self) -> &'static str {
        match self {
            Outcome::Completed { .. } => "completed",
            Outcome::Failed { .. } => "failed",
            Outcome::Afk => "afk",
            Outcome::Aborted => "aborted",
        }
    }
}

/// A drill to run after the current one ends.
pub struct Followup {
    pub mode: Box<dyn Mode>,
    /// Record the drill that just ended as this one's parent (e.g. a practice
    /// drill spawned by a failure). A fresh unrelated drill says `false`.
    pub child: bool,
}

pub trait Mode {
    /// Stored in `drills.mode`.
    fn name(&self) -> &'static str;
    /// One line for the screen header.
    fn title(&self) -> String;
    /// Mode-specific settings, stored as JSON in `drills.config`.
    fn config(&self) -> Value;
    /// Everything the drill will ask for, in order. Called once per drill.
    fn targets(&mut self) -> Vec<Target>;
    fn strictness(&self) -> Strictness;
    /// Drills to push to the front of the queue once this one ends.
    fn after(&self, outcome: &Outcome) -> Vec<Followup>;
}
