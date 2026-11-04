//! The kernel: runs a queue of drills. Owns the terminal, the clock and the
//! store. Implements the three strictness levels once, for every mode.

use std::collections::VecDeque;
use std::sync::mpsc::Receiver;
use std::time::{Duration, Instant};

/// After a failure the next drill ignores keys until the hands have been
/// still this long, so the tail of the word you were typing can't fail the
/// practice drill before you have read it.
const SETTLE: Duration = Duration::from_millis(400);
/// No caret: typed text is bright, untyped faint, and the first faint letter
/// is the next one. The one ambiguity is a pending space after a finished
/// word, so after this long without a key it is drawn as a faint dot.
const HESITATE: Duration = Duration::from_millis(500);

use anyhow::Result;
use ratatui::DefaultTerminal;
use ratatui::crossterm::event::{Event, KeyCode, KeyEventKind, KeyModifiers};
use ratatui::layout::{Constraint, Layout, Rect};
use ratatui::style::{Color, Style, Stylize};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Paragraph};

use crate::input::Stamped;
use crate::mode::{Followup, Mode, Outcome, Strictness, Target, TargetKind, TargetResult};
use crate::store::{KeyRow, Store};

/// "Hopper navy": the box takes the user's terminal palette (a dark navy
/// lifted a step, its foreground as typed text). Faint text is blended from
/// the box toward the typed colour to read at 4:1. Hardcoded on purpose.
mod theme {
    use ratatui::style::Color;
    pub const BG: Color = Color::Rgb(0x1c, 0x2a, 0x40); // box
    pub const MAIN: Color = Color::Rgb(0xaf, 0xd2, 0xf4); // accents
    pub const SUB: Color = Color::Rgb(0x7f, 0x8a, 0x83); // untyped text, 4:1 on the box
    pub const TEXT: Color = Color::Rgb(0xd4, 0xdb, 0xc2); // typed text, a step brighter than the terminal foreground
    pub const ERROR: Color = Color::Rgb(0xee, 0x96, 0x75);
}

pub struct Kernel {
    pub store: Store,
    pub rx: Receiver<Stamped>,
    pub terminal: DefaultTerminal,
    /// A gap this long between two keys voids the drill (outcome `afk`).
    pub afk: Duration,
    /// Session labels (keyboard, place, ...) stamped on every drill.
    pub tags: Vec<String>,
}

struct Queued {
    mode: Box<dyn Mode>,
    parent: Option<i64>,
}

/// Live state of one drill.
struct Drill<'a> {
    id: i64,
    t0: Instant,
    mode: &'a dyn Mode,
    targets: Vec<Target>,
    /// Expected key sequence per target, precomputed.
    expected: Vec<Vec<char>>,
    shown: Vec<bool>,
    cur: usize,
    typed: Vec<char>,
    seq: i64,
    first_key: Option<Instant>,
    last_key: Option<Instant>,
    /// Per finished target: characters standing correct at the end (what
    /// wpm counts); 0 while the target is current or reopened.
    target_correct: Vec<usize>,
    /// Per target: keys typed, keys accepted. Missed words are added from
    /// `wrong_spans` when the drill completes.
    results: Vec<TargetResult>,
    /// Words judged wrong under lenient, as typed; they stay red after the
    /// caret has moved on, until a backspace reopens the word.
    wrong_spans: Vec<WrongSpan>,
    banner: Option<String>,
    tags: String,
    /// While set, keys arriving sooner than SETTLE after the previous one are
    /// ignored; cleared by the first key that arrives after a quiet gap.
    settle_last_input: Option<Instant>,
}

