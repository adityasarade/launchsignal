# LaunchSignal

A stateful monitor that watches for new **Y Combinator** and **a16z Speedrun**
companies and posts deduplicated alerts to Slack — with a dedicated lane for the
case that actually matters to a GTM team: **a founder announcing acceptance
before the programme's own announcement goes out.**

Python 3.12+, **zero third-party dependencies**, SQLite-backed. Runs locally,
in Docker, or as a Pond Protocol V1 agent.

```
🚨 EARLY YC SIGNAL — Founder Announced Before YC
Company:  Acme AI
Founder:  Jane Doe (@janedoe)
Batch:    YC S26
Source:   X (Twitter)
Status:   Founder-announced / not yet officially announced — not found in
          6 snapshot(s) from official_x, official_linkedin, checked
          2026-09-02 09:14 UTC
Original post:
> We got into YC S26! After spending the last year building Acme AI,
> we're moving to SF and going all in.
Open source · Programme profile  |  Detected 2026-09-02 09:14 UTC
```

## Quick start

```bash
git clone https://github.com/adityasarade/launchsignal.git
cd launchsignal
make install                 # venv + editable install
cp .env.example .env         # then fill in the Slack values
make doctor                  # tells you exactly what is missing
make scan                    # first run: silent baseline, no alerts
```

`make doctor` is the one command to run when something looks wrong. It checks
the Python version, whether `.env` was actually read, the database, the Slack
credential (via `auth.test`, without sending a message), and whether public
search is configured — then prints a checklist.

With Docker instead:

```bash
cp .env.example .env
docker compose up monitor
```

## Slack setup (about three minutes)

1. Create an app at <https://api.slack.com/apps> → **From scratch**, and pick
   the workspace you want alerts in.
2. **OAuth & Permissions** → **Bot Token Scopes** → add `chat:write`. That is
   the only scope needed.
3. **Install to Workspace**, then copy the **Bot User OAuth Token** (`xoxb-…`).
4. In Slack, invite the bot to your channel: `/invite @YourAppName`.
5. Copy the channel ID from **Channel details** (bottom of the About tab).
6. Put both values in `.env`, set `SLACK_DRY_RUN=false`, and run `make doctor`.

No redirect URL, no paid plan, no user token, and no `channels:read` — a known
channel ID keeps the scope set minimal.

## Commands

| Command | What it does |
| --- | --- |
| `launchsignal doctor` | Validate setup. Sends nothing. |
| `launchsignal scan` | One full cycle over every source. |
| `launchsignal fast` | Founder lane only: recent social posts. |
| `launchsignal serve` | Full scan every 8h + fast lane every 45m. |
| `launchsignal health` | Per-source status, alert and review counts. |
| `launchsignal review` | Candidates the classifier refused to resolve. |
| `launchsignal pond` | Pond Protocol V1 control plane on `:8080`. |

## Sources

| Source | How it is read | What it yields |
| --- | --- | --- |
| **YC directory** | Public `/companies/sitemap` with a conditional `If-None-Match` request, then one profile fetch per *newly listed* company. The silent baseline skips enrichment, so installing costs one request rather than 6,000 | Company, canonical name, **batch**, description, profile URL |
| **a16z Speedrun** | Public company API with correct pagination | Company, **cohort** (`SR003`), description, founder, X/LinkedIn handles, profile URL |
| **X** | Public web search restricted to canonical `x.com/…/status/…` URLs | Founder acceptance posts |
| **LinkedIn (posts)** | Public web search restricted to canonical `/posts/…` URLs | Founder acceptance posts |
| **LinkedIn (company pages)** | Separate query family restricted to `/company/…` URLs | Company pages *first observed* |

Speedrun is **a16z's** programme, not YC's. It is labelled `a16z Speedrun`
everywhere — in the enum, the database, and every alert.

### What the social sources do and do not do

They read a **public search index**. They never log in, never present a cookie,
never bypass an anti-bot check, and never request a page that requires an
account. A LinkedIn company page is reported as **first observed by us**, which
is not the same as its creation date — the alert says so rather than implying
otherwise.

Set `TINYFISH_API_KEY` to enable them. Without it those three sources are
reported as **failed and skipped**, never as a clean pass — so a scan can never
look complete when two thirds of it did not run.

## The early-signal rule

Four facts are tracked independently, because they can happen in any order:

```
founder_claim         a public post says "we got into YC S26"
directory_membership  the company appears in a public directory
official_x            a configured official X account has posted about it
official_linkedin     a configured official LinkedIn page has posted about it
```

An alert is labelled **“Founder Announced Before YC”** only when official
accounts were genuinely checked, returned at least one snapshot, and did not
mention the company. If no official-snapshot source is configured — or the
search ran but returned nothing usable — the same claim is reported as
**“FOUNDER CLAIM — unverified”**, with the reason spelled out.

