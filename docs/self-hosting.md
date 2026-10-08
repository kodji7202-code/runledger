# Self-hosting the RunLedger team server

The team server collects receipts that developers push with `runledger push`, stores them in one
SQLite database, and serves a shared dashboard, the approval API, team budgets and compliance exports.
This guide covers three setups:

- **Docker Compose with HTTPS** (recommended): the server, plus Caddy, which gets and renews the
  certificate.
- **Bare metal**: a Python virtual environment, a systemd service, and nginx as the HTTPS proxy.
- **Built-in TLS**: the server serves HTTPS itself, with no proxy in front. See
  [Built-in TLS](#built-in-tls-no-reverse-proxy).

The server uses only the Python standard library and SQLite. There is no other database and no other
service to run. For roles, keys, the audit log and the admin features, see [enterprise.md](enterprise.md).
For every endpoint, see [api.md](api.md).

## Requirements

- A host with a public address, and a domain name whose DNS record points at it. For HTTPS with
  Caddy or nginx, ports **80 and 443** must be reachable from the internet, because Let's Encrypt needs
  them.
- For Docker: Docker Engine with Compose v2 (`docker compose version`).
- For bare metal: Python 3.9 or later with `venv`, systemd, and nginx.
- For built-in TLS: a certificate and private key in PEM format.
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

   The team's first key is an admin key labelled `initial`. It is printed once. Save it in a password
   manager. The server stores only a hash of it, so it cannot show the key again.

6. Open the dashboard once with the key: `https://runledger.example.com/?key=YOUR_KEY`. The
   server sets a session cookie and redirects, so the key leaves the address bar. After that,
   open the plain address.

7. Push a run from a developer's machine, with a member or admin key:

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

### Trusted proxy on the Compose network (recommended)

Caddy reaches the server over the Compose network, so the server sees Caddy's address as the client.
Until the server is told that Caddy is a trusted proxy:

- every user shares one failed-sign-in counter with Caddy's address, so a few bad keys from anyone can
  lock out everyone for up to five minutes (`429`);
- the audit log records Caddy's address instead of the user's.

Name the Compose network as a trusted proxy:

1. After the stack is running, find the subnet of the network. Compose names it `<project>_default`,
   which is `deploy_default` in this folder:

   ```bash
   docker network inspect deploy_default --format '{{range .IPAM.Config}}{{.Subnet}}{{end}}'
   ```

2. Tell the server the subnet. The simplest way is the environment of the `runledger` service in
   `deploy/docker-compose.yml`, which needs no change to the command:

   ```yaml
   environment:
     RUNLEDGER_PUBLIC_URL: ${RUNLEDGER_PUBLIC_URL:?set RUNLEDGER_PUBLIC_URL in deploy/.env}
     RUNLEDGER_TRUSTED_PROXIES: "172.20.0.0/16"   # the subnet from step 1
   ```

   The equivalent flag is `--trusted-proxy 172.20.0.0/16` in the server command (the `CMD` in the
   `Dockerfile`, or a `command:` in the compose file).

3. Apply the change with `docker compose up -d`. The server prints
   `Trusting X-Forwarded-For from: 172.20.0.0/16` when it starts.

Docker assigns subnets, and a network that is recreated can get a different one. Check the subnet again
after you change the network, and pin it in the compose file if it must stay the same. Only the Caddy
container should be on this network, which is the case by default.

With a trusted proxy named, the server takes the client address from `X-Forwarded-For`, and it honours
`X-Forwarded-Proto` (set by Caddy) only from that proxy. The `Dockerfile` command also passes
`--trust-proxy`, so the HTTPS cookie flags work through Caddy.

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
   sudo /opt/runledger/venv/bin/python -m pip install runledger-ai
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
   ExecStart=/opt/runledger/venv/bin/runledger serve --host 127.0.0.1 --port 8787 --db /var/lib/runledger/runledger.db --trust-proxy --trusted-proxy 127.0.0.1
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

   The server listens on `127.0.0.1` only, and nginx is the only thing that reaches it. nginx connects
   from `127.0.0.1`, so the unit names `127.0.0.1` as the trusted proxy.

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

   Run `sudo nginx -t && sudo systemctl reload nginx`.

   The server reads `X-Forwarded-Proto` only from the trusted proxy (`--trusted-proxy 127.0.0.1` in the
   unit above), and it reads `X-Forwarded-For` from the same proxy. `--trust-proxy` on its own honours
   `X-Forwarded-Proto` from any address, so keep the trusted-proxy option with it, and keep port 8787
   reachable only from the proxy.

## Built-in TLS (no reverse proxy)

The server can serve HTTPS itself. Give it a PEM certificate and its private key:

```bash
runledger serve --host 0.0.0.0 --port 8443 --db /var/lib/runledger/runledger.db \
  --tls-cert /etc/ssl/runledger/fullchain.pem --tls-key /etc/ssl/runledger/privkey.pem
```

- The server accepts TLS 1.2 and later. `--tls-cert` and `--tls-key` must be given together, and the
  server refuses to start if a file is missing or cannot be loaded.
- With TLS on, the session cookie is `Secure` and the server sends `Strict-Transport-Security`.
- The certificate is read when the server starts. After you renew it, restart the server.
- Plain HTTP connections to the TLS port get no response.
- To make the server reachable from the internet, open only the TLS port in the firewall. If a load
  balancer or proxy is already in front, use the proxy setup above instead.

## Teams, roles and keys

Create a team, and its first admin key, with:

```bash
runledger team create NAME --db /path/to/runledger.db
```

The command prints the team's id and its key. The key is printed once. The database stores only its
SHA-256 hash.

Each key has one role. Viewers read everything; members also push runs and handle approvals; admins also
manage team settings, keys, the audit log, budgets and exports. The full matrix is in
[enterprise.md](enterprise.md#model-teams-keys-and-roles).

Keys are managed with these commands. They act on the database file directly, so run them where the
database lives. They do not need the server to be running, and they do not check roles.

```bash
runledger key list   --team-id 1 --db /path/to/runledger.db
runledger key create --team-id 1 --label "ci-nightly" --role member --db /path/to/runledger.db
runledger key rotate <key id> --db /path/to/runledger.db    # new secret; the old one stops working at once
runledger key revoke <key id> --db /path/to/runledger.db    # permanent; its dashboard sessions end at once
```

The same actions are available over the API to admins (`/api/keys`, see [api.md](api.md)).
A team's last active admin key cannot be revoked, and a revoked key cannot be rotated.

Keys do not expire. Rotate a key when a person leaves or when a key may have leaked.

- Team settings (webhook URLs and the approval time limit) are read with `GET /api/team/settings`, which
  masks the URLs for non-admins, and changed with `PUT /api/team/settings` by an admin.
- Dashboard sessions last 12 hours. They are stored in the database as hashes, so a server restart does
  not sign anyone out. A session ends when its key is revoked or rotated.

## Configuration

| Setting | Where | Meaning |
| --- | --- | --- |
| `--host` | `runledger serve` | Address to listen on. Default `127.0.0.1`. |
| `--port` | `runledger serve` | Port. Default `8787`. |
| `--db` | `runledger serve`, `team create`, `key ...` | Path to the SQLite file. Default `runledger.db`. |
| `--tls-cert FILE`, `--tls-key FILE` | `runledger serve` | PEM certificate and key. The server then serves HTTPS (TLS 1.2 or later). |
| `--secure-cookies`, or `RUNLEDGER_SECURE_COOKIES=1` | `runledger serve`, environment | Sets the `Secure` flag on the dashboard cookie and sends HSTS. |
| `--trust-proxy` | `runledger serve` | Honours `X-Forwarded-Proto: https` for the `Secure` flag and HSTS. Only from the `--trusted-proxy` addresses when any are given; otherwise from any address. |
| `--trusted-proxy CIDR` (repeatable), or `RUNLEDGER_TRUSTED_PROXIES` (comma-separated) | `runledger serve`, environment | The addresses or ranges of your reverse proxies, for example `127.0.0.1` or `172.20.0.0/16`. The client address is read from `X-Forwarded-For` only for these peers. It is used for the failed-sign-in limit and the audit log. |
| `RUNLEDGER_PUBLIC_URL` | environment | Public base URL used in approval links. Set it whenever the server is behind a proxy. |

The database is upgraded automatically when the server starts. Missing columns are added, so an old
database keeps working. A database from before roles existed keeps its one key as an admin key labelled
`initial`.

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
including keys' hashes, prompts, file paths and webhook URLs. Test a restore before you need one.

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
      sign-in. Plain HTTP exposes them. Use a proxy or the built-in TLS. Redirect port 80 to HTTPS
      (both proxy setups above do this).
- [ ] **Keep the server off the internet except through HTTPS.** In Compose it is only reachable from
      Caddy. On bare metal it listens on `127.0.0.1`. Open only the HTTPS port in the firewall.
- [ ] **Name your proxy as a trusted proxy.** With a proxy in front, set `--trusted-proxy` or
      `RUNLEDGER_TRUSTED_PROXIES` to the proxy's address or subnet. Without it, the failed-sign-in limit
      and the audit log use the proxy's address, and `--trust-proxy` honours the forwarded protocol from
      any address.
- [ ] **Give each person or system their own key, with the lowest role that works.** Viewers can read,
      members can push and handle approvals, and only admins can manage keys and settings.
- [ ] **Rotate and revoke keys.** Rotate when a person leaves or a key may have leaked, and revoke the
      keys you no longer use. Keys do not expire on their own.
- [ ] **Protect the database file.** The `team` and `key` commands act on it directly, without roles.
      Whoever can write the file can create an admin key.
- [ ] **Treat team keys as passwords.** Keep them out of Git, CI logs, and shell history. Pass
      them in environment variables (`RUNLEDGER_API_KEY`), not in command arguments.
- [ ] **Know what receipts contain.** A receipt holds the prompts (the first three in full), file paths,
      the first line of each command (up to 120 characters), search patterns, URLs, test counts, models,
      cost and risk reasons. It does not hold file contents or command output. Receipts are not redacted,
      so a secret typed on a command line can appear in one. See the privacy note in
      [docs/github.md](github.md).
- [ ] **Limit the webhook targets.** Approval and budget notifications go to the URLs you set in the team
      settings. Use only URLs you control. The URLs are stored as set in the database.
- [ ] **Review the audit log, and protect exports.** Admins can read the audit log and download the CSV
      and HTML exports. They contain developer and project names, run titles (the first request, up to 200
      characters), risk findings, approval decisions and audit details. Treat them as confidential. They are
      records, not a compliance certification.
- [ ] **Know that there is no retention policy.** Runs stay until the database is removed. To
      erase data, stop the server, back up what you need, and delete the database file and its
      `-wal` and `-shm` files. This removes every team.
- [ ] **Keep the host and the images updated.** Rebuild the Docker image with `--pull` when a new
      base image is released. The container runs as the unprivileged `runledger` user.
- [ ] **Back up regularly, and test the restore.**

## Known limits in 0.2.0

- No single sign-on, no per-person accounts, no multi-factor authentication, and no key expiry.
- Runs and audit events are never deleted automatically. There is no delete or retention command.
- One server process and one SQLite file. This is not a multi-node cluster.
- There is no sign-out endpoint. Sessions end when their key is revoked or rotated, or after 12 hours.
- Receipts are not redacted. The guard's log masks known token formats and quoted secret values only.
- The failed-sign-in limit counts the connection address, unless trusted proxies are configured. Its
  counter is held in memory, so a restart clears it.

## Troubleshooting

- **`cannot listen on ...: Address already in use`**: another process has the port. Change `--port`
  or stop the other process.
- **`--tls-cert and --tls-key must be given together`, or `file not found`**: give both options, with
  files that exist and that the server can read.
- **The container exits at once**: run `docker compose logs runledger`. An unrecognised argument
  means the image and the command line disagree; rebuild with `docker compose build --pull`.
- **Caddy cannot get a certificate**: check the DNS record, and that ports 80 and 443 reach the
  host. The logs are in `docker compose logs caddy`.
- **Everyone gets `429 rate_limited`**: one address sent more than 20 failed credentials in five
  minutes. Wait for the `Retry-After` time. If the server is behind a proxy, set the proxy as a trusted
  proxy (see above) so that the limit counts each client.
- **The dashboard asks you to sign in**: open `https://YOUR_DOMAIN/?key=YOUR_KEY` once, as in
  step 6 of the quickstart. Sessions last 12 hours.
- **`runledger push` says there is no server or key**: set `RUNLEDGER_SERVER` and
  `RUNLEDGER_API_KEY`, or pass `--server` and `--key`.
- **`runledger push` answers `403`**: the key has the viewer role. Use a member or admin key.