impl Kernel {
    pub fn run(&mut self, first: Vec<Box<dyn Mode>>) -> Result<()> {
        let mut queue: VecDeque<Queued> =
            first.into_iter().map(|mode| Queued { mode, parent: None }).collect();
        let mut banner = None;
        while let Some(Queued { mut mode, parent }) = queue.pop_front() {
            let (id, outcome) = self.run_drill(mode.as_mut(), parent, banner.take())?;
            if let Outcome::Aborted = outcome {
                break;
            }
            match &outcome {
                Outcome::Failed { word, .. } => banner = Some(format!("missed  {word}")),
                Outcome::Afk => banner = Some("paused too long, drill void".to_string()),
                Outcome::Completed { results, wpm } => {
                    // Accuracy counts every keypress, as Monkeytype does: a
                    // mistake you fixed still costs here, not in the wpm.
                    let chars: usize = results.iter().map(|r| r.chars).sum();
                    let correct: usize = results.iter().map(|r| r.correct).sum();
                    let acc = if chars == 0 { 100.0 } else { 100.0 * correct as f64 / chars as f64 };
                    let wrong: Vec<&str> =
                        results.iter().flat_map(|r| r.wrong_words.iter().map(String::as_str)).collect();
                    let mut b = format!("{wpm:.0} wpm · {acc:.0}%");
                    if !wrong.is_empty() {
                        b.push_str(&format!("   missed  {}", wrong.join(", ")));
                    }
                    banner = Some(b);
                }
                Outcome::Aborted => {}
            }
            for Followup { mode, child } in mode.after(&outcome).into_iter().rev() {
                queue.push_front(Queued { mode, parent: child.then_some(id) });
            }
        }
        Ok(())
    }

