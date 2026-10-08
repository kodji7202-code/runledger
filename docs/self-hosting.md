# Self-hosting the RunLedger team server

The team server collects receipts that developers push with `runledger push`, stores them
in one SQLite database, and serves a shared dashboard and the approval API. This guide covers
two setups:

- **Docker Compose with HTTPS** (recommended): the server, plus Caddy, which gets and renews
  the certificate.
- **Bare metal**: a Python virtual environment, a systemd service, and nginx as the HTTPS
  proxy.

The server uses only the Python standard library and SQLite. There is no other database and
no other service to run.

## Requirements

- A host with a public address, and a domain name whose DNS record points at it. For HTTPS,
  ports **80 and 443** must be reachable from the internet, because Caddy and Let's Encrypt
  need them.
- For Docker: Docker Engine with Compose v2 (`docker compose version`).
- For bare metal: Python 3.9 or later with `venv`, systemd, and nginx.
- Disk space for the database. It grows with every pushed receipt.

## Quickstart: Docker Compose with HTTPS

Run these commands on the host, from the repository's `deploy` folder.

1. Copy the environment file and set your domain:

   ```bash
   cd deploy
   cp .env.example .env
   ```

   In `.env`, set:

   ```
   RUNLEDGER_DOMAIN=runledger.example.com
   RUNLEDGER_PUBLIC_URL=https://runledger.example.com
   ```

   `RUNLEDGER_PUBLIC_URL` is the address people use. It appears in approval links.

