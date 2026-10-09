# Running the hosted Team plan

This is the operator runbook for the paid, hosted RunLedger Team server (`app.runledger.site`).
It is the same server as [self-hosting.md](self-hosting.md), with billing turned on:

- **Payments.** [Polar](https://polar.sh) is the merchant of record. It runs checkout, takes the
  payment, handles sales tax and VAT, sends invoices and hosts the customer portal (card, seats,
  cancellation).
- **Provisioning.** Polar calls the server's webhook. A paid subscription creates a team and an admin
  key. The key is emailed to the customer through [Resend](https://resend.com).
- **Access.** The subscription's state decides what the team can do (see "Lifecycle" below).
- **Data.** History is kept for 90 days (`RUNLEDGER_RETENTION_DAYS=90`). A team is deleted 30 days
  after its subscription ends.

Teams you create by hand with `runledger team create` are not billed and have no limits. Use them for
your own team and for demos.

## Lifecycle

| Polar subscription | Team |
| --- | --- |
| `incomplete` (first payment pending) | No team yet. |
| `active` | Team created on the first event, admin key emailed. Full access. |
| `active` with `cancel_at_period_end` | Full access until the period ends. The dashboard shows the end date. |
| `past_due` | Full access for 7 days, and the customer gets an email. Then read only until the payment succeeds. |
| `canceled`, `unpaid` (ended) | Read only: the dashboard, API reads and exports work; pushes and approval requests get HTTP 402. The customer gets an email with the deletion date. |
| 30 days after it ended | The team, its keys, runs, approvals and audit log are deleted. The subscription row stays, without the email. |

**Seats.** A seat is attributed to the authenticated API key that pushed a run in the last 30 days,
not to the editable `user` string in the receipt. When all seats are taken, an additional key's first
push gets HTTP 402. Teams should assign a separate key to each developer; shared keys cannot prove
separate human identities, because this release has no individual sign-in or SSO. Receipt `user` remains
a reporting label. The customer adds seats in the Polar portal; Polar sends `subscription.updated` and
the new limit applies at once.

**Lost key.** `https://app.runledger.site/recover` emails a new admin key to the subscription's email,
at most once every 10 minutes per subscription, and five requests per hour per address. The answer is
the same for unknown emails. If an admin key already occupied a paid seat, the recovery key inherits
that seat without rewriting its historical runs. Other existing keys remain valid, but repeated
recoveries rotate the same recovery key rather than allowing unlimited active recovery aliases.
If no admin key occupied a seat and all seats belong to other keys, the recovered admin can manage
the team but must free an occupied seat by expiry or add seats before pushing a run.

**Events.** The server verifies the Standard Webhooks signature (`webhook-id`, `webhook-timestamp`,
`webhook-signature`, five minutes of clock tolerance), applies each `webhook-id` once, and ignores events
older than the stored state. If the welcome email cannot be sent, the webhook answers 503 so Polar
delivers it again; the retry rotates the key, so the customer only ever receives a working one. Events
other than `subscription.*` are acknowledged and ignored.

## Settings

All in `deploy/.env` (see `deploy/.env.example`). Keep the file private: `chmod 600 deploy/.env`.

| Variable | Value |
| --- | --- |
| `RUNLEDGER_DOMAIN` | `app.runledger.site` |
| `RUNLEDGER_PUBLIC_URL` | `https://app.runledger.site` |
| `RUNLEDGER_RETENTION_DAYS` | `90` |
| `RUNLEDGER_POLAR_WEBHOOK_SECRET` | The secret of the Polar webhook endpoint. Setting it turns billing on. |
| `RUNLEDGER_POLAR_PRODUCT_IDS` | The Team product's id, so other products never create teams. |
| `RUNLEDGER_BILLING_PORTAL_URL` | `https://polar.sh/<your-org>/portal` |
| `RUNLEDGER_RESEND_API_KEY` | A Resend key with sending access only. |
| `RUNLEDGER_MAIL_FROM` | `RunLedger <team@runledger.site>` (the domain must be verified in Resend). |
| `RUNLEDGER_SUPPORT_EMAIL` | The address customers reply to. |
| `RUNLEDGER_TRUSTED_PROXIES` | The Compose network's subnet (see self-hosting.md). |

With the webhook secret set and no Resend settings, the server refuses to start: a customer would pay
and never receive a key.

## Server setup (Oracle Cloud Always Free, Arm)

1. Create an Ubuntu 24.04 instance, shape `VM.Standard.A1.Flex` (1-2 OCPU, 6-12 GB). Add your SSH key.
2. **Open ports 80 and 443 twice.** In the VCN's security list (or a network security group), add
   ingress rules for TCP 80 and 443 from `0.0.0.0/0`. Oracle's Ubuntu images also block them in the
   instance's own firewall:

   ```bash
   sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 80 -j ACCEPT
   sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 443 -j ACCEPT
   sudo netfilter-persistent save
   ```

3. Install Docker:

   ```bash
   curl -fsSL https://get.docker.com | sudo sh
   sudo usermod -aG docker ubuntu   # then log out and in again
   ```

4. Point DNS at the server: an `A` record `app` -> the instance's public IP. Reserve the IP in Oracle
   (Networking > Reserved public IPs) so it survives a restart.
5. Get the code and configure it:

   ```bash
   git clone https://github.com/kodji7202-code/runledger.git && cd runledger/deploy
   cp .env.example .env && chmod 600 .env && nano .env
   docker compose up -d --build
   docker compose logs -f runledger    # "Billing on: Polar webhooks at https://app.runledger.site/billing/polar/webhook"
   ```

6. Check `https://app.runledger.site/health` answers `{"ok": true, ...}`.

## Polar and Resend

- **Polar webhook.** Settings > Webhooks > Add endpoint: URL
  `https://app.runledger.site/billing/polar/webhook`, format Raw, events `subscription.created`,
  `subscription.active`, `subscription.updated`, `subscription.canceled`, `subscription.uncanceled`,
  `subscription.revoked` and `subscription.past_due`. Copy the secret into `.env` and restart
  (`docker compose up -d`).
- **Polar product.** Recurring monthly, seat-based pricing, $15 per seat. Add a required custom text
  field with the slug `team_name` to the checkout; it becomes the team's name. Set the success URL to
  `https://runledger.site/welcome.html`.
- **Resend.** Add and verify the sending domain (the DNS records Resend shows), then create an API key
  with "Sending access" for that domain.

## Test before selling

Polar has a sandbox (`sandbox.polar.sh`) with test cards. Point a sandbox webhook at the server, buy the
product with a test card, and check: the welcome email arrives, the key signs in, a push works, the
`billing list` row is `active`.

```bash
docker compose exec runledger runledger billing list --db /data/runledger.db
```

Then cancel in the sandbox portal and check the dashboard banner. Delete the sandbox webhook and
the test team afterwards, and switch to the production webhook secret.

## Operations

- **Backups.** `deploy/backup.sh` from cron, daily (see self-hosting.md, "Backups"). Copy
  `deploy/backups/` off the machine too, for example to Oracle Object Storage (20 GB free) with `rclone`,
  or to your own computer with `scp`. Test a restore.
- **Monitoring.** A free uptime monitor (UptimeRobot, Better Stack) on `https://app.runledger.site/health`
  every 5 minutes, alerting your email.
- **Logs.** `docker compose logs --since 24h runledger`. Webhook failures, ignored events and email
  failures are logged without keys or email bodies.
- **Polar delivery log.** Polar shows each webhook delivery and lets you redeliver. After 10 failed
  deliveries in a row it disables the endpoint, so watch it after a deploy.
- **Upgrades.** `git pull && docker compose build --pull && docker compose up -d`, after a backup.
- **Idle reclaim.** Oracle may reclaim Always Free instances that stay idle. Upgrade the account to
  Pay As You Go (resources inside the Always Free limits stay free) to avoid it, and set a budget alert
  in Oracle at 1 EUR.