    fn run_drill(
        &mut self,
        mode: &mut dyn Mode,
        parent: Option<i64>,
        banner: Option<String>,
    ) -> Result<(i64, Outcome)> {
        let targets = mode.targets();
        let id = self.store.begin_drill(mode.name(), parent, &mode.config(), &self.tags)?;
        let t0 = Instant::now();
        if targets.is_empty() {
            let outcome = Outcome::Completed { results: Vec::new(), wpm: 0.0 };
            self.store.end_drill(id, &outcome, 0)?;
            return Ok((id, outcome));
        }
        let expected = targets.iter().map(Target::expected).collect();
        let n = targets.len();
        let mut d = Drill {
            id,
            t0,
            mode,
            targets,
            expected,
            shown: vec![false; n],
            cur: 0,
            typed: Vec::new(),
            seq: 0,
            first_key: None,
            last_key: None,
            target_correct: vec![0; n],
            results: vec![TargetResult::default(); n],
            wrong_spans: Vec::new(),
            settle_last_input: banner.is_some().then_some(t0),
            banner,
            tags: self.tags.join(" "),
        };
        let strictness = d.mode.strictness();

        loop {
            self.mark_shown(&mut d)?;
            self.terminal.draw(|f| draw(f, &d))?;

            // Wake up without a key when the settle period ends (the fade
            // lifts) or when a pending space has waited long enough to show
            // its dot; either way just redraw.
            let wake = match d.settle_last_input {
                Some(last) => Some(SETTLE.saturating_sub(last.elapsed())),
                None if d.next_is_space() && !d.hesitating() => {
                    d.last_key.map(|t| HESITATE.saturating_sub(t.elapsed()))
                }
                None => None,
            };
            let stamped = match wake {
                Some(remaining) => match self.rx.recv_timeout(remaining) {
                    Ok(st) => st,
                    Err(std::sync::mpsc::RecvTimeoutError::Timeout) => {
                        if d.settle_last_input.is_some_and(|last| last.elapsed() >= SETTLE) {
                            d.settle_last_input = None;
                        }
                        continue;
                    }
                    Err(e) => return Err(e.into()),
                },
                None => self.rx.recv()?,
            };
            let Stamped { at, event } = stamped;
            let Event::Key(key) = event else { continue };
            if key.kind != KeyEventKind::Press {
                continue;
            }
            let t_ns = (at - d.t0).as_nanos() as i64;

            let is_quit = matches!(key.code, KeyCode::Esc)
                || matches!(key.code, KeyCode::Char('c') if key.modifiers.contains(KeyModifiers::CONTROL));
            if let Some(last) = d.settle_last_input
                && !is_quit
            {
                if at.duration_since(last) < SETTLE {
                    d.settle_last_input = Some(at);
                    if let KeyCode::Char(c) = key.code {
                        let pos = d.typed.len();
                        self.record(&d, t_ns, pos, Some(c), "ignored", None)?;
                        d.seq += 1;
                    }
                    continue;
                }
                d.settle_last_input = None;
            }

            if let Some(last) = d.last_key
                && at.duration_since(last) > self.afk
                && !is_quit
            {
                // The key that ends the pause is dropped: the drill is void and
                // the screen is about to change.
                return self.finish(&d, t_ns, Outcome::Afk);
            }
            if matches!(key.code, KeyCode::Char(_) | KeyCode::Backspace) {
                d.last_key = Some(at);
            }

            match key.code {
                KeyCode::Esc => return self.finish(&d, t_ns, Outcome::Aborted),
                KeyCode::Char('c') if key.modifiers.contains(KeyModifiers::CONTROL) => {
                    return self.finish(&d, t_ns, Outcome::Aborted);
                }
                // Alt/Ctrl+Backspace, Ctrl+W and Ctrl+Alt+H delete the word
                // being typed, like readline. Logged as a backspace named `word`.
                KeyCode::Backspace if key.modifiers.intersects(KeyModifiers::CONTROL | KeyModifiers::ALT) => {
                    self.delete_word(&mut d, t_ns)?;
                }
                KeyCode::Char('w') if key.modifiers == KeyModifiers::CONTROL => {
                    self.delete_word(&mut d, t_ns)?;
                }
                KeyCode::Char('h') if key.modifiers.contains(KeyModifiers::CONTROL | KeyModifiers::ALT) => {
                    self.delete_word(&mut d, t_ns)?;
                }
                KeyCode::Backspace => {
                    let start = if strictness == Strictness::Lenient { word_start(&d.typed) } else { 0 };
                    if d.typed.len() > start {
                        d.typed.pop();
                        let pos = d.typed.len();
                        self.record(&d, t_ns, pos, None, "backspace", None)?;
                        d.seq += 1;
                    } else if strictness == Strictness::Lenient && start > 0 {
                        // At the start of a word: reopen the one before it,
                        // as typed, for correction. Its separator goes.
                        d.typed.pop();
                        let pos = d.typed.len();
                        self.record(&d, t_ns, pos, None, "backspace", None)?;
                        d.seq += 1;
                        reopen(&mut d, pos);
                    } else if strictness == Strictness::Lenient
                        && d.cur > 0
                        && d.targets[d.cur - 1].kind == TargetKind::Word
                    {
                        // Nothing typed yet: back into the previous word
                        // target, which is no longer complete.
                        d.cur -= 1;
                        d.target_correct[d.cur] = 0;
                        self.store.uncomplete_target(d.id, d.cur)?;
                        let exp = &d.expected[d.cur];
                        d.typed = exp[..exp.len() - 1].to_vec();
                        let pos = d.typed.len();
                        self.record(&d, t_ns, pos, None, "backspace", None)?;
                        d.seq += 1;
                        reopen(&mut d, pos);
                    }
                }
                // Ctrl/Alt combos are not typing, but they are evidence (a
                // home-row modifier that engaged), so they are logged, not judged.
                KeyCode::Char(c) if key.modifiers.intersects(KeyModifiers::CONTROL | KeyModifiers::ALT) => {
                    let name = format!(
                        "{}{}{c}",
                        if key.modifiers.contains(KeyModifiers::CONTROL) { "ctrl+" } else { "" },
                        if key.modifiers.contains(KeyModifiers::ALT) { "alt+" } else { "" },
                    );
                    let pos = d.typed.len();
                    self.record_named(&d, t_ns, pos, name, "modified")?;
                    d.seq += 1;
                }
                KeyCode::Char(c) => {
                    d.first_key.get_or_insert(at);
                    let pos = d.typed.len();
                    let exp = &d.expected[d.cur];
                    let matched = exp.get(pos) == Some(&c);

                    // Judge first, record second. `correct` means the kernel
                    // accepted the key as progress, so the key that ends a
                    // failed drill is always recorded as 0, whichever rule
                    // rejected it. `rejected` carries the word to practise.
                    let rejected: Option<String> = match strictness {
                        Strictness::Master if !matched => {
                            let (ws, we) = word_span(exp, pos.min(exp.len().saturating_sub(1)));
                            Some(exp[ws..we].iter().collect())
                        }
                        _ => None,
                    };

                    let correct = matched && rejected.is_none();
                    self.record(&d, t_ns, pos, Some(c), "char", Some(correct))?;
                    d.seq += 1;
                    d.results[d.cur].chars += 1;
                    if correct {
                        d.results[d.cur].correct += 1;
                    }
                    if let Some(word) = rejected {
                        return self.fail(&d, t_ns, word);
                    }

                    if strictness == Strictness::Lenient && c == ' ' {
                        // A space submits the word; right or wrong, the caret
                        // moves to the next expected word. With nothing typed
                        // for the word yet (a double tap) it does nothing.
                        let ws = word_start(&d.typed);
                        if ws < exp.len() && ws < d.typed.len() {
                            let (_, we) = word_span(exp, ws);
                            let typed_word = d.typed[ws..].to_vec();
                            submit_word(&mut d, ws, we, &typed_word);
                        }
                    } else {
                        if matched || pos < exp.len() {
                            // Lenient keeps the wrong key on screen.
                            d.typed.push(c);
                        }
                        // Lenient, last key of the target: no space will
                        // come, so the last word is submitted now.
                        let len = exp.len();
                        if strictness == Strictness::Lenient && d.typed.len() == len {
                            let ws = word_start(&d.typed);
                            let last_ws = word_span(exp, len - 1).0;
                            if ws == last_ws && ws < len {
                                let typed_word = d.typed[ws..].to_vec();
                                submit_word(&mut d, ws, len, &typed_word);
                            }
                        }
                    }

                    let exp = &d.expected[d.cur];
                    if d.typed.len() == exp.len() && d.typed == *exp {
                        self.store.complete_target(d.id, d.cur, t_ns)?;
                        d.target_correct[d.cur] = live_correct(&d);
                        d.cur += 1;
                        d.typed.clear();
                        if d.cur == d.targets.len() {
                            let mut results = d.results.clone();
                            let mut spans: Vec<&WrongSpan> = d.wrong_spans.iter().collect();
                            spans.sort_by_key(|s| (s.target, s.start));
                            for s in spans {
                                results[s.target].wrong_words.push(d.expected[s.target][s.start..s.end].iter().collect());
                            }
                            let correct: usize = d.target_correct.iter().sum();
                            let wpm = d.first_key.map_or(0.0, |t| {
                                correct as f64 / 5.0 / (t.elapsed().as_secs_f64().max(0.5) / 60.0)
                            });
                            return self.finish(&d, t_ns, Outcome::Completed { results, wpm });
                        }
                    }
                }
                // Anything else (enter, tab, arrows, ...) is logged the same way.
                other => {
                    let pos = d.typed.len();
                    self.record_named(&d, t_ns, pos, format!("{other:?}").to_lowercase(), "other")?;
                    d.seq += 1;
                }
            }
        }
    }

