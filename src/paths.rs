use std::path::PathBuf;

/// `$TEEPEE_DATA_DIR`, else `~/.local/share/teepee_typing`.
pub fn data_dir() -> PathBuf {
    if let Some(p) = std::env::var_os("TEEPEE_DATA_DIR") {
        return PathBuf::from(p);
    }
    dirs::home_dir()
        .expect("home directory")
        .join(".local/share/teepee_typing")
}

pub fn db_path() -> PathBuf {
    data_dir().join("teepee.db")
}

pub fn books_dir() -> PathBuf {
    data_dir().join("books")
}
