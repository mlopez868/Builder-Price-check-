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
-- ============ views ============
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
-- ============ point-in-time reconstruction ============
create or replace function plan_price_on(p_plan_id bigint, p_date date)
returns numeric language sql stable as $$
  select base_price from plan_price_events
  where plan_id = p_plan_id and observed_on <= p_date
  order by observed_on desc limit 1;
$$;