    /// Drop the trailing spaces and then the word before them from what has
    /// been typed for the current target (at the start of a word that is
    /// the previous word, which is then open again); nothing happens (and
    /// nothing is logged) when there is nothing to delete.
    fn delete_word(&self, d: &mut Drill, t_ns: i64) -> Result<()> {
        let mut n = d.typed.len();
        while n > 0 && d.typed[n - 1] == ' ' {
            n -= 1;
        }
        while n > 0 && d.typed[n - 1] != ' ' {
            n -= 1;
        }
        if n == d.typed.len() {
            return Ok(());
        }
        self.record_named(d, t_ns, n, "word".to_string(), "backspace")?;
        d.seq += 1;
        reopen(d, n);
        Ok(())
    }

    /// A keystroke row for a key that is not a typed character: `typed` holds
    /// its name (e.g. `ctrl+x`, `enter`), `correct` is NULL.
    fn record_named(&self, d: &Drill, t_ns: i64, pos: usize, name: String, kind: &'static str) -> Result<()> {
        let row = KeyRow {
            seq: d.seq,
            t_ns,
            target_idx: d.cur as i64,
            pos: pos as i64,
            expected: d.expected[d.cur].get(pos).copied(),
            typed_name: Some(name),
            typed: None,
            kind,
            correct: None,
        };
        self.store.add_key(d.id, &row)
    }

