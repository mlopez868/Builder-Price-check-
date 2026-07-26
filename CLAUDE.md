# Build Brief: San Antonio Builder Pricing Tracker
> **How to use this:** drop this file into an empty repo as `CLAUDE.md`, open Claude Code in that directory, and say *"Read CLAUDE.md and build Phase 0."* Work one phase at a time and verify acceptance criteria before moving on.
---
## 1. What we're building
A daily automation that tracks new-home pricing from San Antonio homebuilder websites and stores it as a queryable time series. The output is used for land acquisition underwriting — competitive $/sf by submarket, price movement over time, and inventory absorption velocity.
Three tiers of data, in ascending order of value:
1. **Communities** — parent entity, mostly metadata
2. **Plans** — base price by floorplan. Normalizes to $/sf, the comparison unit across builders
3. **Quick-move-in homes (QMI)** — actual standing inventory with asking price. Highest signal: real prices including lot premium, visible discounting, and — when a listing disappears — a free absorption/days-on-market measurement
---
## 2. Non-negotiable constraints
These are architectural decisions already made. Do not revisit them or propose alternatives.
**No LLM at runtime.** The scheduled job must be fully deterministic. Models are used only (a) by a human operator in Claude Code to author an extractor config, and (b) to repair a broken config. The runner never calls a model. Same input must produce identical output on every run — this data lands in financial models and has to be auditable.
**Write only on change.** Do not insert a row per entity per day. Fetch daily, compare against the last known value, and write an event row *only when a value differs*. Otherwise update `last_checked_at` on the entity. This is what keeps us inside Supabase's 500 MB free tier — a naive daily dump would exceed it in about four months.
**Events are immutable.** Never `UPDATE` or `DELETE` a row in any `*_events` table. Corrections are new rows. Entity tables (`communities`, `plans`, `qmi_homes`) may be updated.
**Store `raw` payloads only on change,** never on an unchanged observation. Raw JSON blobs are the main storage risk.
**Cheapest transport first.** For each builder, prefer in this order: (1) direct HTTP to a JSON endpoint, (2) JSON-LD parsed from the page, (3) Playwright rendering. Most modern builder sites serve their plan and inventory data from a JSON API behind the page — find it before writing any DOM selectors.
**Read-only, public data only.** This tool reads public marketing pages. No authentication, no form submission, no employer-proprietary data of any kind in this repo or database.
---
## 3. Stack
- **Python 3.12**, `uv` for dependency management
- `httpx` (fetching), `jsonpath-ng` (field mapping), `playwright` (rendering fallback only), `psycopg[binary]` (Postgres), `pytest`
- **Supabase free tier** for Postgres
- **GitHub Actions** for scheduling
- Secrets via GitHub Actions secrets; local dev via `.env` (gitignored)
Required env var: `DATABASE_URL` — Supabase connection string (use the session pooler URI).
---
## 4. Repo layout
```
.
├── .github/workflows/
│   ├── daily-qmi.yml          # QMI + incentives, daily
│   ├── weekly-plans.yml       # plan pricing, weekly + month-end
│   └── monthly-backup.yml     # pg_dump to repo artifact
├── migrations/
│   └── 0001_init.sql
├── extractors/
│   ├── _schema.json           # JSON Schema the configs validate against
│   └── <builder-slug>.json    # one per builder
├── src/pricing/
│   ├── config.py              # env + settings
│   ├── db.py                  # connection, upserts, event writes
│   ├── fetch.py               # http / jsonld / playwright transports
│   ├── extract.py             # config-driven mapping to normalized dicts
│   ├── diff.py                # change detection
│   ├── runner.py              # orchestration + CLI entrypoint
│   └── digest.py              # weekly change report
├── scripts/
│   ├── dry_run.py             # test an extractor config without writing
│   └── backup.py
├── tests/
│   ├── fixtures/              # saved JSON payloads — tests never hit the network
│   └── test_*.py
├── pyproject.toml
└── CLAUDE.md
```
---
## 5. Database schema
Write this as `migrations/0001_init.sql`.
```sql
-- ============ entities ============
create table builders (
  id          bigserial primary key,
  slug        text not null unique,
  name        text not null,
  root_url    text,
  notes       text,
  created_at  timestamptz default now()
);
create table extractors (
  id           bigserial primary key,
  builder_id   bigint not null references builders(id),
  version      int not null default 1,
  transport    text not null check (transport in ('http','jsonld','playwright')),
  config       jsonb not null,
  authored_by  text,                    -- model + date, for audit
  is_active    boolean default true,
  created_at   timestamptz default now(),
  unique (builder_id, version)
);
create table communities (
  id              bigserial primary key,
  builder_id      bigint not null references builders(id),
  source_key      text not null,        -- builder's own internal id
  name            text not null,
  url             text,
  submarket       text,                 -- our taxonomy, set manually
  city            text,
  zip             text,
  status          text default 'active',
  first_seen      date default current_date,
  last_checked_at timestamptz,
  last_seen_on    date,
  unique (builder_id, source_key)
);
create table plans (
  id                 bigserial primary key,
  community_id       bigint not null references communities(id),
  source_key         text not null,
  name               text not null,
  sq_ft              int,
  stories            smallint,
  beds               numeric(3,1),
  baths              numeric(3,1),
  garage             smallint,
  min_lot_width      int,
  identity_hash      text,              -- md5(sq_ft|beds|baths|stories) — rename detection
  current_base_price numeric(12,2),     -- denormalized latest, for cheap diffing
  first_seen         date default current_date,
  last_checked_at    timestamptz,
  last_seen_on       date,
  unique (community_id, source_key)
);
create index on plans (identity_hash);
create table qmi_homes (
  id                 bigserial primary key,
  community_id       bigint not null references communities(id),
  plan_id            bigint references plans(id),
  source_key         text not null,
  address            text,
  lot                text,
  block              text,
  sq_ft              int,
  est_completion     date,
  current_list_price numeric(12,2),
  first_seen         date default current_date,
  last_checked_at    timestamptz,
  last_seen_on       date,
  delisted_on        date,              -- absorption signal
  unique (community_id, source_key)
);
-- ============ immutable events ============
create table plan_price_events (
  id           bigserial primary key,
  plan_id      bigint not null references plans(id),
  extractor_id bigint references extractors(id),
  observed_on  date not null,
  base_price   numeric(12,2),
  prev_price   numeric(12,2),
  status       text,
  raw          jsonb,
  created_at   timestamptz default now()
);
create index on plan_price_events (plan_id, observed_on desc);
create table qmi_price_events (
  id           bigserial primary key,
  qmi_id       bigint not null references qmi_homes(id),
  extractor_id bigint references extractors(id),
  observed_on  date not null,
  list_price   numeric(12,2),
  prev_price   numeric(12,2),
  was_price    numeric(12,2),           -- builder's own struck-through price
  status       text,
  raw          jsonb,
  created_at   timestamptz default now()
);
create index on qmi_price_events (qmi_id, observed_on desc);
create table incentive_events (
  id           bigserial primary key,
  community_id bigint not null references communities(id),
  observed_on  date not null,
  headline     text,
  body         text,
  body_hash    text,                    -- dedupe: only write when hash changes
  created_at   timestamptz default now()
);
create index on incentive_events (community_id, observed_on desc);
-- ============ operations ============
create table scrape_runs (
  id            bigserial primary key,
  extractor_id  bigint references extractors(id),
  job           text,                   -- 'qmi' | 'plans'
  started_at    timestamptz default now(),
  finished_at   timestamptz,
  status        text,                   -- ok | empty | error
  entities_seen int,
  events_written int,
  error         text
);
```
### Views
```sql
-- price per square foot, every observed change
create view v_plan_psf as
select b.name as builder, c.submarket, c.name as community,
       p.name as plan, p.sq_ft, e.observed_on, e.base_price,
       round(e.base_price / nullif(p.sq_ft,0), 2) as psf,
       e.prev_price,
       round(100 * (e.base_price - e.prev_price) / nullif(e.prev_price,0), 2) as pct_change
from plan_price_events e
join plans p on p.id = e.plan_id
join communities c on c.id = p.community_id
join builders b on b.id = c.builder_id;
-- days on site for sold/delisted inventory
create view v_qmi_absorption as
select b.name as builder, c.submarket, c.name as community,
       h.address, h.sq_ft, h.first_seen, h.delisted_on,
       (h.delisted_on - h.first_seen) as days_on_site,
       h.current_list_price
from qmi_homes h
join communities c on c.id = h.community_id
join builders b on b.id = c.builder_id
where h.delisted_on is not null;
```
### Point-in-time reconstruction
Because we only store changes, provide a helper:
```sql
create or replace function plan_price_on(p_plan_id bigint, p_date date)
returns numeric language sql stable as $$
  select base_price from plan_price_events
  where plan_id = p_plan_id and observed_on <= p_date
  order by observed_on desc limit 1;
$$;
```
---
## 6. Extractor config format
One JSON file per builder in `extractors/`. This is the only thing that changes when adding a builder — **adding a builder must never require writing code.** Validate every config against `extractors/_schema.json` on load.
```json
{
  "builder_slug": "example-homes",
  "transport": "http",
  "host": "www.example-homes.com",
  "rate_limit_ms": 1500,
  "price_includes_lot_premium": false,
  "communities": {
    "url": "https://{host}/api/communities?market=san-antonio",
    "path": "$.results[*]",
    "map": {
      "source_key": "id",
      "name": "name",
      "url": "url",
      "zip": "postalCode",
      "city": "city"
    }
  },
  "plans": {
    "url": "https://{host}/api/communities/{source_key}/floorplans",
    "path": "$.plans[*]",
    "map": {
      "source_key": "planId",
      "name": "planName",
      "sq_ft": "squareFeet",
      "base_price": "basePrice",
      "beds": "bedrooms",
      "baths": "bathrooms",
      "stories": "stories",
      "garage": "garageBays"
    }
  },
  "qmi": {
    "url": "https://{host}/api/communities/{source_key}/inventory",
    "path": "$.homes[*]",
    "map": {
      "source_key": "homeId",
      "address": "streetAddress",
      "sq_ft": "squareFeet",
      "list_price": "price",
      "was_price": "originalPrice",
      "est_completion": "estimatedCompletion",
      "status": "availabilityStatus",
      "plan_name": "floorplanName"
    }
  },
  "incentives": {
    "url": "https://{host}/community/{source_key}",
    "transport": "playwright",
    "selectors": { "headline": ".promo-banner h2", "body": ".promo-banner p" }
  }
}
```
Notes:
- `map` values are JSONPath expressions relative to each item matched by `path`. Support both bare field names and full JSONPath.
- Any section may override the top-level `transport`.
- `price_includes_lot_premium` is a per-builder flag set manually at onboarding. Never compare $/sf across builders without checking it.
- Prices arrive as strings like `"$329,990"` — normalize to numeric in `extract.py`, never in the config.
---
## 7. Runner behavior
`python -m pricing.runner --job qmi` / `--job plans` / `--builder example-homes`
For each active extractor:
1. Open a `scrape_runs` row.
2. Fetch and extract per config, honoring `rate_limit_ms` between requests.
3. **Upsert entities** on `(builder_id, source_key)` — update mutable fields, always set `last_checked_at = now()` and `last_seen_on = current_date`.
4. **Detect renames:** if a `source_key` is absent but an incoming plan's `identity_hash` matches an existing plan in the same community, treat it as a rename — update `name` and `source_key` on the existing row, do not create a new plan.
5. **Diff:** compare incoming price to `current_base_price` / `current_list_price`.
   - Unchanged → write no event; `last_checked_at` already updated.
   - Changed or first sight → insert an event row with `prev_price` populated and `raw` attached, then update the denormalized current price on the entity.
