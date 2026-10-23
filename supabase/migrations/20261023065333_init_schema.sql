-- Migration 1: handbook vectors, student tokens, usage log, app settings, and the two RPCs.
-- Everything is server-only: RLS is on with no policies, anon/authenticated have no grants,
-- and only service_role (the FastAPI server) can execute the functions.

create extension if not exists vector with schema extensions;

-- Handbook chunks. embedding_model records which model made each vector: vectors from
-- different models are not comparable, so the app refuses to mix them.
create table public.handbook_chunks (
  id bigint generated always as identity primary key,
  section_slug text not null,
  section_title text not null,
  heading text,
  content text not null,
  token_count int not null,
  embedding extensions.vector(512) not null,
  embedding_model text not null,
  created_at timestamptz not null default now()
);
create index handbook_chunks_embedding_idx
  on public.handbook_chunks using hnsw (embedding extensions.vector_cosine_ops);
create index handbook_chunks_section_idx on public.handbook_chunks (section_slug);

-- Student tokens. Only a sha256 of the token is stored; the plain token is shown once at mint.
create table public.student_tokens (
  id uuid primary key default gen_random_uuid(),
  label text not null,
  email text,
  token_hash text not null unique,
  token_prefix text not null,
  rpm_limit int not null default 20,
  daily_limit int not null default 300,
  revoked_at timestamptz,
  created_at timestamptz not null default now()
);

-- One row per request on either surface. This table is the Stage 4 observability example.
create table public.usage_log (
  id bigint generated always as identity primary key,
  token_id uuid references public.student_tokens (id) on delete set null,
  surface text not null check (surface in ('endpoint', 'demo')),
  client_ip_hash text,
  model text,
  input_tokens int,
  output_tokens int,
  latency_ms int,
  status int,
  outcome text not null,
  detail text,
  ignored_params jsonb,
  judgments jsonb,
  estimated_cost_usd numeric(10, 6),
  created_at timestamptz not null default now()
);
create index usage_log_token_created_idx on public.usage_log (token_id, created_at);
create index usage_log_ip_created_idx on public.usage_log (client_ip_hash, created_at);
create index usage_log_created_idx on public.usage_log (created_at);

create table public.app_settings (
  key text primary key,
  value jsonb not null,
  updated_at timestamptz not null default now()
);
insert into public.app_settings (key, value) values
  ('kill_switch', 'false'),
  ('spend_cap_usd', '10'),
  ('demo_rpm_per_ip', '10');

alter table public.handbook_chunks enable row level security;
alter table public.student_tokens enable row level security;
alter table public.usage_log enable row level security;
alter table public.app_settings enable row level security;

revoke all on table public.handbook_chunks, public.student_tokens, public.usage_log,
  public.app_settings from anon, authenticated;

-- Nearest handbook chunks by cosine similarity (1 = same direction, 0 = unrelated).
create function public.match_chunks(
  query_embedding extensions.vector(512),
  match_count int default 4
)
returns table (
  id bigint,
  section_slug text,
  section_title text,
  heading text,
  content text,
  similarity double precision
)
language sql
stable
set search_path = public, extensions
as $$
  select c.id, c.section_slug, c.section_title, c.heading, c.content,
         1 - (c.embedding <=> query_embedding) as similarity
  from public.handbook_chunks c
  order by c.embedding <=> query_embedding
  limit match_count;
$$;

-- Allow or deny one class-endpoint request, cheapest checks first, in a single round trip.
-- Serverless functions keep no memory between requests, so limits are counted from usage_log.
create function public.authorize_request(p_token_hash text)
returns jsonb
language plpgsql
stable
set search_path = public
as $$
declare
  v_token public.student_tokens%rowtype;
  v_cap numeric;
  v_spent numeric;
  v_minute int;
  v_day int;
  -- Days and months follow class time (ET), not UTC
  v_day_start timestamptz :=
    date_trunc('day', now() at time zone 'America/New_York') at time zone 'America/New_York';
  v_month_start timestamptz :=
    date_trunc('month', now() at time zone 'America/New_York') at time zone 'America/New_York';
begin
  if coalesce((select (value #>> '{}')::boolean from app_settings where key = 'kill_switch'), false)
  then
    return jsonb_build_object('allowed', false, 'reason', 'kill_switch');
  end if;

  select * into v_token from student_tokens where token_hash = p_token_hash;
  if not found then
    return jsonb_build_object('allowed', false, 'reason', 'invalid_token');
  end if;
  if v_token.revoked_at is not null then
    return jsonb_build_object('allowed', false, 'reason', 'revoked', 'token_id', v_token.id);
  end if;

  select (value #>> '{}')::numeric into v_cap from app_settings where key = 'spend_cap_usd';
  select coalesce(sum(estimated_cost_usd), 0) into v_spent
    from usage_log where created_at >= v_month_start;
  if v_cap is not null and v_spent >= v_cap then
    return jsonb_build_object('allowed', false, 'reason', 'spend_cap', 'token_id', v_token.id);
  end if;

  -- Denied requests are logged too but don't count, so retrying doesn't extend a lockout.
  -- Check-then-log is not atomic: two simultaneous requests at the limit can both pass.
  -- That's acceptable at class scale and a good Stage 4 discussion.
  select count(*) filter (where created_at >= now() - interval '1 minute'),
         count(*) filter (where created_at >= v_day_start)
    into v_minute, v_day
    from usage_log
   where token_id = v_token.id
     and outcome <> 'denied'
     and created_at >= least(v_day_start, now() - interval '1 minute');

  if v_minute >= v_token.rpm_limit then
    return jsonb_build_object('allowed', false, 'reason', 'rate_limit_minute',
      'token_id', v_token.id, 'retry_after_seconds', 60);
  end if;
  if v_day >= v_token.daily_limit then
    return jsonb_build_object('allowed', false, 'reason', 'rate_limit_day', 'token_id', v_token.id);
  end if;

  return jsonb_build_object(
    'allowed', true,
    'reason', null,
    'token_id', v_token.id,
    'label', v_token.label,
    'remaining_minute', v_token.rpm_limit - v_minute - 1,
    'remaining_day', v_token.daily_limit - v_day - 1
  );
end;
$$;

revoke execute on function public.match_chunks(extensions.vector, int) from public, anon, authenticated;
revoke execute on function public.authorize_request(text) from public, anon, authenticated;
grant execute on function public.match_chunks(extensions.vector, int) to service_role;
grant execute on function public.authorize_request(text) to service_role;