    /// Write the row for the current target when it becomes current. Words
    /// are all on screen from the start, so their `shown_at` is 0; a sentence
    /// appears when it becomes current. Targets never reached get no row.
    fn mark_shown(&self, d: &mut Drill) -> Result<()> {
        let i = d.cur;
        if d.shown[i] {
            return Ok(());
        }
        let shown_at = match d.targets[i].kind {
            TargetKind::Word => 0,
            TargetKind::Sentence => d.t0.elapsed().as_nanos() as i64,
        };
        self.store.add_target(d.id, i, &d.targets[i], shown_at)?;
        d.shown[i] = true;
        Ok(())
    }

    fn record(
        &self,
        d: &Drill,
        t_ns: i64,
        pos: usize,
        typed: Option<char>,
        kind: &'static str,
        correct: Option<bool>,
    ) -> Result<()> {
        let row = KeyRow {
            seq: d.seq,
            t_ns,
            target_idx: d.cur as i64,
            pos: pos as i64,
            expected: d.expected[d.cur].get(pos).copied(),
            typed,
            typed_name: None,
            kind,
            correct,
        };
        self.store.add_key(d.id, &row)
    }

    fn fail(&self, d: &Drill, t_ns: i64, word: String) -> Result<(i64, Outcome)> {
        let outcome = Outcome::Failed { target: d.targets[d.cur].clone(), word };
        self.finish(d, t_ns, outcome)
    }

    fn finish(&self, d: &Drill, t_ns: i64, outcome: Outcome) -> Result<(i64, Outcome)> {
        self.store.end_drill(d.id, &outcome, t_ns)?;
        Ok((d.id, outcome))
    }
}

/// Where the word being typed starts: after the last separator typed.
fn word_start(typed: &[char]) -> usize {
    typed.iter().rposition(|&c| c == ' ').map_or(0, |i| i + 1)
}

/// Lenient: judge the word typed for `exp[ws..we]` on what it ends up as;
/// it is missed if it differs. Then resync `typed` to the expected text up
/// to and including the separator, so the caret is on the next word
/// whatever was typed.
fn submit_word(d: &mut Drill, ws: usize, we: usize, typed_word: &[char]) {
    let exp = &d.expected[d.cur];
    if typed_word != &exp[ws..we] {
        d.wrong_spans.push(WrongSpan { target: d.cur, start: ws, end: we, typed: typed_word.to_vec() });
    }
    let upto = (we + 1).min(exp.len());
    d.typed.truncate(ws);
    d.typed.extend_from_slice(&exp[ws..upto]);
}

/// Cut `typed` back to `n` characters. A submitted word whose separator
/// (at its `end`) is cut is open again: the one ending exactly at `n` comes
/// back as it was typed, to be corrected; any later one is simply gone.
fn reopen(d: &mut Drill, n: usize) {
    d.typed.truncate(n);
    let cur = d.cur;
    let mut i = 0;
    while i < d.wrong_spans.len() {
        let s = &d.wrong_spans[i];
        if s.target == cur && s.end >= n {
            if s.start < n {
                let typed = s.typed.clone();
                d.typed.truncate(s.start);
                d.typed.extend_from_slice(&typed);
            }
            d.wrong_spans.remove(i);
        } else {
            i += 1;
        }
    }
}

/// Characters of the current target standing correct right now. After a
/// resync `typed` holds the expected text for every submitted word, so a
/// word judged wrong is counted from what was really typed, and its
/// separator counts against it.
fn live_correct(d: &Drill) -> usize {
    let exp = &d.expected[d.cur];
    let mut n = d.typed.iter().zip(exp).filter(|(a, b)| a == b).count();
    for s in d.wrong_spans.iter().filter(|s| s.target == d.cur) {
        n -= (s.end + 1).min(d.typed.len()).saturating_sub(s.start);
        n += s.typed.iter().enumerate().filter(|&(i, c)| s.start + i < s.end && exp[s.start + i] == *c).count();
    }
    n
}

/// `[start, end)` of the word in `exp` that contains position `pos`, where
/// words are separated by spaces and `end` points at the separator (or the
/// end of the sequence).
fn word_span(exp: &[char], pos: usize) -> (usize, usize) {
    let pos = pos.min(exp.len());
    let start = exp[..pos].iter().rposition(|&c| c == ' ').map_or(0, |i| i + 1);
    let end = exp[pos..].iter().position(|&c| c == ' ').map_or(exp.len(), |i| pos + i);
    (start, end)
}

