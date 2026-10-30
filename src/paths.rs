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

/// Where exit-time snapshots of the database go. Under iCloud's Documents so
/// they get synced; a snapshot is a closed, consistent copy, which is safe to
/// sync where the live WAL database would not be. With `TEEPEE_DATA_DIR` set
/// (tests), snapshots stay inside that directory.
pub fn backup_dir() -> PathBuf {
    if std::env::var_os("TEEPEE_DATA_DIR").is_some() {
        return data_dir().join("backups");
    }
    dirs::home_dir().expect("home directory").join("Documents/teepee_typing_backups")
}
