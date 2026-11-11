-- Migration 3: more surfaces write to usage_log, and the open ones share one per-visitor limit.
-- booking = the appointment agent, mcp = the MCP server, eval = the answer-quality eval in CI.

alter table public.usage_log drop constraint usage_log_surface_check;
alter table public.usage_log add constraint usage_log_surface_check
  check (surface in ('endpoint', 'demo', 'booking', 'mcp', 'eval'));

-- Same checks as before. The only change: the per-minute count now covers every open surface,
-- so one visitor gets one allowance across the demo page, the booking agent and the MCP server.
create or replace function public.authorize_demo_request(p_ip_hash text)
returns jsonb
language plpgsql
stable
set search_path = public
as $$
declare
  v_cap numeric;
  v_spent numeric;
  v_limit int;
  v_minute int;
  -- Months follow class time (ET), not UTC
  v_month_start timestamptz :=
    date_trunc('month', now() at time zone 'America/New_York') at time zone 'America/New_York';
begin
  if coalesce((select (value #>> '{}')::boolean from app_settings where key = 'kill_switch'), false)
  then
    return jsonb_build_object('allowed', false, 'reason', 'kill_switch');
  end if;

  -- One budget for every surface: the same sum authorize_request uses
  select (value #>> '{}')::numeric into v_cap from app_settings where key = 'spend_cap_usd';
  select coalesce(sum(estimated_cost_usd), 0) into v_spent
    from usage_log where created_at >= v_month_start;
  if v_cap is not null and v_spent >= v_cap then
    return jsonb_build_object('allowed', false, 'reason', 'spend_cap');
  end if;

  -- Denied requests don't count, and check-then-log is not atomic: see authorize_request.
  -- Class-endpoint rows carry a token, not an address, so they never match here.
  select (value #>> '{}')::int into v_limit from app_settings where key = 'demo_rpm_per_ip';
  select count(*) into v_minute
    from usage_log
   where client_ip_hash = p_ip_hash
     and outcome <> 'denied'
     and created_at >= now() - interval '1 minute';

  if v_limit is not null and v_minute >= v_limit then
    return jsonb_build_object('allowed', false, 'reason', 'rate_limit_minute',
      'retry_after_seconds', 60);
  end if;

  return jsonb_build_object(
    'allowed', true,
    'reason', null,
    'remaining_minute', v_limit - v_minute - 1
  );
end;
$$;
