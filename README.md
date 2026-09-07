# FTP Change Sync

Connects to an FTP server, walks every folder recursively, downloads and
hashes each file, and compares it against the last known hash stored in a
SQLite database. New and changed files are saved locally; every change is
logged with a timestamp. Snapshots of the files are created prior to any
updates being made.

## Setup

```
cp config.ini.example config.ini
```

Edit `config.ini` with your server details. There are three options for password use:

- Put it directly in `config.ini` (simplest, least secure)
- Set the `FTP_PASSWORD` environment variable (keeps it out of the file)
- Leave it blank - you'll be prompted for it each time you run the script

Permissions on the config.ini should be restricted to the user running ftp_sync.


## Usage

```
python ftp_sync.py [--config config.ini] [--dry-run] [--site name [name]]
```

**--dry-run** previews what would change without downloading anything or touching the database:

```
python ftp_sync.py --config config.ini --dry-run
```

Since it hashes the full contents of every file, run time scales with the
total size of the server, not just the number of files - this is thorough
but not fast for a very large site.

## What gets stored

**`mirror/`** (or wherever `local_dir` points) - a local copy of every file
that has ever been downloaded, kept up to date with the newest version.

**SQLite database** (`ftp_changes.db` by default) with two tables:

- `files` - current state: path, hash, size, when first seen, when last
  changed, when last checked. One row per file currently on the server.
- `change_log` - full history: every new/modified/deleted event with the
  old hash, new hash, size, and UTC timestamp. Nothing is ever deleted from
  this table, so it's a complete audit trail.

Example queries:

```sql
-- Everything that changed in the last 24 hours
SELECT * FROM change_log WHERE changed_at >= datetime('now', '-1 day');

-- Full change history for one file
SELECT * FROM change_log WHERE path = '/site/index.html' ORDER BY changed_at;

-- Files removed from the server
SELECT * FROM change_log WHERE change_type = 'deleted';
```

## Notes

- Files that disappear from the server are logged as `deleted` in
  `change_log` and removed from the `files` table (their local copy in
  `mirror/` is left alone - only the tracking record is removed).
- If your server doesn't support the `MLSD` command, the script falls back
  to `NLST` with directory probing, which is slower but works on older
  FTP servers.
- Run it by hand whenever you want a fresh sync; nothing is scheduled
  automatically.