2. Point the DNS record for `RUNLEDGER_DOMAIN` at the host. Wait until it resolves
   (`dig +short runledger.example.com` shows the host's address).

3. Start the stack:

   ```bash
   docker compose up -d --build
   ```

   Caddy requests the certificate on first start. Check progress with `docker compose logs -f caddy`.

4. Check the server:

   ```bash
   curl -fsS https://runledger.example.com/health
   ```

   The reply is `{"ok": true, "version": "..."}`.

5. Create the first team. The command runs in the container, and its working directory is the
   data volume, so the team is created in the same database the server uses:

   ```bash
   docker compose run --rm runledger team create myteam
   ```

   The API key is printed once. Save it in a password manager. The server stores only a hash
   of it, so it cannot show the key again.

6. Open the dashboard once with the key: `https://runledger.example.com/?key=YOUR_KEY`. The
   server sets a session cookie and redirects, so the key leaves the address bar. After that,
   open the plain address.

7. Push a run from a developer's machine:

   ```bash
   export RUNLEDGER_SERVER=https://runledger.example.com
   export RUNLEDGER_API_KEY=YOUR_KEY
   runledger push --user "Ana"
   ```

Day-to-day commands, from `deploy`:

```bash
docker compose ps                  # status
docker compose logs -f runledger   # server log (request lines have no query strings)
docker compose down                # stop; keeps the volumes and the data
```

Do not run `docker compose down -v` unless you want to delete the data volumes.

## Bare metal: pip, systemd and nginx

1. Create a service user and its data folder:

   ```bash
   sudo useradd --system --home-dir /var/lib/runledger --shell /usr/sbin/nologin runledger
   sudo install -d -o runledger -g runledger -m 750 /var/lib/runledger
   ```

2. Install RunLedger into a virtual environment. Use the released package when it is on PyPI,
   or a wheel or source folder until then:

   ```bash
   sudo python3 -m venv /opt/runledger/venv
   sudo /opt/runledger/venv/bin/python -m pip install runledger
   # until the package is on PyPI, install a wheel built from this repository:
   # sudo /opt/runledger/venv/bin/python -m pip install /path/to/runledger-<version>-py3-none-any.whl
   ```

3. Create `/etc/systemd/system/runledger.service`:

   ```ini
   [Unit]
   Description=RunLedger team server
   After=network-online.target
   Wants=network-online.target

   [Service]
   User=runledger
   Group=runledger
   WorkingDirectory=/var/lib/runledger
   Environment=RUNLEDGER_PUBLIC_URL=https://runledger.example.com
   ExecStart=/opt/runledger/venv/bin/runledger serve --host 127.0.0.1 --port 8787 --db /var/lib/runledger/runledger.db --trust-proxy
   Restart=on-failure
   NoNewPrivileges=true
   ProtectSystem=strict
   ReadWritePaths=/var/lib/runledger
   PrivateTmp=true

   [Install]
   WantedBy=multi-user.target
   ```

   Then start it:

   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable --now runledger
   sudo systemctl status runledger
   ```

   The server listens on `127.0.0.1` only. nginx is the only thing that reaches it.

4. Create the first team, as the service user, with the same database path:

   ```bash
   sudo -u runledger /opt/runledger/venv/bin/runledger team create myteam --db /var/lib/runledger/runledger.db
   ```

5. Put nginx in front of it, with a certificate for your domain (for example one issued with
   certbot). `/etc/nginx/conf.d/runledger.conf`:

   ```nginx
   server {
       listen 80;
       server_name runledger.example.com;
       return 301 https://$host$request_uri;
   }

   server {
       listen 443 ssl http2;
       server_name runledger.example.com;

       ssl_certificate     /etc/letsencrypt/live/runledger.example.com/fullchain.pem;
       ssl_certificate_key /etc/letsencrypt/live/runledger.example.com/privkey.pem;

       client_max_body_size 11m;   # the server accepts up to 10 MB per request

       location / {
           proxy_pass http://127.0.0.1:8787;
           proxy_set_header Host $host;
           proxy_set_header X-Real-IP $remote_addr;
           proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
           proxy_set_header X-Forwarded-Proto $scheme;
           proxy_read_timeout 90s;
       }
   }
   ```

   Run `sudo nginx -t && sudo systemctl reload nginx`. The `X-Forwarded-Proto` header tells
   the server that the client used HTTPS. `--trust-proxy` makes the server read that header, so
   only use it when nothing else can reach port 8787.

## Teams and keys

Each team has one API key. Create a team, and its key, with:

```bash
runledger team create NAME --db /path/to/runledger.db
```

- The key is printed once. The database stores only its SHA-256 hash.
- Every key can push runs, read every run of its team, use the dashboard, and approve or deny
  approvals for its team. Teams cannot see each other's runs.
- Team settings (webhook URLs and the approval timeout) are read and changed with
  `GET` and `PUT /api/team/settings`, using the team key.
- Dashboard sessions last 12 hours and are kept in memory. A restart signs everyone out.

This version has no `runledger key create` command and no roles. There are also no commands to
rotate or revoke a key (see the security checklist and the known limits below).

## Configuration

| Setting | Where | Meaning |
| --- | --- | --- |
| `--host` | `runledger serve` | Address to listen on. Default `127.0.0.1`. |
| `--port` | `runledger serve` | Port. Default `8787`. |
| `--db` | `runledger serve`, `team create` | Path to the SQLite file. Default `runledger.db`. |
| `--trust-proxy` | `runledger serve` | Trust the `X-Forwarded-*` headers from the reverse proxy. Use it only when the proxy is the only way in. |
| `RUNLEDGER_PUBLIC_URL` | environment | Public base URL used in approval links. Set it whenever the server is behind a proxy. |

The database is upgraded automatically when the server starts. Missing columns are added,
so an old database keeps working.

## Backups

The database runs in SQLite's WAL mode. Do not copy the `.db` file while the server is
running. The copy can miss recent writes, and the `-wal` file holds them. Use SQLite's backup
mechanism instead.

**Bare metal**, with the `sqlite3` command-line tool (`sudo apt install sqlite3`):

```bash
sudo sqlite3 /var/lib/runledger/runledger.db ".backup '/var/backups/runledger-$(date +%F).db'"
```

**Docker.** The image has no `sqlite3` command. Use Python's backup API inside the container,
then copy the file out:

```bash
docker compose exec runledger python -c "import sqlite3; src = sqlite3.connect('/data/runledger.db'); dst = sqlite3.connect('/data/backup.db'); src.backup(dst); dst.close(); src.close()"
docker compose cp runledger:/data/backup.db "./runledger-$(date +%F).db"
docker compose exec runledger rm /data/backup.db
```

**Whole volume.** For a complete copy, including the certificates in the Caddy volume, stop
the stack and archive the volumes. The volume names start with the project name, which is the
folder name (`deploy`). Check them with `docker volume ls`.

```bash
docker compose down
docker run --rm -v deploy_runledger-data:/data -v "$PWD":/backup alpine \
  tar czf /backup/runledger-data-$(date +%F).tgz -C /data .
docker compose up -d
```

Store backups off the host, encrypted. They contain the same data as the live database,
including prompts and file paths. Test a restore before you need one.

**Restore.** Stop the server. Replace `runledger.db` with the backup file, and delete any
`runledger.db-wal` and `runledger.db-shm` files that belong to the old database. Start the
server again.

## Upgrades

1. Make a backup (above).
2. Get the new version and rebuild.

   - Docker: `git pull`, then `cd deploy && docker compose build --pull && docker compose up -d`.
   - Bare metal: `sudo /opt/runledger/venv/bin/python -m pip install --upgrade runledger`, then
     `sudo systemctl restart runledger`.

3. Check `CHANGELOG.md` for changes that affect you. Before 1.0, a minor version can change
   behavior.

There is no supported way to go back to an older version on the same database. If you need to
roll back, install the older version and restore the backup from before the upgrade.

## Security checklist

- [ ] **Serve only over HTTPS.** Keys travel in the `Authorization` header and in the dashboard
      sign-in. Plain HTTP exposes them. Redirect port 80 to HTTPS (both setups above do this).
- [ ] **Keep port 8787 off the internet.** In Compose it is only reachable from Caddy. On bare
      metal it listens on `127.0.0.1`. Open only 80 and 443 in the firewall.
- [ ] **Treat team keys as passwords.** Keep them out of Git, CI logs, and shell history. Pass
      them in environment variables (`RUNLEDGER_API_KEY`), not in command arguments.
- [ ] **Plan for a leaked key.** This version cannot revoke or replace a key, so a leaked key
      stays valid until the database is changed by hand. Keep keys out of places they can leak
      from, and do not share one key between people who should not see each other's runs.
- [ ] **Assume every key holder can read the whole team.** The dashboard and the API show all
      runs of the team to any holder of its key, and anyone with the key can approve requests.
- [ ] **Review what developers push.** A receipt holds the prompts, the files the agent read or
      changed, command output, and the risk reasons. It can contain secrets, such as the contents
      of `.env` files. See the privacy note in [docs/github.md](github.md).
- [ ] **Know that there is no retention policy.** Runs stay until the database is removed. To
      erase data, stop the server, back up what you need, and delete the database file and its
      `-wal` and `-shm` files. This removes every team.
- [ ] **Limit the webhook targets.** Approval notifications are sent to the URLs you set in the
      team settings. Use only URLs you control.
- [ ] **Restrict network access to the proxy.** Use `--trust-proxy` only when the proxy is the
      only route to the server. Otherwise a client can send its own `X-Forwarded-*` headers.
- [ ] **Keep the host and the images updated.** Rebuild the Docker image with `--pull` when a new
      base image is released. The container runs as the unprivileged `runledger` user.
- [ ] **Back up regularly, and test the restore.**

## Known limits in 0.2.0

- Enterprise authentication is in progress. Today there are no per-user accounts, no roles, no
  key rotation, and no key revocation.
- Runs are never deleted automatically. There is no delete or retention command.
- One server process and one SQLite file. This is not a multi-node cluster.
- Dashboard sessions are held in memory, so a restart signs everyone out.
- The server does not terminate TLS. Use the Caddy or nginx setups above, or your own proxy.

## Troubleshooting

- **`cannot listen on ...: Address already in use`**: another process has the port. Change `--port`
  or stop the other process.
- **The container exits at once**: run `docker compose logs runledger`. An unrecognised argument
  means the image and the command line disagree; rebuild with `docker compose build --pull`.
- **Caddy cannot get a certificate**: check the DNS record, and that ports 80 and 443 reach the
  host. The logs are in `docker compose logs caddy`.
- **The dashboard asks you to sign in**: open `https://YOUR_DOMAIN/?key=YOUR_KEY` once, as in
  step 6 of the quickstart.
- **`runledger push` says there is no server or key**: set `RUNLEDGER_SERVER` and
  `RUNLEDGER_API_KEY`, or pass `--server` and `--key`.
