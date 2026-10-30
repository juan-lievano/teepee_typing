//! The kernel: runs a queue of drills. Owns the terminal, the clock and the
//! store. Implements the three strictness levels once, for every mode.

use std::collections::VecDeque;
use std::sync::mpsc::Receiver;
use std::time::Instant;

use anyhow::Result;
use ratatui::DefaultTerminal;
use ratatui::crossterm::event::{Event, KeyCode, KeyEventKind, KeyModifiers};
use ratatui::layout::{Constraint, Layout, Rect};
use ratatui::style::{Color, Modifier, Style, Stylize};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Paragraph, Wrap};

use crate::input::Stamped;
use crate::mode::{Followup, Mode, Outcome, Strictness, Target, TargetKind};
use crate::store::{KeyRow, Store};

pub struct Kernel {
    pub store: Store,
    pub rx: Receiver<Stamped>,
    pub terminal: DefaultTerminal,
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
    correct_chars: usize,
    banner: Option<String>,
    tags: String,
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
            if let Outcome::Failed { word, .. } = &outcome {
                banner = Some(format!("missed  {word}"));
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
            correct_chars: 0,
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

    /// Record `shown_at` for every target that is about to be on screen.
    fn mark_shown(&self, d: &mut Drill) -> Result<()> {
        let now = d.t0.elapsed().as_nanos() as i64;
        for i in 0..d.targets.len() {
            if d.shown[i] {
                continue;
            }
            let visible = match d.targets[i].kind {
                TargetKind::Word => true,
                TargetKind::Sentence => i == d.cur,
            };
            if visible {
                self.store.add_target(d.id, i, &d.targets[i], now)?;
                d.shown[i] = true;
            }
        }
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
            Span::raw(if d.tags.is_empty() { String::new() } else { format!("  tags: {}", d.tags) }).cyan().bold(),
            Span::raw("  esc quits").dark_gray(),
        ]),
        header,
    );
    if let Some(b) = &d.banner {
        f.render_widget(Line::from(format!(" {b}")).red().bold(), banner);
    }

    let column = centered(body, 72);
    let text = match d.targets[d.cur].kind {
        TargetKind::Word => words_text(d),
        TargetKind::Sentence => sentence_text(d),
    };
    let chars: usize = text.iter().map(Line::width).sum();
    let para = Paragraph::new(text).wrap(Wrap { trim: false });
    // Vertically centre a short text, keep long ones top-aligned (estimate).
    let lines = (chars / column.width.max(1) as usize + 1) as u16;
    let area = if lines < column.height {
        Rect { y: column.y + (column.height - lines) / 2, height: lines, ..column }
    } else {
        column
    };
    f.render_widget(para, area);

    let wpm = match d.first_key {
        Some(t) if t.elapsed().as_secs_f64() > 0.5 => {
            d.correct_chars as f64 / 5.0 / (t.elapsed().as_secs_f64() / 60.0)
        }
        _ => 0.0,
    };
    f.render_widget(
        Line::from(format!(" {:>3.0} wpm   {}/{}", wpm, d.cur, d.targets.len())).dark_gray(),
        footer,
    );
}

fn centered(area: Rect, max_width: u16) -> Rect {
    let w = max_width.min(area.width.saturating_sub(4));
    let [_, mid, _] = Layout::horizontal([Constraint::Fill(1), Constraint::Length(w), Constraint::Fill(1)])
        .areas(area);
    mid
}

#[derive(Clone, Copy)]
enum Glyph {
    Done,
    Wrong,
    Cursor,
    Pending,
}

fn style(g: Glyph) -> Style {
    match g {
        Glyph::Done => Style::default(),
        Glyph::Wrong => Style::default().fg(Color::Red).add_modifier(Modifier::UNDERLINED),
        Glyph::Cursor => Style::default().add_modifier(Modifier::REVERSED),
        Glyph::Pending => Style::default().fg(Color::DarkGray),
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
            let shown = if matches!(g, Glyph::Wrong) && c != ' ' { d.typed[p] } else { c };
            (shown, g)
        })
        .collect()
}

/// All words of the drill as one wrapped paragraph, Monkeytype style.
fn words_text(d: &Drill) -> Vec<Line<'static>> {
    let mut spans = Vec::new();
    for i in 0..d.targets.len() {
        for (c, g) in glyphs(d, i) {
            spans.push(Span::styled(c.to_string(), style(g)));
        }
    }
    vec![Line::from(spans)]
}

/// Only the current sentence.
fn sentence_text(d: &Drill) -> Vec<Line<'static>> {
    let spans = glyphs(d, d.cur)
        .into_iter()
        .map(|(c, g)| Span::styled(c.to_string(), style(g)))
        .collect::<Vec<_>>();
    vec![Line::from(spans)]
}