// ---------------------------------------------------------------- rendering

/// Text column width and the padding of the box around it, in cells.
const COLUMN: u16 = 60;
const PAD_X: u16 = 4;
const PAD_Y: u16 = 1;
/// Lines of text shown at once (the window scrolls with the caret, like
/// Monkeytype), and the blank rows left between them for air.
const VISIBLE_LINES: usize = 3;
const LINE_GAP: u16 = 1;

fn draw(f: &mut ratatui::Frame, d: &Drill) {
    let [header, banner, body, footer] = Layout::vertical([
        Constraint::Length(1),
        Constraint::Length(2),
        Constraint::Min(3),
        Constraint::Length(1),
    ])
    .areas(f.area());

    f.render_widget(
        Line::from(vec![
            Span::raw(format!(" {}", d.mode.title())).dim(),
            Span::raw(if d.tags.is_empty() { "  no tags".to_string() } else { format!("  tags: {}", d.tags) })
                .fg(theme::MAIN),
            Span::raw("  esc quits").dim(),
        ]),
        header,
    );
    if let Some(b) = &d.banner {
        f.render_widget(Line::from(format!(" {b}")).fg(theme::ERROR).bold(), banner);
    }

    // The text column, and a padded box around it painted in the theme.
    let column = centered(body, COLUMN);
    let (all, caret_line) = wrap(d, column.width.max(1) as usize);
    // Window of VISIBLE_LINES around the caret: the caret's line is the second
    // one once the text has scrolled, like Monkeytype.
    let first = caret_line.saturating_sub(1).min(all.len().saturating_sub(VISIBLE_LINES));
    let window: Vec<Line<'static>> = all.into_iter().skip(first).take(VISIBLE_LINES).collect();
    let mut lines: Vec<Line<'static>> = Vec::new();
    for (i, l) in window.into_iter().enumerate() {
        if i > 0 {
            for _ in 0..LINE_GAP {
                lines.push(Line::default());
            }
        }
        lines.push(l);
    }
    let n = lines.len() as u16;
    let text_h = n.min(column.height.saturating_sub(2 * PAD_Y)).max(1);
    let box_h = text_h + 2 * PAD_Y;
    let box_area = Rect {
        x: column.x.saturating_sub(PAD_X),
        y: column.y + column.height.saturating_sub(box_h) / 2,
        width: (column.width + 2 * PAD_X).min(body.width),
        height: box_h,
    };
    f.render_widget(Block::new().style(Style::default().bg(theme::BG)), box_area);
    let text_area = Rect { x: column.x, y: box_area.y + PAD_Y, width: column.width, height: text_h };
    f.render_widget(Paragraph::new(lines), text_area);

    let wpm = match d.first_key {
        Some(t) if t.elapsed().as_secs_f64() > 0.5 => {
            let correct = d.target_correct.iter().sum::<usize>() + live_correct(d);
            correct as f64 / 5.0 / (t.elapsed().as_secs_f64() / 60.0)
        }
        _ => 0.0,
    };
    f.render_widget(
        Line::from(format!(" {:>3.0} wpm   {}/{}", wpm, d.cur, d.targets.len())).dim(),
        footer,
    );
}

fn centered(area: Rect, max_width: u16) -> Rect {
    let w = max_width.min(area.width.saturating_sub(4));
    let [_, mid, _] = Layout::horizontal([Constraint::Fill(1), Constraint::Length(w), Constraint::Fill(1)])
        .areas(area);
    mid
}

/// A word submitted wrong under lenient strictness: `typed` is what was
/// typed for `expected[target][start..end]`, kept so the screen shows it.
struct WrongSpan {
    target: usize,
    start: usize,
    end: usize,
    typed: Vec<char>,
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum Glyph {
    Done,
    Wrong,
    /// The next expected character; drawn like Pending, but it places the
    /// window and, for a space, carries the hesitation dot.
    Cursor,
    Pending,
}

impl Drill<'_> {
    fn next_is_space(&self) -> bool {
        self.expected[self.cur].get(self.typed.len()) == Some(&' ')
    }

    /// No key for HESITATE since the last one.
    fn hesitating(&self) -> bool {
        self.last_key.is_some_and(|t| t.elapsed() >= HESITATE)
    }
}

