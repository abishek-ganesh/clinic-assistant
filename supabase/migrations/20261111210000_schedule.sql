-- Migration 4: a mock appointment schedule for the booking agent.
-- Open slots are computed from each provider's weekly blocks, never stored, so the schedule
-- never goes stale. Only bookings are rows. No patient name or detail is collected: the page
-- is public and the data stays synthetic.
-- Server-only like everything else: RLS on with no policies, functions for service_role only.

-- The fictional providers. location is where in-person visits happen.
create table public.providers (
  slug text primary key,
  name text not null,
  location text not null check (location in ('Main Street', 'Riverside'))
);
insert into public.providers (slug, name, location) values
  ('patel', 'Dr. Patel', 'Main Street'),
  ('lee', 'Dr. Lee', 'Main Street'),
  ('kim', 'Dr. Kim', 'Riverside');

-- Weekly blocks: on this weekday, this provider sees this kind of visit from starts to ends,
-- in back-to-back slots of slot_minutes. Blocks for one provider never overlap, so two slots
-- can never cover the same minutes. weekday is ISO: 1 = Monday ... 6 = Saturday.
-- New patient visits and same-day visits are missing on purpose: the handbook says those
-- are booked by phone.
create table public.provider_blocks (
  provider text not null references public.providers (slug),
  weekday int not null check (weekday between 1 and 6),
  visit_type text not null check (visit_type in ('returning', 'physical', 'telehealth')),
  starts time not null,
  ends time not null,
  slot_minutes int not null check (slot_minutes in (20, 40)),
  primary key (provider, weekday, starts),
  check (starts < ends)
);
insert into public.provider_blocks (provider, weekday, visit_type, starts, ends, slot_minutes)
-- Dr. Patel, Main Street, Monday to Friday: physicals first, then returning visits
select 'patel', d, 'physical', '08:00'::time, '09:20'::time, 40 from generate_series(1, 5) d
union all
select 'patel', d, 'returning', '09:40', '11:00', 20 from generate_series(1, 5) d
union all
-- Dr. Lee, Main Street, Monday, Wednesday and Friday afternoons, plus Saturday morning.
-- Saturday is Main Street only, 9 to 1, returning patients only.
select 'lee', d, 'returning', '14:00', '15:20', 20 from unnest(array[1, 3, 5]) d
union all
select 'lee', d, 'telehealth', '16:00', '17:20', 20 from unnest(array[1, 3, 5]) d
union all
select 'lee', 6, 'returning', '10:00', '11:20', 20
union all
-- Dr. Kim, Riverside (closes at 5), Tuesday and Thursday
select 'kim', d, 'telehealth', '09:00', '10:20', 20 from unnest(array[2, 4]) d
union all
select 'kim', d, 'returning', '13:00', '14:20', 20 from unnest(array[2, 4]) d
union all
select 'kim', d, 'physical', '15:00', '16:20', 40 from unnest(array[2, 4]) d;

-- Holiday closures from the handbook. closes_at null means closed all day; a time means the
-- clinic closes early and only slots that end by then are offered. Listed through
-- November 2027: add rows to extend.
create table public.clinic_closures (
  day date primary key,
  closes_at time,
  reason text not null
);
insert into public.clinic_closures (day, closes_at, reason) values
  ('2026-11-26', null, 'Thanksgiving Day'),
  ('2026-11-27', null, 'Friday after Thanksgiving'),
  ('2026-12-24', '12:00', 'Christmas Eve'),
  ('2026-12-25', null, 'Christmas Day'),
  ('2026-12-31', '12:00', 'New Year''s Eve'),
  ('2027-01-01', null, 'New Year''s Day'),
  ('2027-05-31', null, 'Memorial Day'),
  ('2027-07-05', null, 'Independence Day (observed)'),
  ('2027-09-06', null, 'Labor Day'),
  ('2027-11-25', null, 'Thanksgiving Day'),
  ('2027-11-26', null, 'Friday after Thanksgiving');

-- One row per booked slot. The unique constraint is what makes double-booking impossible:
-- two requests racing for the same slot cannot both insert.
create table public.appointments (
  id uuid primary key default gen_random_uuid(),
  provider text not null references public.providers (slug),
  starts_at timestamptz not null,
  visit_type text not null,
  -- Short code a person can read back. The id is the real key.
  confirmation_code text not null,
  client_ip_hash text,
  created_at timestamptz not null default now(),
  unique (provider, starts_at)
);
create index appointments_ip_created_idx on public.appointments (client_ip_hash, created_at);

-- The most slots one visitor may book in 24 hours, so one script cannot fill the schedule.
insert into public.app_settings (key, value) values ('booking_max_per_ip_per_day', '5');

alter table public.providers enable row level security;
alter table public.provider_blocks enable row level security;
alter table public.clinic_closures enable row level security;
alter table public.appointments enable row level security;

revoke all on table public.providers, public.provider_blocks, public.clinic_closures,
  public.appointments from anon, authenticated;

