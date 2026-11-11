-- Migration 4: the numbers behind the public /stats page, computed next to the data.
-- Aggregates only. The function reads counts, sums and percentiles from usage_log and returns
-- nothing that identifies a caller: no token ids or labels, no IP hashes, no per-request rows.

-- Usage for the last p_days calendar days (Eastern time, today included), plus month-to-date
-- spend next to the cap. One round trip; the app never pulls raw rows.
create function public.usage_stats(p_days int default 30)
returns jsonb
language sql
stable
set search_path = public
as $$
  with bounds as (
    -- Days follow class time (ET), like the limits. The window is clamped to 1..90 days.
    select b.days, b.today, b.today - (b.days - 1) as first_day
    from (
      select greatest(1, least(coalesce(p_days, 30), 90)) as days,
             (now() at time zone 'America/New_York')::date as today
    ) b
  ),
  recent as (
    -- Only the columns the aggregates need ever leave the table
    select u.surface, u.outcome, u.latency_ms, u.input_tokens, u.output_tokens,
           u.estimated_cost_usd,
           (u.created_at at time zone 'America/New_York')::date as day
    from usage_log u, bounds
    where u.created_at >= bounds.first_day::timestamp at time zone 'America/New_York'
  ),
  by_surface as (
    select surface, count(*) as requests, coalesce(sum(estimated_cost_usd), 0) as cost_usd
    from recent group by surface
  ),
  demo_outcomes as (
    select outcome, count(*) as requests
    from recent where surface = 'demo' group by outcome
  ),
  demo as (
    -- Rates are out of the questions the pipeline finished: denials and errors are not answers
    select coalesce(sum(requests), 0) as requests,
           coalesce(sum(requests) filter (where outcome = 'declined'), 0) as declined,
           coalesce(sum(requests) filter (where outcome = 'emergency'), 0) as emergency,
           coalesce(sum(requests)
             filter (where outcome in ('answered', 'declined', 'emergency')), 0) as completed
    from demo_outcomes
  ),
  latency as (
    select
      count(*) filter (where surface = 'demo' and outcome = 'answered') as demo_count,
      percentile_cont(0.5) within group (order by latency_ms)
        filter (where surface = 'demo' and outcome = 'answered') as demo_p50,
      percentile_cont(0.95) within group (order by latency_ms)
        filter (where surface = 'demo' and outcome = 'answered') as demo_p95,
      count(*) filter (where surface = 'endpoint' and outcome = 'answered') as endpoint_count,
      percentile_cont(0.5) within group (order by latency_ms)
        filter (where surface = 'endpoint' and outcome = 'answered') as endpoint_p50,
      percentile_cont(0.95) within group (order by latency_ms)
        filter (where surface = 'endpoint' and outcome = 'answered') as endpoint_p95
    from recent
    where latency_ms is not null
  ),
  daily as (
    -- generate_series supplies every day in the window, so a quiet day shows as zero
    select g.day::date as day, count(r.day) as requests,
           coalesce(sum(r.estimated_cost_usd), 0) as cost_usd
    from bounds,
         generate_series(bounds.first_day, bounds.today, interval '1 day') as g(day)
    left join recent r on r.day = g.day::date
    group by g.day
  )
  select jsonb_build_object(
    'days', bounds.days,
    'first_day', bounds.first_day,
    'last_day', bounds.today,
    'totals', (
      select jsonb_build_object(
        'requests', count(*),
        'cost_usd', coalesce(sum(estimated_cost_usd), 0),
        'input_tokens', coalesce(sum(input_tokens), 0),
        'output_tokens', coalesce(sum(output_tokens), 0))
      from recent),
    'by_surface', (
      select coalesce(jsonb_agg(to_jsonb(s) order by s.requests desc, s.surface), '[]'::jsonb)
      from by_surface s),
    'demo', (
      select jsonb_build_object(
        'requests', d.requests,
        'completed', d.completed,
        'decline_rate', round(d.declined / nullif(d.completed, 0), 4),
        'emergency_rate', round(d.emergency / nullif(d.completed, 0), 4),
        'outcomes', (
          select coalesce(jsonb_agg(to_jsonb(o) order by o.requests desc, o.outcome), '[]'::jsonb)
          from demo_outcomes o))
      from demo d),
    'latency', (
      select jsonb_build_object(
        'demo_answered', jsonb_build_object(
          'count', l.demo_count, 'p50_ms', round(l.demo_p50), 'p95_ms', round(l.demo_p95)),
        'endpoint_completions', jsonb_build_object(
          'count', l.endpoint_count,
          'p50_ms', round(l.endpoint_p50), 'p95_ms', round(l.endpoint_p95)))
      from latency l),
    'daily', (select jsonb_agg(to_jsonb(d) order by d.day) from daily d),
    'month', jsonb_build_object(
      -- The same sum and the same month start the authorize functions use for the spend cap
      'spend_usd', (
        select coalesce(sum(estimated_cost_usd), 0) from usage_log
        where created_at >= date_trunc('month', now() at time zone 'America/New_York')
                            at time zone 'America/New_York'),
      'cap_usd', (select (value #>> '{}')::numeric from app_settings where key = 'spend_cap_usd'))
  )
  from bounds;
$$;

revoke execute on function public.usage_stats(int) from public, anon, authenticated;
grant execute on function public.usage_stats(int) to service_role;