6. **Detect delisting:** for each community, compare the fetched set of QMI `source_key`s against non-delisted rows in the DB. Anything in the DB but absent from the feed gets `delisted_on = current_date`. Require the fetch to have returned a non-empty set first — never mark delistings off an empty or errored response.
7. **Incentives:** hash the body text; write an event only when the hash differs from the latest.
8. Close the `scrape_runs` row with counts.
**Failure handling.** If a fetch errors or returns zero entities for a builder that previously returned some, mark the run `empty`/`error`, skip all writes for that builder, and continue to the next. Never let one broken builder abort the job, and never let an empty response cascade into mass delistings. At the end, exit non-zero if any builder failed so the Action surfaces it.
---
## 8. Schedules
| Workflow | Cron (UTC) | Job |
|---|---|---|
| `daily-qmi.yml` | `0 11 * * *` | QMI + incentives — inventory turns over daily and delisting resolution drives absorption |
| `weekly-plans.yml` | `0 11 * * 2` plus `0 11 28-31 * *` | Plan base pricing; extra month-end runs catch incentive repricing |
| `monthly-backup.yml` | `0 12 1 * *` | `pg_dump` uploaded as a workflow artifact |
On failure, open a GitHub issue with the builder name and error so it lands in the operator's inbox.
---
## 9. Build phases
Do these strictly in order. Stop at each acceptance gate.
### Phase 0 — Schema and one hardcoded builder
Set up the project, run `0001_init.sql` against Supabase, and hardcode a scraper for a single builder and community. No config abstraction yet. The goal is to discover where the schema is wrong before there's data to migrate.
**Accept when:** one command populates one community, its plans, and its QMI homes, and running it twice produces zero new event rows the second time.
That second condition is the whole point — prove change-only writes work before building anything on top.
### Phase 1 — Config-driven runner
Extract the hardcoded logic into the config format. Build `fetch.py`, `extract.py`, `diff.py`, `runner.py`, and `_schema.json`. Port builder #1 to a config, then add two more builders as configs only.
**Accept when:** adding a builder requires only a new JSON file, and `pytest` passes against saved fixtures with no network access.
### Phase 2 — Onboarding workflow
Build `scripts/dry_run.py`: takes a config path, fetches, extracts, and prints a formatted preview table (plans with $/sf, QMIs with prices) **without writing to the database**.
This is what makes onboarding conversational — the operator asks Claude Code to explore a builder's site and draft a config, runs the dry run, eyeballs the output, and commits only if it looks right.
**Accept when:** a bad field mapping is visible in the preview before any data is written.
### Phase 3 — Scheduling and resilience
Add the three workflows, per-builder failure isolation, delisting detection with the empty-response guard, and GitHub issue creation on failure.
**Accept when:** deliberately breaking one extractor's URL causes exactly one builder to fail, an issue to open, and all other builders to complete normally.
### Phase 4 — Digest
`digest.py` produces a weekly summary: price changes with % and direction, median $/sf by submarket vs. 30 days prior, new communities detected, and QMI delistings with days-on-site. Deliver via email or Slack webhook.
**Accept when:** the digest runs off real accumulated data and reads like something worth opening.
---
## 10. Guardrails
Do **not**:
- call any LLM API from `src/pricing/` — the runtime path stays model-free
- `UPDATE` or `DELETE` rows in any `*_events` table
- write an event row when the value is unchanged
- store `raw` payloads on unchanged observations
- mark delistings when a fetch returned zero rows or errored
- hit live websites in tests — use fixtures in `tests/fixtures/`
- write DOM selectors before confirming no JSON endpoint exists
- commit `.env`, connection strings, or any credential
- add authentication, form submission, or login flows of any kind
Do:
- check `robots.txt` for each builder at onboarding and record the outcome in the config's `notes`
- set a descriptive `User-Agent` with contact info
- keep `rate_limit_ms` at 1500 or higher
- keep every network payload shape captured as a fixture the first time you see it
---
## 11. Setup notes
- Supabase projects created after May 30, 2026 require explicit Postgres grants for PostgREST access. We connect over direct Postgres, not PostgREST, so this shouldn't bite — but if the dashboard's table editor shows empty tables that contain data, that's the cause.
- Free-tier Supabase projects pause after 7 days without requests; the daily workflow keeps it alive as a side effect.
- Free tier has no backups. `monthly-backup.yml` is not optional — this data cannot be re-scraped retroactively.
- Add `DATABASE_URL` to GitHub Actions secrets before Phase 3.
