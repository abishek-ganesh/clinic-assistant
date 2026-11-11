-- Assertions for every migration: vector ranking, every authorize_request and
-- authorize_demo_request outcome, anon lockout.
\set ON_ERROR_STOP on
grant all on all tables in schema public to service_role;
create function pg_temp.vec(a float, b float) returns extensions.vector language sql as $$
  select (array[a, b] || array_fill(0::float, array[510]))::float4[]::extensions.vector $$;

do $$
declare r jsonb; t uuid; top text; sims float[];
begin
  -- match_chunks ranks by cosine similarity
  insert into handbook_chunks (section_slug, section_title, content, token_count, embedding, embedding_model)
  values ('refills','Refills','r',1,pg_temp.vec(1,0),'m'), ('hours','Hours','h',1,pg_temp.vec(0,1),'m');
  select section_slug into top from match_chunks(pg_temp.vec(0.9,0.1), 2) limit 1;
  assert top = 'refills', 'match_chunks wrong top: ' || top;
  select array_agg(similarity) into sims from match_chunks(pg_temp.vec(0.9,0.1), 2);
  assert sims[1] > sims[2] and sims[1] <= 1, 'similarities not descending in [0,1]';

  r := authorize_request('nope');
  assert r->>'reason' = 'invalid_token', r::text;

  insert into student_tokens (label, token_hash, token_prefix, rpm_limit, daily_limit)
  values ('test', 'h1', 'sk-bootcamp-', 3, 5) returning id into t;
  r := authorize_request('h1');
  assert (r->>'allowed')::boolean and (r->>'remaining_minute')::int = 2, r::text;

  -- per-minute limit, and denied rows don't count toward it
  insert into usage_log (token_id, surface, outcome) select t, 'endpoint', 'denied' from generate_series(1,10);
  r := authorize_request('h1');
  assert (r->>'allowed')::boolean, 'denied rows counted: ' || r::text;
  insert into usage_log (token_id, surface, outcome) select t, 'endpoint', 'completed' from generate_series(1,3);
  r := authorize_request('h1');
  assert r->>'reason' = 'rate_limit_minute', r::text;

  -- per-day limit: move those 3 rows out of the last minute (still today), add 2 more older ones
  update usage_log set created_at = now() - interval '2 minutes' where token_id = t;
  r := authorize_request('h1');
  assert (r->>'allowed')::boolean, 'minute window too wide: ' || r::text;
  insert into usage_log (token_id, surface, outcome, created_at)
  select t, 'endpoint', 'completed', now() - interval '2 minutes' from generate_series(1,2);
  r := authorize_request('h1');
  assert r->>'reason' = 'rate_limit_day', r::text;
  delete from usage_log;

  -- spend cap counts all traffic this month, both surfaces
  insert into usage_log (surface, outcome, estimated_cost_usd) values ('demo', 'answered', 10.5);
  r := authorize_request('h1');
  assert r->>'reason' = 'spend_cap', r::text;
  delete from usage_log;

  update student_tokens set revoked_at = now() where id = t;
  r := authorize_request('h1');
  assert r->>'reason' = 'revoked', r::text;

  update app_settings set value = 'true' where key = 'kill_switch';
  r := authorize_request('nope');
  assert r->>'reason' = 'kill_switch', 'kill switch must win over everything: ' || r::text;
  raise notice 'ALL FUNCTION CHECKS PASSED';
end $$;

-- authorize_demo_request: every outcome, counted per IP on the demo surface only
do $$
declare r jsonb;
begin
  r := authorize_demo_request('ip1');
  assert r->>'reason' = 'kill_switch', 'kill switch still on from the block above: ' || r::text;
  update app_settings set value = 'false' where key = 'kill_switch';
  update app_settings set value = '3' where key = 'demo_rpm_per_ip';

  r := authorize_demo_request('ip1');
  assert (r->>'allowed')::boolean and (r->>'remaining_minute')::int = 2, r::text;
  assert r->'reason' = 'null'::jsonb, 'allowed must carry a null reason: ' || r::text;

  -- denied rows, endpoint rows, other IPs and rows older than a minute don't count
  insert into usage_log (surface, client_ip_hash, outcome)
  select 'demo', 'ip1', 'denied' from generate_series(1,10);
  insert into usage_log (surface, client_ip_hash, outcome)
  select 'endpoint', 'ip1', 'completed' from generate_series(1,10);
  insert into usage_log (surface, client_ip_hash, outcome)
  select 'demo', 'ip2', 'answered' from generate_series(1,10);
  insert into usage_log (surface, client_ip_hash, outcome, created_at)
  select 'demo', 'ip1', 'answered', now() - interval '2 minutes' from generate_series(1,10);
  r := authorize_demo_request('ip1');
  assert (r->>'allowed')::boolean and (r->>'remaining_minute')::int = 2, 'wrong rows counted: ' || r::text;

  insert into usage_log (surface, client_ip_hash, outcome) values ('demo', 'ip1', 'answered'), ('demo', 'ip1', 'declined');
  r := authorize_demo_request('ip1');
  assert (r->>'allowed')::boolean and (r->>'remaining_minute')::int = 0, r::text;
  insert into usage_log (surface, client_ip_hash, outcome) values ('demo', 'ip1', 'emergency');
  r := authorize_demo_request('ip1');
  assert r->>'reason' = 'rate_limit_minute' and (r->>'retry_after_seconds')::int = 60, r::text;
  assert not (r->>'allowed')::boolean, r::text;
  r := authorize_demo_request('ip3');
  assert (r->>'allowed')::boolean, 'one IP at its limit must not block another: ' || r::text;

  -- spend cap counts all traffic this month and wins over the rate limit
  insert into usage_log (token_id, surface, outcome, estimated_cost_usd) values (null, 'endpoint', 'completed', 10.5);
  r := authorize_demo_request('ip1');
  assert r->>'reason' = 'spend_cap', r::text;
  update usage_log set created_at = date_trunc('month', now() at time zone 'America/New_York') at time zone 'America/New_York' - interval '1 second'
   where estimated_cost_usd is not null;
  r := authorize_demo_request('ip3');
  assert (r->>'allowed')::boolean, 'last month''s spend counted: ' || r::text;
  delete from usage_log;

  update app_settings set value = 'true' where key = 'kill_switch';
  r := authorize_demo_request('ip3');
  assert r->>'reason' = 'kill_switch', 'kill switch must win over everything: ' || r::text;
  raise notice 'ALL DEMO FUNCTION CHECKS PASSED';
end $$;

-- Anonymous roles are locked out of tables and functions
set role anon;
do $$ begin
  begin perform 1 from public.student_tokens; raise exception 'anon could read student_tokens';
  exception when insufficient_privilege then null; end;
  begin perform public.authorize_request('x'); raise exception 'anon could execute authorize_request';
  exception when insufficient_privilege then null; end;
  begin perform public.authorize_demo_request('x'); raise exception 'anon could execute authorize_demo_request';
  exception when insufficient_privilege then null; end;
  raise notice 'ANON LOCKED OUT';
end $$;
reset role;
set role service_role;
select (public.authorize_request('nope'))->>'reason' as service_role_can_call;
select (public.authorize_demo_request('x'))->>'reason' as service_role_can_call_demo;
