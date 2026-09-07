#!/usr/bin/env python3
"""
ftp_sync.py

Connects to one or more FTP servers ("sites"), recursively walks each
site's remote tree, downloads and hashes every file, and compares each
hash against the last known hash stored in a shared SQLite database.
New and modified files are saved to that site's local directory and
logged; files that vanish from the server are logged as deleted.

Before any changes are applied for a site, the entire current local
copy is snapshotted into one dated .tar.gz archive under that site's
snapshot directory. That way, restoring the whole tree after a bad
sync (or a compromised source) is a single "extract this one archive"
operation rather than reassembling many individually archived files.

Config format (config.ini):

    [general]
    db_path = ./ftp_changes.db

    [site:name1]
    host = ftp.example1.com
    port = 21
    user = user1
    password =
    remote_root = /public
    local_dir = ./example1
    snapshot_dir = ./example1/.snapshots  ; optional, defaults shown
    use_tls = false
    encoding = latin-1

    [site:name2]
    host = ftp.example2.com
    ...

Each [site:NAME] section is synced independently; NAME is free text
(used as the "site" column in the database and in log output).

Usage:
    python ftp_sync.py --config config.ini
    python ftp_sync.py --config config.ini --dry-run         # detect changes only
    python ftp_sync.py --config config.ini --site name1      # only sync one site
    python ftp_sync.py --config config.ini --site name1 --site name2

Password:
    Put it in config.ini under the site, or set an environment variable
    FTP_PASSWORD_<NAME> (NAME upper-cased, recommended - keeps it out of
    the config file), or leave both empty and you'll be prompted for it
    for that site each run.
"""

import argparse
import configparser
import ftplib
import getpass
import hashlib
import os
import shutil
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_CONFIG = "config.ini"
SITE_PREFIX = "site:"


#--------------------------------------------------------------------------
def load_config(path):
    if not os.path.exists(path):
        print(f"Config file not found: {path}")
        print("Copy config.ini.example to config.ini and fill it in.")
        sys.exit(1)

    parser = configparser.ConfigParser()
    parser.read(path)

    general = parser["general"] if parser.has_section("general") else {}
    db_path = general.get("db_path", "./ftp_changes.db")

    sites = {}
    for section_name in parser.sections():
        if not section_name.startswith(SITE_PREFIX):
            continue
        site_name = section_name[len(SITE_PREFIX):].strip()
        if not site_name:
            print(f"Skipping section with empty site name: [{section_name}]")
            continue
        cfg = parser[section_name]
        local_dir = cfg.get("local_dir", fallback=f"./{site_name}")
        sites[site_name] = {
            "name": site_name,
            "host": cfg.get("host", fallback=None),
            "port": cfg.getint("port", fallback=21),
            "user": cfg.get("user", fallback=None),
            "password": (
                os.environ.get(f"FTP_PASSWORD_{site_name.upper()}")
                or cfg.get("password", fallback=None)
                or None
            ),
            "remote_root": "/" + cfg.get("remote_root", fallback="/").strip("/"),
            "local_dir": local_dir,
            "snapshot_dir": cfg.get("snapshot_dir", fallback=str(Path(local_dir) / ".snapshots")),
            "use_tls": cfg.getboolean("use_tls", fallback=False),
            "encoding": cfg.get("encoding", fallback="latin-1"),
        }

    if not sites:
        print(f"No [{SITE_PREFIX}NAME] sections found in {path}.")
        sys.exit(1)

    return {"db_path": db_path, "sites": sites}


