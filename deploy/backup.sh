#!/bin/sh
# Daily backup of the RunLedger database, for the host's crontab. From the deploy folder:
#
#   crontab -e
#   15 3 * * * cd /home/ubuntu/runledger/deploy && ./backup.sh >> backup.log 2>&1
#
# The copy is taken by the running server's image with SQLite's online backup, so the server
# keeps running. Copies land in the data volume under /data/backups (14 kept), and the newest
# is also copied to ./backups on the host. Copy that folder off the machine as well (see
# docs/self-hosting.md, "Backups").
#
# POSIX sh and POSIX tools only, so this also runs on busybox/Alpine, macOS and BSD hosts.
set -eu

keep=14

# The command prints the copy it wrote ("Backup: /data/backups/runledger-....db  (123 bytes;
# removed 0 older)"). Copy exactly that file, so two runs in the same second cannot copy the
# same file twice.
out=$(docker compose exec -T runledger runledger backup --db /data/runledger.db --dir /data/backups --keep "$keep")
latest=$(printf '%s\n' "$out" | tr -d '\r' | sed -n 's/^Backup: \([^ ][^ ]*\) .*/\1/p' | tail -n 1)
if [ -z "$latest" ]; then
    echo "backup.sh: could not read the copy's name from: $out" >&2
    exit 1
fi

# A name that is already taken gets a numeric suffix instead of being overwritten, which is what
# happens when the script runs twice within the same second.
mkdir -p backups
dest="backups/$(basename "$latest")"
n=1
while [ -e "$dest" ]; do
    dest="backups/$(basename "$latest" .db)-$n.db"
    n=$((n + 1))
done
docker compose cp "runledger:$latest" "$dest"

# Keep the newest $keep copies on the host too, with POSIX tools only: BSD and busybox versions of
# the common tools have no GNU extensions (no negative count for `head`, no empty-input flag for
# the delete helper).
ls -1t backups/runledger-*.db 2>/dev/null | tail -n +$((keep + 1)) | while read -r f; do
    rm -f -- "$f"
done
