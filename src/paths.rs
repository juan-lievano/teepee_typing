use std::path::PathBuf;

/// `$TEEPEE_DATA_DIR` if set (tests), else `data/` inside the source checkout
/// this binary was installed from. That folder is gitignored and, on this
/// machine, under iCloud's Documents, so the database is synced with no extra
/// machinery. SQLite runs in single-file journal mode so the file is
/// consistent between writes.
pub fn data_dir() -> PathBuf {
    if let Some(p) = std::env::var_os("TEEPEE_DATA_DIR") {
        return PathBuf::from(p);
    }
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("data")
}

pub fn db_path() -> PathBuf {
    data_dir().join("teepee.db")
}

pub fn books_dir() -> PathBuf {
    data_dir().join("books")
}
