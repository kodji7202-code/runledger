#!/bin/sh
# Daily backup of the RunLedger database, for the host's crontab. From the deploy folder:
#
#   crontab -e
#   15 3 * * * cd /home/ubuntu/runledger/deploy && ./backup.sh >> backup.log 2>&1
#
# The copy is taken by the running server's image with SQLite's online backup, so the server
# keeps running. Copies land in the data volume under /data/backups (14 kept), and the newest
# is also copied to ./backups on the host. Copy that folder off the machine as well (see
# docs/hosted.md, "Backups").
set -eu

docker compose exec -T runledger runledger backup --db /data/runledger.db --dir /data/backups --keep 14
mkdir -p backups
latest=$(docker compose exec -T runledger sh -c 'ls -1 /data/backups/runledger-*.db | tail -n 1' | tr -d '\r')
docker compose cp "runledger:$latest" "backups/$(basename "$latest")"
# Keep 14 copies on the host too.
ls -1 backups/runledger-*.db | head -n -14 | xargs -r rm --