-- Every slot the clinic offers between two dates, booked or not: each weekly block cut into
-- slots, minus holiday closures. Both functions below read the schedule through this one.
-- It runs in clinic time, so dates, weekdays and clock times all mean Eastern.
-- slot_id is "<provider slug>:<local start>", for example patel:2026-12-01T09:40.
create function public.schedule_slots(p_from date, p_to date)
returns table (
  slot_id text,
  provider text,
  provider_name text,
  visit_type text,
  starts_at timestamptz,
  display text
)
language sql
stable
set search_path = public
set timezone = 'America/New_York'
as $$
  select b.provider || ':' || to_char(s.slot_start, 'YYYY-MM-DD"T"HH24:MI'),
         b.provider,
         p.name,
         b.visit_type,
         s.slot_start::timestamptz,
         -- "Tue Dec 1, 9:40 AM with Dr. Patel (Main Street)"
         to_char(s.slot_start, 'Dy Mon FMDD, FMHH12:MI AM') || ' with ' || p.name
           || case when b.visit_type = 'telehealth' then ' (video visit)'
                   else ' (' || p.location || ')' end
    from generate_series(p_from::timestamp, p_to::timestamp, interval '1 day') as d (day)
    join provider_blocks b on b.weekday = extract(isodow from d.day)
    join providers p on p.slug = b.provider
    left join clinic_closures c on c.day = d.day::date
    cross join lateral generate_series(
      d.day + b.starts,
      d.day + b.ends - make_interval(mins => b.slot_minutes),
      make_interval(mins => b.slot_minutes)
    ) as s (slot_start)
   where c.day is null
      or s.slot_start + make_interval(mins => b.slot_minutes) <= d.day + c.closes_at;
$$;

-- Open slots, soonest first. Booking opens tomorrow (same-day visits are phone only) and
-- covers 14 days. starts_at is ISO 8601 with the clinic's UTC offset.
create function public.available_slots(
  p_visit_type text default null,
  p_from date default null,
  p_limit int default 5
)
returns table (
  slot_id text,
  provider text,
  visit_type text,
  starts_at text,
  display text
)
language sql
stable
set search_path = public
set timezone = 'America/New_York'
as $$
  select s.slot_id,
         s.provider_name,
         s.visit_type,
         to_char(s.starts_at, 'YYYY-MM-DD"T"HH24:MI:SSTZH:TZM'),
         s.display
    from schedule_slots(
           greatest(coalesce(p_from, current_date + 1), current_date + 1),
           current_date + 14
         ) s
   where (p_visit_type is null or s.visit_type = p_visit_type)
     and not exists (
           select 1 from appointments a
            where a.provider = s.provider and a.starts_at = s.starts_at
         )
   order by s.starts_at, s.provider
   limit least(greatest(coalesce(p_limit, 5), 1), 20);
$$;

-- Book one slot. p_starts_local is clinic wall-clock time. Returns {"booked": true, ...} or
-- {"booked": false, "reason": "invalid_slot" | "booking_limit" | "slot_taken"}.
-- Booking the same slot twice from the same visitor returns the first booking again.
create function public.book_slot(p_provider text, p_starts_local timestamp, p_ip_hash text)
returns jsonb
language plpgsql
set search_path = public
set timezone = 'America/New_York'
as $$
declare
  v_slot record;
  v_limit int;
  v_recent int;
  v_code text;
begin
  -- Only a slot the schedule really offers can be booked: nobody books 3 AM or a Sunday
  select * into v_slot
    from schedule_slots(current_date + 1, current_date + 14) s
   where s.provider = p_provider and s.starts_at = p_starts_local::timestamptz;
  if not found then
    return jsonb_build_object('booked', false, 'reason', 'invalid_slot');
  end if;

  select (value #>> '{}')::int into v_limit
    from app_settings where key = 'booking_max_per_ip_per_day';
  select count(*) into v_recent
    from appointments
   where client_ip_hash = p_ip_hash and created_at >= now() - interval '1 day';
  if v_limit is not null and v_recent >= v_limit then
    return jsonb_build_object('booked', false, 'reason', 'booking_limit');
  end if;

  v_code := 'FC-' || upper(substr(replace(gen_random_uuid()::text, '-', ''), 1, 6));
  begin
    insert into appointments (provider, starts_at, visit_type, confirmation_code, client_ip_hash)
    values (v_slot.provider, v_slot.starts_at, v_slot.visit_type, v_code, p_ip_hash);
  exception when unique_violation then
    -- Already booked. If this same visitor booked it (a repeated Confirm, or a retry after a
    -- lost response), answer with that booking again instead of an error. Otherwise someone
    -- else got there between the availability check and this insert.
    select confirmation_code into v_code
      from appointments
     where provider = v_slot.provider and starts_at = v_slot.starts_at
       and client_ip_hash = p_ip_hash;
    if not found then
      return jsonb_build_object('booked', false, 'reason', 'slot_taken');
    end if;
  end;

  return jsonb_build_object(
    'booked', true,
    'confirmation_code', v_code,
    'slot_id', v_slot.slot_id,
    'visit_type', v_slot.visit_type,
    'display', v_slot.display
  );
end;
$$;

revoke execute on function public.schedule_slots(date, date) from public, anon, authenticated;
revoke execute on function public.available_slots(text, date, int) from public, anon, authenticated;
revoke execute on function public.book_slot(text, timestamp, text) from public, anon, authenticated;
grant execute on function public.schedule_slots(date, date) to service_role;
grant execute on function public.available_slots(text, date, int) to service_role;
grant execute on function public.book_slot(text, timestamp, text) to service_role;
