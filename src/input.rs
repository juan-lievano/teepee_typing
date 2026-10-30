//! Keys are stamped on a dedicated thread the instant `read()` returns, so
//! drawing and database writes on the main thread can never delay a stamp.

use std::sync::mpsc::{self, Receiver};
use std::thread;
use std::time::Instant;

use ratatui::crossterm::event::{self, Event};

pub struct Stamped {
    pub at: Instant,
    pub event: Event,
}

pub fn spawn() -> Receiver<Stamped> {
    let (tx, rx) = mpsc::channel();
    thread::spawn(move || {
        while let Ok(event) = event::read() {
            let at = Instant::now();
            if tx.send(Stamped { at, event }).is_err() {
                break;
            }
        }
    });
    rx
}