/// Pull a colour toward the box background; `t` = 0 leaves it, 1 hides it.
fn toward_bg(c: Color, t: f32) -> Color {
    let (Color::Rgb(r, g, b), Color::Rgb(br, bg, bb)) = (c, theme::BG) else { return c };
    let mix = |a: u8, b: u8| (a as f32 + (b as f32 - a as f32) * t).round() as u8;
    Color::Rgb(mix(r, br), mix(g, bg), mix(b, bb))
}

/// `faded` is the settle period after a failure: the box is on screen but
/// keys are ignored, and everything in it sits closer to the background.
fn style(g: Glyph, faded: bool) -> Style {
    let t = if faded { 0.6 } else { 0.0 };
    match g {
        Glyph::Done => Style::default().fg(toward_bg(theme::TEXT, t)),
        Glyph::Wrong => Style::default().fg(toward_bg(theme::ERROR, t)),
        Glyph::Pending | Glyph::Cursor => Style::default().fg(toward_bg(theme::SUB, t)),
    }
}

/// Glyph state of each expected character in target `i`.
fn glyphs(d: &Drill, i: usize) -> Vec<(char, Glyph)> {
    let exp = &d.expected[i];
    let mut out = Vec::with_capacity(exp.len());
    for (p, &c) in exp.iter().enumerate() {
        if let Some(span) = d.wrong_spans.iter().find(|w| w.target == i && w.start <= p && p < w.end) {
            // A word submitted wrong stays on screen as typed, in red; letters
            // never typed stay faint, extra letters are appended.
            match span.typed.get(p - span.start) {
                Some(&t) => out.push((t, Glyph::Wrong)),
                None => out.push((c, Glyph::Pending)),
            }
            if p + 1 == span.end {
                let extra = span.typed.get(span.end - span.start..).unwrap_or(&[]);
                out.extend(extra.iter().map(|&t| (t, Glyph::Wrong)));
            }
            continue;
        }
        let g = if i < d.cur {
            Glyph::Done
        } else if i > d.cur {
            Glyph::Pending
        } else if p < d.typed.len() {
            if d.typed[p] == c { Glyph::Done } else { Glyph::Wrong }
        } else if p == d.typed.len() {
            Glyph::Cursor
        } else {
            Glyph::Pending
        };
        // Show the wrong key itself where one was typed, so you see what
        // happened; a pending space gets its dot once you hesitate on it.
        let shown = match g {
            Glyph::Wrong if c != ' ' => d.typed[p],
            Glyph::Cursor if c == ' ' && d.hesitating() => '·',
            _ => c,
        };
        out.push((shown, g));
    }
    out
}

/// Greedy word wrap of what is on screen (all words, or the current
/// sentence) into lines of `width`, plus the index of the line holding the
/// caret. Done by hand so the window and the box can be sized.
fn wrap(d: &Drill, width: usize) -> (Vec<Line<'static>>, usize) {
    let faded = d.settle_last_input.is_some();
    // Tokens are "word plus trailing space"; a line never breaks inside one.
    let mut tokens: Vec<Vec<(char, Glyph)>> = Vec::new();
    let shown: Vec<usize> = match d.targets[d.cur].kind {
        TargetKind::Word => (0..d.targets.len()).collect(),
        TargetKind::Sentence => vec![d.cur],
    };
    for i in shown {
        let mut tok = Vec::new();
        for g in glyphs(d, i) {
            let is_space = g.0 == ' ';
            tok.push(g);
            if is_space {
                tokens.push(std::mem::take(&mut tok));
            }
        }
        if !tok.is_empty() {
            tokens.push(tok);
        }
    }

    let mut lines: Vec<Vec<Span<'static>>> = vec![Vec::new()];
    let mut col = 0usize;
    let mut caret_line = 0;
    for tok in tokens {
        if col > 0 && col + tok.len() > width {
            lines.push(Vec::new());
            col = 0;
        }
        for (c, g) in tok {
            if g == Glyph::Cursor {
                caret_line = lines.len() - 1;
            }
            lines.last_mut().unwrap().push(Span::styled(c.to_string(), style(g, faded)));
            col += 1;
        }
    }
    (lines.into_iter().map(Line::from).collect(), caret_line)
}