The two programmes are compared separately. A claim about a YC batch is only
ever checked against YC's own accounts, so an a16z post about a similarly named
company cannot suppress it. The programme itself is read from the claim text,
not assumed: a post saying “we joined a16z Speedrun SR004” alerts as
`a16z Speedrun`, with its own company key.

That distinction is the point. “I checked nothing and found nothing” is not
evidence that a programme has stayed quiet, so the card never says it is. Every
early alert carries the accounts checked, how many snapshots were read, and
when — an auditable statement instead of an absolute negative.

Directory membership never suppresses an early claim. A company can be listed
in the directory *and* still have been announced by its founder first, so
`CONFIRMED` and `EARLY_FOUNDER_CLAIM` are separate alerts about the same
company.

## State and deduplication

- **`observations`** — append-only, keyed by `(source, external_id)`. Tracking
  parameters are stripped first, so `?s=20` is not a second post.
- **`company_key`** — clusters every source's view of one company. One X post
  and one LinkedIn post about the same company are **one** alert.
- **`alert_key`** = `company_key|kind`. Different *kinds* of news about the same
  company each get their own delivery slot.
- **Silent baseline, per source.** A source's first scan records what already
  exists and alerts nothing. It is tracked per source, so adding a source later
  does not mute the others, and a source that fails mid-baseline is not marked
  seeded.
- **Atomic delivery claim.** The alert row is written *before* the Slack call,
  and the write itself is the claim: exactly one caller can create a given
  `alert_key`, so two sources in one scan — or two concurrent scans — cannot
  both send. Retries take an equally atomic lease. Sends are paced to Slack's
  rate limit and `429` is honoured.
- **Dead-lettering.** A transient failure is retried on later scans. A fatal one
  (`channel_not_found`, `invalid_auth`) is recorded as dead immediately and
  shown by `launchsignal health`, rather than retried until an attempt budget
  quietly runs out.
- **One scan at a time.** The scheduler and the Pond control plane share a
  database-backed lock, so they cannot overlap and double-deliver.
- **Review queue.** If no company can be resolved from a post, it goes to
  `launchsignal review` — the monitor never invents a name to fill a card.

## Adding another network

1. Add a `Source` enum member and one row in `SOURCE_CATEGORIES`
   ([`models.py`](src/launchsignal/models.py)).
2. Write an adapter with `name` and `scan(store)` ([`sources.py`](src/launchsignal/sources.py)).
3. Register it in `all_scan_sources()`.

Classification keys off the *category*, never the source identity, so the
classifier, store and Slack renderer need no changes. There is a test that
holds this line (`ExtensibilityTest`).

## Pond Protocol V1

```bash
POND_ACCESS_KEY=... launchsignal pond
```

| Endpoint | Auth | Purpose |
| --- | --- | --- |
| `GET /manifest` | public | Actions, schemas, limits |
| `POST /runs` | Bearer | `get_health` (sync), `run_scan` (async), `review_signal` (sync) |
| `GET /tasks/{id}` | Bearer | Poll an async run |
| `GET /healthz` | public | Liveness |

`run_id` is idempotent — replaying one returns the original task instead of
starting a second scan. Auth **fails closed**: with no `POND_ACCESS_KEY` set,
`/runs` and `/tasks` reject every request rather than standing open.

## Tests

```bash
make test        # 157 tests, no network access
```

Every test maps to a behaviour that matters: the baseline is silent, an
identical rescan sends nothing, a directory listing does not suppress a later
founder claim, a failed Slack send is retried rather than lost, a *fatal* one
is not retried forever, exactly one caller can claim an alert under concurrency,
one failing source does not stop the others, a Speedrun post is never labelled
YC, an unchecked claim is never presented as a scoop, and `Arc` does not match
`Arcade`.

Verified live against the real sources: a baseline of 6,462 records
(6,204 YC + 258 a16z Speedrun) in ~5s with zero alerts, an identical rescan
producing zero new records and zero alerts, and newly listed companies alerting
with a real batch and description pulled from their profile pages
(`ReactWise — YC S24 — AI Co-Pilot for Chemical Process Optimization`).

## Cost

Runs at **$0**. Public sitemap, public API, and a free public-search tier. No
paid X API path exists in the code. See [docs/NO-SPEND.md](docs/NO-SPEND.md).

## Limits, stated plainly

- Public search has imperfect recall; it finds what is indexed, not everything.
- A LinkedIn company page's *first observed* time is not its creation time.
- Batch labels are configuration (`LAUNCHSIGNAL_BATCHES`) and need updating when
  a new batch is announced.
- SQLite is durable on a persistent volume and fine for one workspace. A
  multi-tenant deployment would want PostgreSQL.
