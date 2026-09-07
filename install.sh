#!/usr/bin/env bash
#
# install.sh - Install ftp_sync.py on a Raspberry Pi running OpenMediaVault.
#
# What this does:
#   - Copies ftp_sync.py into an install directory and creates a venv for it.
#   - Creates a data directory (config.ini, database, mirrored files, logs)
#     under an OMV shared folder you specify - so it lives on your storage
#     array, not the SD card.
#   - Installs a daily cron job (/etc/cron.d/ftp_sync) that runs the sync
#     overnight, logging to <data_dir>/logs/ftp_sync.log.
#   - Installs a logrotate config (/etc/logrotate.d/ftp_sync) so that log
#     file doesn't grow forever.
#
# Usage (run as root, e.g. with sudo):
#   sudo ./install.sh /srv/dev-disk-by-id-XXXXXXXX/ftp-mirror
#
# The path argument is the OMV shared folder you want config/db/mirrored
# files to live under. Find it in the OMV web UI under
# Storage > Shared Folders (the "Path" column), or under /srv on the Pi.
# If you omit it, you'll be prompted for it.
#
# Re-running this script is safe: it won't overwrite an existing config.ini,
# and it replaces the cron/logrotate/venv/script files with the latest copy.

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
INSTALL_DIR="/opt/ftp_sync"
CRON_SCHEDULE="0 2 * * *"     # daily at 2:00 AM
CRON_FILE="/etc/cron.d/ftp_sync"
LOGROTATE_FILE="/etc/logrotate.d/ftp_sync"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

DATA_DIR="${1:-}"

# ---------------------------------------------------------------------------
# Sanity checks
# ---------------------------------------------------------------------------
if [[ $EUID -ne 0 ]]; then
    echo "This script needs root (it writes to /etc/cron.d, /etc/logrotate.d," >&2
    echo "and /opt). Re-run it with sudo:" >&2
    echo "  sudo $0 ${DATA_DIR}" >&2
    exit 1
fi

if [[ -z "$DATA_DIR" ]]; then
    read -rp "Path to the OMV shared folder for config/db/mirror/logs: " DATA_DIR
fi
if [[ -z "$DATA_DIR" ]]; then
    echo "A data directory path is required." >&2
    exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 not found. Install it first: sudo apt install python3" >&2
    exit 1
fi
if ! python3 -c "import venv" >/dev/null 2>&1; then
    echo "python3-venv not found. Install it first: sudo apt install python3-venv" >&2
    exit 1
fi

# The user the cron job and files will run/be owned as - whoever invoked
# sudo, falling back to root if run directly as root.
TARGET_USER="${SUDO_USER:-root}"
TARGET_GROUP="$(id -gn "$TARGET_USER")"

echo "Install directory : $INSTALL_DIR"
echo "Data directory     : $DATA_DIR"
echo "Runs as            : $TARGET_USER"
echo "Schedule           : $CRON_SCHEDULE (daily overnight)"
echo

# ---------------------------------------------------------------------------
# Install the program
# ---------------------------------------------------------------------------
mkdir -p "$INSTALL_DIR"
cp "$SCRIPT_DIR/ftp_sync.py" "$INSTALL_DIR/ftp_sync.py"

if [[ ! -d "$INSTALL_DIR/venv" ]]; then
    echo "Creating virtual environment ..."
    python3 -m venv "$INSTALL_DIR/venv"
fi
# ftp_sync.py only uses the Python standard library today, so there's
# nothing to pip install - the venv just keeps this isolated and ready
# for any future dependency without touching system Python packages.

# ---------------------------------------------------------------------------
# Set up the data directory (config, database, mirror, logs)
# ---------------------------------------------------------------------------
mkdir -p "$DATA_DIR" "$DATA_DIR/logs"

if [[ ! -f "$DATA_DIR/config.ini" ]]; then
    cp "$SCRIPT_DIR/config.ini.example" "$DATA_DIR/config.ini"
    echo "Wrote a starter config to $DATA_DIR/config.ini - edit it before the"
    echo "first run (host/user/password/remote_root/local_dir per site)."
else
    echo "Existing $DATA_DIR/config.ini left untouched."
fi

# config.ini can contain FTP passwords in plain text, so keep it private.
chmod 600 "$DATA_DIR/config.ini"
chown -R "$TARGET_USER:$TARGET_GROUP" "$INSTALL_DIR" "$DATA_DIR"

# ---------------------------------------------------------------------------
# Cron job
#
# cron jobs have no TTY, so ftp_sync.py's interactive password prompt will
# never fire - passwords for each site must be set in config.ini (kept at
# mode 600 above) or as FTP_PASSWORD_<SITE> environment variables added to
# this cron file.
# ---------------------------------------------------------------------------
cat > "$CRON_FILE" <<EOF
# Managed by ftp_sync's install.sh - edit and re-run install.sh to change.
# Runs the FTP sync daily and appends output to the log file.
$CRON_SCHEDULE $TARGET_USER $INSTALL_DIR/venv/bin/python $INSTALL_DIR/ftp_sync.py --config $DATA_DIR/config.ini >> $DATA_DIR/logs/ftp_sync.log 2>&1
EOF
chmod 644 "$CRON_FILE"

# ---------------------------------------------------------------------------
# Log rotation
# ---------------------------------------------------------------------------
cat > "$LOGROTATE_FILE" <<EOF
$DATA_DIR/logs/ftp_sync.log {
    weekly
    rotate 8
    compress
    missingok
    notifempty
    su $TARGET_USER $TARGET_GROUP
}
EOF

echo
echo "Done."
echo
echo "Next steps:"
echo "  1. Edit $DATA_DIR/config.ini with your FTP site(s)."
echo "  2. Test it by hand before trusting the cron job:"
echo "       sudo -u $TARGET_USER $INSTALL_DIR/venv/bin/python $INSTALL_DIR/ftp_sync.py --config $DATA_DIR/config.ini --dry-run"
echo "  3. The cron job runs daily at 02:00. Logs land in $DATA_DIR/logs/ftp_sync.log."
echo "  4. To change the schedule, edit $CRON_FILE directly (standard 5-field cron syntax, with the username in the 6th field)."
