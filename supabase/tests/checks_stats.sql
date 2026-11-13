-- Assertions for usage_stats: every number, the Eastern-time day buckets, the window clamp,
-- the empty state, that nothing identifying leaks, and the anon lockout.
-- Runs in one transaction and rolls back, so it leaves no rows behind for other check files.
\set ON_ERROR_STOP on
begin;
grant all on all tables in schema public to service_role;
delete from usage_log;

do $$
declare
  r jsonb;
  t uuid;
  -- Noon Eastern today and yesterday: safely inside one calendar day whenever this runs
  v_today date := (now() at time zone 'America/New_York')::date;
  v_noon timestamptz := (v_today + time '12:00') at time zone 'America/New_York';
  v_midnight timestamptz := v_today::timestamp at time zone 'America/New_York';
  v_month_start timestamptz :=
    date_trunc('month', now() at time zone 'America/New_York') at time zone 'America/New_York';
begin
  -- Empty log: zeros and nulls, never an error, and still one entry per day
  r := usage_stats(7);
  assert (r->>'days')::int = 7 and jsonb_array_length(r->'daily') = 7, r::text;
  assert (r->'totals'->>'requests')::int = 0 and (r->'totals'->>'cost_usd')::numeric = 0, r::text;
  assert r->'by_surface' = '[]'::jsonb and r->'demo'->'outcomes' = '[]'::jsonb, r::text;
  assert r->'demo'->'decline_rate' = 'null'::jsonb, 'no questions means no rate: ' || r::text;
  assert r->'latency'->'demo_answered'->'p50_ms' = 'null'::jsonb, r::text;
  assert (r->'month'->>'cap_usd')::numeric = 10 and (r->'month'->>'spend_usd')::numeric = 0, r::text;

  -- The window is clamped to 1..90 days
  assert (usage_stats(0)->>'days')::int = 1 and (usage_stats(1000)->>'days')::int = 90;
  assert (usage_stats(null)->>'days')::int = 30 and (usage_stats()->>'days')::int = 30;

  insert into student_tokens (label, token_hash, token_prefix)
  values ('private-label', 'private-token-hash', 'sk-bootcamp-') returning id into t;

  -- Today, demo: 5 answered (100..500 ms), 2 declined, 1 emergency, 1 denied, 1 error
  insert into usage_log (surface, outcome, client_ip_hash, latency_ms, input_tokens, output_tokens,
                         estimated_cost_usd, created_at)
  select 'demo', 'answered', 'private-ip-hash', n * 100, 700, 50, 0.0001, v_noon
  from generate_series(1, 5) n;
  insert into usage_log (surface, outcome, client_ip_hash, latency_ms, detail, created_at) values
    ('demo', 'declined', 'private-ip-hash', 9000, 'out_of_scope', v_noon),
    ('demo', 'declined', 'private-ip-hash', 9000, 'low_relevance', v_noon),
    ('demo', 'emergency', 'private-ip-hash', 9000, 'emergency', v_noon),
    ('demo', 'denied', 'private-ip-hash', null, 'rate_limit_minute', v_noon),
    ('demo', 'error', 'private-ip-hash', 9000, null, v_noon);
  -- Today, other surfaces
  insert into usage_log (surface, outcome, token_id, latency_ms, input_tokens, output_tokens,
                         estimated_cost_usd, created_at) values
    ('endpoint', 'answered', t, 1000, 100, 200, 0.002, v_noon),
    ('endpoint', 'invalid_request', t, null, null, null, 0, v_noon),
    ('eval', 'answered', null, 1200, 700, 50, 0.0001, v_noon),
    ('booking', 'replied', null, 800, 300, 40, 0.0001, v_noon),
    ('mcp', 'tool_call', null, 50, null, null, 0, v_noon);
  -- One second either side of midnight Eastern: different days, though the same day in UTC
  insert into usage_log (surface, outcome, created_at) values
    ('demo', 'answered', v_midnight - interval '1 second'),
    ('demo', 'answered', v_midnight + interval '1 second');

  r := usage_stats(2);
  assert (r->'totals'->>'requests')::int = 17, r::text;
  assert (r->'totals'->>'cost_usd')::numeric = 0.0027, r::text;
  assert (r->'totals'->>'input_tokens')::int = 4600, r::text;
  assert (r->'totals'->>'output_tokens')::int = 540, r::text;

  -- Surfaces, busiest first
  assert r->'by_surface'->0->>'surface' = 'demo' and (r->'by_surface'->0->>'requests')::int = 12, r::text;
  assert (r->'by_surface'->0->>'cost_usd')::numeric = 0.0005, r::text;
  assert jsonb_array_length(r->'by_surface') = 5, r::text;
  assert (select (s->>'requests')::int from jsonb_array_elements(r->'by_surface') s
           where s->>'surface' = 'endpoint') = 2, r::text;

  -- Demo outcomes: rates are out of the 10 questions the pipeline finished, not all 12 rows
  assert (r->'demo'->>'requests')::int = 12 and (r->'demo'->>'completed')::int = 10, r::text;
  assert (r->'demo'->>'decline_rate')::numeric = 0.2, r::text;
  assert (r->'demo'->>'emergency_rate')::numeric = 0.1, r::text;
  assert r->'demo'->'outcomes'->0 = '{"outcome": "answered", "requests": 7}'::jsonb, r::text;
  assert jsonb_array_length(r->'demo'->'outcomes') = 5, r::text;

  -- Latency: answered demo questions only (the 9000 ms declines and errors stay out)
  assert (r->'latency'->'demo_answered'->>'count')::int = 5, r::text;
  assert (r->'latency'->'demo_answered'->>'p50_ms')::int = 300, r::text;
  assert (r->'latency'->'demo_answered'->>'p95_ms')::int = 480, r::text;
  assert (r->'latency'->'endpoint_completions'->>'count')::int = 1, r::text;
  assert (r->'latency'->'endpoint_completions'->>'p95_ms')::int = 1000, r::text;

  -- Per-day series: oldest first, bucketed by Eastern day
  assert jsonb_array_length(r->'daily') = 2, r::text;
  assert r->'daily'->0->>'day' = (v_today - 1)::text and (r->'daily'->0->>'requests')::int = 1, r::text;
  assert r->'daily'->1->>'day' = v_today::text and (r->'daily'->1->>'requests')::int = 16, r::text;
  assert (r->'daily'->1->>'cost_usd')::numeric = 0.0027, r::text;
  assert r->>'first_day' = (v_today - 1)::text and r->>'last_day' = v_today::text, r::text;

  -- A one-day window drops yesterday's row
  assert (usage_stats(1)->'totals'->>'requests')::int = 16, usage_stats(1)::text;

  -- Aggregates only: no label, token hash, IP hash or decline reason anywhere in the output
  assert usage_stats(90)::text !~ 'private|out_of_scope|rate_limit', 'identifying data leaked';

  -- Month-to-date spend ignores the window and stops at the start of the Eastern month
  delete from usage_log;
  insert into usage_log (surface, outcome, estimated_cost_usd, created_at) values
    ('endpoint', 'answered', 1.25, v_month_start + interval '1 second'),
    ('endpoint', 'answered', 5, v_month_start - interval '1 second');
  r := usage_stats(1);
  assert (r->'month'->>'spend_usd')::numeric = 1.25, r::text;
  update app_settings set value = '25' where key = 'spend_cap_usd';
  assert (usage_stats(1)->'month'->>'cap_usd')::numeric = 25;

  raise notice 'ALL USAGE STATS CHECKS PASSED';
end $$;

-- Only the server's role can call it
set role anon;
do $$ begin
  begin perform public.usage_stats(); raise exception 'anon could execute usage_stats';
  exception when insufficient_privilege then null; end;
end $$;
reset role;
set role authenticated;
do $$ begin
  begin perform public.usage_stats(); raise exception 'authenticated could execute usage_stats';
  exception when insufficient_privilege then null; end;
end $$;
reset role;
set role service_role;
select (public.usage_stats(7))->>'days' as service_role_can_call_stats;
reset role;
rollback;