#--------------------------------------------------------------------------
def init_db(db_path):
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS files (
            site TEXT NOT NULL,
            path TEXT NOT NULL,
            hash TEXT NOT NULL,
            size INTEGER NOT NULL,
            first_seen TEXT NOT NULL,
            last_changed TEXT NOT NULL,
            last_checked TEXT NOT NULL,
            PRIMARY KEY (site, path)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS change_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            site TEXT NOT NULL,
            path TEXT NOT NULL,
            change_type TEXT NOT NULL,      -- 'new' | 'modified' | 'deleted'
            old_hash TEXT,
            new_hash TEXT,
            size INTEGER,
            changed_at TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


#--------------------------------------------------------------------------
def connect_ftp(site_cfg):
    ftp = ftplib.FTP_TLS() if site_cfg["use_tls"] else ftplib.FTP()
    # Python 3.9+ defaults ftplib to strict UTF-8, which raises
    # UnicodeDecodeError on servers that send directory listings in
    # Latin-1 or another 8-bit encoding. Latin-1 can decode any byte
    # sequence without error and round-trips back to the same bytes,
    # so it's a safe default even if it doesn't perfectly render
    # non-ASCII filenames.
    ftp.encoding = site_cfg["encoding"]
    ftp.connect(site_cfg["host"], site_cfg["port"], timeout=30)
    ftp.login(site_cfg["user"], site_cfg["password"])
    if site_cfg["use_tls"]:
        ftp.prot_p()
    return ftp


def list_remote_files(ftp, root):
    """Recursively list every file (not directory) under root."""
    files = []
    _walk(ftp, root, files)
    return files

index=0
def step_spinner():
    """Displays | \\ - / sequence, overwriting previous character on each call."""
    global index
    frames = ['|', '\\', '-', '/']
    print(f"{frames[index % 4]}\b", end='', flush=True)
    index += 1

def hide_spinner():
    # print(f"\b ", end="", flush=True)
    pass

def _walk(ftp, path, files):
    """ Recursivly walk a directory tree to build a list of files."""
    # Prefer MLSD (structured, gives file/dir type reliably).
    try:
        entries = list(ftp.mlsd(path))
    except (ftplib.error_perm, AttributeError):
        entries = None

    if entries is not None:
        for name, facts in entries:
            step_spinner()
            if name in (".", ".."):
                continue
            full = f"{path.rstrip('/')}/{name}"
            entry_type = facts.get("type")
            if entry_type == "dir":
                _walk(ftp, full, files)
            elif entry_type == "file":
                files.append(full)
        hide_spinner()
        return

    # Fallback for servers that don't support MLSD: use NLST and probe
    # each entry by trying to CWD into it.
    original = ftp.pwd()
    try:
        ftp.cwd(path)
    except ftplib.error_perm:
        # Don't have permissions so end.
        return

    names = ftp.nlst()
    for name in names:
        step_spinner()
        if name in (".", ".."):
            continue
        full = f"{path.rstrip('/')}/{name}"
        try:
            ftp.cwd(full)
            ftp.cwd(original)
            _walk(ftp, full, files)
        except ftplib.error_perm:
            files.append(full)
    ftp.cwd(original)
    hide_spinner()


def download_and_hash(ftp, remote_path, tmp_path):
    """Download and hash a file from FTP server."""
    sha256 = hashlib.sha256()
    size = 0
    with open(tmp_path, "wb") as f:
        def writer(data):
            nonlocal size
            f.write(data)
            sha256.update(data)
            size += len(data)
        ftp.retrbinary(f"RETR {remote_path}", writer)
    return sha256.hexdigest(), size


def snapshot_tree(local_root, snapshot_root, site, timestamp):
    """
    Archive the entire current local copy into one dated .tar.gz under
    the site's snapshot directory, before any changes from this run are
    applied. Restoring a whole tree is then just extracting that one
    archive, rather than reassembling many individually versioned files.

    No-op if the local copy doesn't exist yet or is empty (e.g. the
    very first run for a site, or a snapshot directory nested inside an
    otherwise-empty local_dir).
    """
    if not local_root.exists():
        return None

    has_content = any(
        p for p in local_root.rglob("*")
        if snapshot_root not in p.parents and p != snapshot_root
    )
    if not has_content:
        return None

    snapshot_root = Path(snapshot_root)
    snapshot_root.mkdir(parents=True, exist_ok=True)
    archive_path = snapshot_root / f"{site}-{timestamp}.tar.gz"

    with tarfile.open(archive_path, "w:gz") as tar:
        for item in local_root.iterdir():
            if item == snapshot_root or snapshot_root in item.parents:
                continue
            tar.add(item, arcname=item.name)

    return archive_path


def sync_site(site_cfg, conn, dry_run=False):
    """Run the sync for a single site using an already-open shared DB connection."""
    site = site_cfg["name"]
    cur = conn.cursor()

    # Make sure the local directory exists.
    local_root = Path(site_cfg["local_dir"])
    local_root.mkdir(parents=True, exist_ok=True)
    snapshot_root = Path(site_cfg["snapshot_dir"])

    now = datetime.now(timezone.utc).isoformat()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")

    # Snapshot the whole tree as it currently stands, before this run's
    # changes are applied, so it can be restored in one go if needed.
    if not dry_run:
        snapshot_path = snapshot_tree(local_root, snapshot_root, site, timestamp)
        if snapshot_path:
            print(f"[{site}] Snapshotted current copy to {snapshot_path}")

    # Connect to the host and get the full list of files.
    print(f"[{site}] Connecting to {site_cfg['host']} ...")
    ftp = connect_ftp(site_cfg)

    print(f"[{site}] Listing files under {site_cfg['remote_root']} ...")
    remote_files = list_remote_files(ftp, site_cfg["remote_root"])
    print(f"[{site}] Found {len(remote_files)} files on server.\n")

    # Go through each file looking for changes.
    seen_paths = set()
    new_count = modified_count = unchanged_count = error_count = 0
    for remote_path in remote_files:
        print(f"  [???] {remote_path}\033[K", end="", flush=True)
        seen_paths.add(remote_path)
        rel_path = remote_path[len(site_cfg["remote_root"]):].lstrip("/")
        local_path = local_root / rel_path

        # Fetch the file to a temporary file name and calculate the hash.
        fd, tmp_path = tempfile.mkstemp()
        os.close(fd)
        try:
            new_hash, size = download_and_hash(ftp, remote_path, tmp_path)
        except Exception as e:
            print(f"  ERROR downloading {remote_path}: {e}")
            os.unlink(tmp_path)
            error_count += 1
            continue

        # Find the file in the DB and workout if the file is new/changed.
        cur.execute("SELECT hash FROM files WHERE site = ? AND path = ?", (site, remote_path))
        db_rec = cur.fetchone()
        if db_rec is None:
            change_type = "new"
        elif db_rec[0] != new_hash:
            change_type = "modified"
        else:
            change_type = None

        if change_type:
            old_hash = db_rec[0] if db_rec else None
            print(f"\r  [{change_type.upper()}] {remote_path}")
            if not dry_run:
                local_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(tmp_path, local_path)
                cur.execute("""
                    INSERT INTO files (site, path, hash, size, first_seen, last_changed, last_checked)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(site, path) DO UPDATE SET
                        hash=excluded.hash,
                        size=excluded.size,
                        last_changed=excluded.last_changed,
                        last_checked=excluded.last_checked
                """, (site, remote_path, new_hash, size,
                      now if db_rec is None else now, now, now))
                cur.execute("""
                    INSERT INTO change_log (site, path, change_type, old_hash, new_hash, size, changed_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (site, remote_path, change_type, old_hash, new_hash, size, now))
            else:
                os.unlink(tmp_path)
            if change_type == "new":
                new_count += 1
            else:
                modified_count += 1
        else:
            os.unlink(tmp_path)
            if not dry_run:
                cur.execute("UPDATE files SET last_checked = ? WHERE site = ? AND path = ?", (now, site, remote_path))
            unchanged_count += 1
            print("\r", end="")

    # Anything in the DB for this site that wasn't seen this run has been
    # deleted server-side.
    cur.execute("SELECT path, hash FROM files WHERE site = ?", (site,))
    deleted = [r for r in cur.fetchall() if r[0] not in seen_paths]
    for path, old_hash in deleted:
        print(f"  [DELETED] {path}")
        if not dry_run:
            rel_path = path[len(site_cfg["remote_root"]):].lstrip("/")
            local_path = local_root / rel_path
            if local_path.exists():
                local_path.unlink()
            cur.execute("""
                INSERT INTO change_log (site, path, change_type, old_hash, new_hash, size, changed_at)
                VALUES (?, ?, 'deleted', ?, NULL, NULL, ?)
            """, (site, path, old_hash, now))
            cur.execute("DELETE FROM files WHERE site = ? AND path = ?", (site, path))

    if not dry_run:
        conn.commit()
    ftp.quit()
    print("\033[K", end="", flush=True)

    print(f"\n[{site}] Summary:")
    print(f"  New:       {new_count}")
    print(f"  Modified:  {modified_count}")
    print(f"  Deleted:   {len(deleted)}")
    print(f"  Unchanged: {unchanged_count}")
    if error_count:
        print(f"  Errors:    {error_count}")
    if dry_run:
        print("  (dry run - nothing was downloaded, snapshotted, or written to the database)")
    elif new_count+modified_count+len(deleted)==0:
        # Nothing has changed so remove the snapshot
        if snapshot_path:
            print("Snapshot not needed.  Removing...")
            os.remove(snapshot_path)

    print()


def main():
    parser = argparse.ArgumentParser(
        description="Sync changed files from one or more FTP sites, snapshot the prior tree "
                     "state, and log changes to a shared SQLite database."
    )
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="Path to config.ini (default: ./config.ini)")
    parser.add_argument("--dry-run", action="store_true",
                         help="Report what changed without downloading, snapshotting, or writing to the database")
    parser.add_argument("--site", action="append", metavar="NAME",
                         help="Only sync this site (may be given multiple times). Default: all sites in config.")
    args = parser.parse_args()

    cfg = load_config(args.config)
    all_sites = cfg["sites"]

    if args.site:
        unknown = [s for s in args.site if s not in all_sites]
        if unknown:
            print(f"Unknown site(s) in --site: {', '.join(unknown)}")
            print(f"Sites defined in config: {', '.join(all_sites)}")
            sys.exit(1)
        selected = {name: all_sites[name] for name in args.site}
    else:
        selected = all_sites

    conn = init_db(cfg["db_path"])
    failed_sites = []
    try:
        for site_name, site_cfg in selected.items():
            if not site_cfg["host"] or not site_cfg["user"]:
                print(f"[{site_name}] Skipping: config is missing 'host' or 'user'.")
                failed_sites.append(site_name)
                continue
            if not site_cfg["password"]:
                site_cfg["password"] = getpass.getpass(
                    f"FTP password for {site_cfg['user']}@{site_cfg['host']} ({site_name}): "
                )
            try:
                sync_site(site_cfg, conn, dry_run=args.dry_run)
            except Exception as e:
                print(f"[{site_name}] ERROR: sync failed: {e}\n")
                failed_sites.append(site_name)
    finally:
        conn.close()

    if failed_sites:
        print(f"Completed with errors on: {', '.join(failed_sites)}")
        sys.exit(1)

#-----------------------------------------------------------------------------
if __name__ == "__main__":
    main()
