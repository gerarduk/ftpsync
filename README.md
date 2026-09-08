# ftp_sync on Raspberry Pi + OpenMediaVault

## What's in this bundle

- `ftp_sync.py` - the sync program (multi-site, versioned snapshots).
- `config.ini.example` - starter config, copied in automatically on install.
- `install.sh` - sets everything up: venv, daily cron job, log rotation.

## Prerequisites

On the Pi (Raspbian/Debian under OMV normally has these already):

```
sudo apt update
sudo apt install python3 python3-venv
```

## Find your OMV shared folder path

In the OMV web UI: **Storage > Shared Folders**, pick (or create) the folder
you want the mirror to live in, and note its **Path** column - it'll look
like `/srv/dev-disk-by-id-XXXXXXXX/ftp-mirror`. That's what config, the
SQLite database, snapshots, and the mirrored files themselves will live
under - keeping them on your storage array rather than the SD card.

## Install

```
sudo ./install.sh /srv/dev-disk-by-id-XXXXXXXX/ftp-mirror
```

This will:
- Copy `ftp_sync.py` into `/opt/ftp_sync` and create a venv there.
- Create the shared-folder path above with a `config.ini` (only written if
  one doesn't already exist there) and a `logs/` subfolder.
- Install `/etc/cron.d/ftp_sync`, running the sync daily at 02:00, appending
  output to `<shared_folder>/logs/ftp_sync.log`.
- Install `/etc/logrotate.d/ftp_sync`, rotating that log weekly and keeping
  8 weeks of history, compressed.
- Own everything as whichever user you ran `sudo` as (so ordinary OMV/SSH
  user, not root, unless you installed as root directly).

Re-running `install.sh` later (e.g. after updating `ftp_sync.py`) is safe -
it refreshes the script and venv but never overwrites an existing
`config.ini`.

## Configure

Edit the config it created:

```
sudo nano /srv/dev-disk-by-id-XXXXXXXX/ftp-mirror/config.ini
```

Add a `[site:NAME]` section per FTP server (see the comments in the file).
**Cron jobs have no terminal**, so `ftp_sync.py`'s interactive password
prompt will never fire under cron - put each site's password directly in
`config.ini` (it's created at file mode `600`, readable only by the owning
user) rather than relying on the prompt.

## Test before trusting the cron job

```
sudo -u <your-user> /opt/ftp_sync/venv/bin/python /opt/ftp_sync/ftp_sync.py \
    --config /srv/dev-disk-by-id-XXXXXXXX/ftp-mirror/config.ini --dry-run
```

Drop `--dry-run` once it looks right. Add `--site NAME` to test just one
site.

## Changing the schedule

Edit `/etc/cron.d/ftp_sync` directly - standard 5-field cron syntax, with
the username as the 6th field before the command. It defaults to:

```
0 2 * * * <user> /opt/ftp_sync/venv/bin/python ...
```

## Where everything ends up

```
/opt/ftp_sync/                                  # program + venv (SD card)
/srv/.../ftp-mirror/
├── config.ini                                  # your sites (mode 600)
├── ftp_changes.db                               # shared change-tracking DB
├── logs/ftp_sync.log                            # cron output, log-rotated
└── mirror/<site>/
    ├── ...current mirrored files...
    └── .snapshots/<site>-<timestamp>.tar.gz     # full-tree snapshot per run
```

## Uninstalling

```
sudo rm -rf /opt/ftp_sync /etc/cron.d/ftp_sync /etc/logrotate.d/ftp_sync
```

Your `config.ini`, database, and mirrored files under the shared folder are
left untouched - remove that folder yourself if you want it gone too.
