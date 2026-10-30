//! The kernel: runs a queue of drills. Owns the terminal, the clock and the
//! store. Implements the three strictness levels once, for every mode.

use std::collections::VecDeque;
use std::sync::mpsc::Receiver;
use std::time::{Duration, Instant};

/// After a failure the next drill ignores keys until the hands have been
/// still this long, so the tail of the word you were typing can't fail the
/// practice drill before you have read it.
const SETTLE: Duration = Duration::from_millis(400);

use anyhow::Result;
use ratatui::DefaultTerminal;
use ratatui::crossterm::event::{Event, KeyCode, KeyEventKind, KeyModifiers};
use ratatui::layout::{Constraint, Layout, Rect};
use ratatui::style::{Style, Stylize};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Paragraph};

use crate::input::Stamped;
use crate::mode::{Followup, Mode, Outcome, Strictness, Target, TargetKind};
use crate::store::{KeyRow, Store};

/// "Hopper navy, ice caret": the box takes the user's terminal palette
/// (a dark navy lifted a step, the terminal's cursor blue as caret, its
/// foreground as typed text). Faint text is blended from the box toward
/// the typed colour to read at 3.4:1. Hardcoded on purpose.
mod theme {
    use ratatui::style::Color;
    pub const BG: Color = Color::Rgb(0x1c, 0x2a, 0x40); // box
    pub const MAIN: Color = Color::Rgb(0xaf, 0xd2, 0xf4); // caret, accents
    pub const SUB: Color = Color::Rgb(0x72, 0x7e, 0x7a); // untyped text
    pub const TEXT: Color = Color::Rgb(0xca, 0xd2, 0xb5); // typed text
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
    correct_chars: usize,
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
                Outcome::Afk { .. } => banner = Some("paused too long, drill void".to_string()),
                _ => {}
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
            self.store.end_drill(id, &Outcome::Completed, 0)?;
            return Ok((id, Outcome::Completed));
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
            correct_chars: 0,
            settle_last_input: banner.is_some().then_some(t0),
            banner,
            tags: self.tags.join(" "),
        };
        let strictness = d.mode.strictness();

        loop {
            self.mark_shown(&mut d)?;
            self.terminal.draw(|f| draw(f, &d))?;

            let Stamped { at, event } = self.rx.recv()?;
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
                return self.finish(&d, t_ns, Outcome::Afk { target_idx: d.cur });
            }
            if matches!(key.code, KeyCode::Char(_) | KeyCode::Backspace) {
                d.last_key = Some(at);
            }

            match key.code {
                KeyCode::Esc => return self.finish(&d, t_ns, Outcome::Aborted),
                KeyCode::Char('c') if key.modifiers.contains(KeyModifiers::CONTROL) => {
                    return self.finish(&d, t_ns, Outcome::Aborted);
                }
                KeyCode::Backspace => {
                    if d.typed.pop().is_some() {
                        let pos = d.typed.len();
                        self.record(&d, t_ns, pos, None, "backspace", None)?;
                        d.seq += 1;
                    }
                }
                KeyCode::Char(c)
                    if !key.modifiers.intersects(KeyModifiers::CONTROL | KeyModifiers::ALT) =>
                {
                    d.first_key.get_or_insert(at);
                    let pos = d.typed.len();
                    let exp = &d.expected[d.cur];
                    let want = exp.get(pos).copied();
                    let correct = want == Some(c);
                    self.record(&d, t_ns, pos, Some(c), "char", Some(correct))?;
                    d.seq += 1;

                    // Expert: a space submits the current word.
                    if strictness == Strictness::Expert && c == ' ' {
                        let (ws, we) = word_span(exp, pos);
                        let word_ok = pos == we && d.typed[ws..pos] == exp[ws..we];
                        if !word_ok {
                            let word: String = exp[ws..we].iter().collect();
                            return self.fail(&d, t_ns, word);
                        }
                    }

                    if correct {
                        d.correct_chars += 1;
                        d.typed.push(c);
                    } else {
                        match strictness {
                            Strictness::Master => {
                                let (ws, we) = word_span(exp, pos.min(exp.len().saturating_sub(1)));
                                let word: String = exp[ws..we].iter().collect();
                                return self.fail(&d, t_ns, word);
                            }
                            Strictness::Lenient | Strictness::Expert => {
                                if pos < exp.len() {
                                    d.typed.push(c);
                                }
                            }
                        }
                    }

                    if d.typed.len() == exp.len() {
                        if d.typed == *exp {
                            self.store.complete_target(d.id, d.cur, t_ns)?;
                            d.cur += 1;
                            d.typed.clear();
                            if d.cur == d.targets.len() {
                                return self.finish(&d, t_ns, Outcome::Completed);
                            }
                        } else if strictness == Strictness::Expert {
                            // Sentence ends on a wrong last word: no space will
                            // ever come, so judge it now.
                            let (ws, we) = word_span(exp, exp.len() - 1);
                            let word: String = exp[ws..we].iter().collect();
                            return self.fail(&d, t_ns, word);
                        }
                    }
                }
                _ => {}
            }
        }
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
            kind,
            correct,
        };
        self.store.add_key(d.id, &row)
    }

    fn fail(&self, d: &Drill, t_ns: i64, word: String) -> Result<(i64, Outcome)> {
        let outcome = Outcome::Failed { target_idx: d.cur, target: d.targets[d.cur].clone(), word };
        self.finish(d, t_ns, outcome)
    }

    fn finish(&self, d: &Drill, t_ns: i64, outcome: Outcome) -> Result<(i64, Outcome)> {
        self.store.end_drill(d.id, &outcome, t_ns)?;
        Ok((d.id, outcome))
    }
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
const COLUMN: u16 = 72;
const PAD_X: u16 = 4;
const PAD_Y: u16 = 1;

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
    let lines = wrap(d, column.width.max(1) as usize);
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
            d.correct_chars as f64 / 5.0 / (t.elapsed().as_secs_f64() / 60.0)
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

#[derive(Clone, Copy, PartialEq, Eq)]
enum Glyph {
    Done,
    Wrong,
    Cursor,
    Pending,
}

fn style(g: Glyph) -> Style {
    match g {
        Glyph::Done => Style::default().fg(theme::TEXT),
        Glyph::Wrong => Style::default().fg(theme::ERROR),
        Glyph::Pending => Style::default().fg(theme::SUB),
        Glyph::Cursor => Style::default().fg(theme::BG).bg(theme::MAIN),
    }
}

/// Glyph state of each expected character in target `i`.
fn glyphs(d: &Drill, i: usize) -> Vec<(char, Glyph)> {
    let exp = &d.expected[i];
    exp.iter()
        .enumerate()
        .map(|(p, &c)| {
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
            // Show the wrong key itself where one was typed, so you see what happened.
            let shown = if g == Glyph::Wrong && c != ' ' { d.typed[p] } else { c };
            (shown, g)
        })
        .collect()
}

/// Greedy word wrap of what is on screen (all words, or the current
/// sentence) into lines of `width`. Done by hand so the box can be sized.
fn wrap(d: &Drill, width: usize) -> Vec<Line<'static>> {
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
    for tok in tokens {
        if col > 0 && col + tok.len() > width {
            lines.push(Vec::new());
            col = 0;
        }
        for (c, g) in tok {
            lines.last_mut().unwrap().push(Span::styled(c.to_string(), style(g)));
            col += 1;
        }
    }
    lines.into_iter().map(Line::from).collect()
}
