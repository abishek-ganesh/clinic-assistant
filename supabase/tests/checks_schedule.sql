-- Assertions for the schedule migration: the generated slots match the handbook's hours,
-- booked slots disappear, and book_slot refuses everything that is not a real open slot.
\set ON_ERROR_STOP on
set timezone = 'America/New_York';

-- Split "patel:2026-12-01T09:40" the way app/schedule.py does
create function pg_temp.slot_provider(slot_id text) returns text language sql as $$
  select split_part(slot_id, ':', 1) $$;
create function pg_temp.slot_start(slot_id text) returns timestamp language sql as $$
  select substr(slot_id, position(':' in slot_id) + 1)::timestamp $$;

do $$
declare
  n int;
  r jsonb;
  s record;
  local_start timestamp;
  code text;
  tomorrow date := current_date + 1;
begin
  -- A known week, Mon Dec 7 to Sun Dec 13, 2026 (no holidays)
  select count(*) into n from schedule_slots('2026-12-13', '2026-12-13');
  assert n = 0, 'Sunday must have no slots';
  select count(*) into n from schedule_slots('2026-12-12', '2026-12-12')
   where provider <> 'lee' or visit_type <> 'returning';
  assert n = 0, 'Saturday is Main Street, returning patients only';
  select count(*) into n from schedule_slots('2026-12-12', '2026-12-12')
   where starts_at::time < '09:00' or starts_at::time >= '13:00';
  assert n = 0, 'Saturday slots must sit inside 9 am to 1 pm';
  select count(*) into n from schedule_slots('2026-12-12', '2026-12-12');
  assert n = 4, 'expected 4 Saturday slots, got ' || n;

  -- Monday: Patel 2 physicals + 4 returning, Lee 4 returning + 4 telehealth
  select count(*) into n from schedule_slots('2026-12-07', '2026-12-07');
  assert n = 14, 'expected 14 Monday slots, got ' || n;
  select count(*) into n from schedule_slots('2026-12-07', '2026-12-11')
   where starts_at::time < '08:00' or starts_at::time >= '18:00';
  assert n = 0, 'weekday slots must sit inside 8 am to 6 pm';
  select count(*) into n from schedule_slots('2026-12-07', '2026-12-11')
   where provider = 'kim' and starts_at::time >= '16:40';
  assert n = 0, 'Riverside closes at 5 pm';
  select count(*) into n from schedule_slots('2026-12-07', '2026-12-13')
   where visit_type not in ('returning', 'physical', 'telehealth');
  assert n = 0, 'new patient visits are booked by phone, never online';

  -- Physicals are 40 minutes apart, returning visits 20
  select count(*) into n from schedule_slots('2026-12-07', '2026-12-07')
   where provider = 'patel' and visit_type = 'physical'
     and starts_at::time in ('08:00', '08:40');
  assert n = 2, 'Patel physicals should start at 8:00 and 8:40';

  -- Holidays: closed all day, or only slots that end by the early close
  select count(*) into n from schedule_slots('2026-11-26', '2026-11-27');
  assert n = 0, 'Thanksgiving and the Friday after are closed';
  select count(*) into n from schedule_slots('2026-12-25', '2026-12-25');
  assert n = 0, 'Christmas Day is closed';
  select count(*) into n from schedule_slots('2026-12-24', '2026-12-24')
   where starts_at::time >= '11:41';
  assert n = 0, 'Christmas Eve closes at noon';
  select count(*) into n from schedule_slots('2026-12-24', '2026-12-24');
  assert n > 0, 'Christmas Eve morning is open';

  -- slot_id and display formats
  select * into s from schedule_slots('2026-12-07', '2026-12-07')
   where provider = 'patel' and visit_type = 'returning' order by starts_at limit 1;
  assert s.slot_id = 'patel:2026-12-07T09:40', s.slot_id;
  assert s.display = 'Mon Dec 7, 9:40 AM with Dr. Patel (Main Street)', s.display;
  select * into s from schedule_slots('2026-12-07', '2026-12-07')
   where visit_type = 'telehealth' order by starts_at limit 1;
  assert s.display = 'Mon Dec 7, 4:00 PM with Dr. Lee (video visit)', s.display;

  -- available_slots: starts tomorrow, covers 14 days, honors the filters and the limit
  select count(*) into n from available_slots();
  assert n = 5, 'default limit is 5, got ' || n;
  select count(*) into n from available_slots(null, null, 500);
  assert n = 20, 'limit is capped at 20, got ' || n;
  select count(*) into n from available_slots(null, current_date - 30, 20)
   where starts_at::timestamptz::date < tomorrow;
  assert n = 0, 'never offers today or the past';
  select count(*) into n from available_slots(null, current_date + 15, 20);
  assert n = 0, 'nothing beyond the 14-day window';
  select count(*) into n from available_slots('physical', null, 20) where visit_type <> 'physical';
  assert n = 0, 'visit type filter leaked';
  select count(*) into n from available_slots('new_patient', null, 20);
  assert n = 0, 'unknown visit types return nothing';
  select count(*) into n from available_slots(null, current_date + 7, 20)
   where starts_at::timestamptz::date < current_date + 7;
  assert n = 0, 'p_from ignored';
  select * into s from available_slots() limit 1;
  assert s.starts_at ~ '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:00-0[45]:00$', s.starts_at;
  assert s.provider like 'Dr. %', s.provider;

  -- book_slot: the first open slot books once
  select * into s from available_slots() limit 1;
  local_start := pg_temp.slot_start(s.slot_id);
  r := book_slot(pg_temp.slot_provider(s.slot_id), local_start, 'ip1');
  assert (r->>'booked')::boolean, r::text;
  assert r->>'confirmation_code' ~ '^FC-[0-9A-F]{6}$', r::text;
  assert r->>'slot_id' = s.slot_id and r->>'display' = s.display, r::text;
  select count(*) into n from available_slots(null, null, 20) a where a.slot_id = s.slot_id;
  assert n = 0, 'a booked slot must leave the available list';

  -- the same slot again: another visitor is refused, the same visitor gets the same booking
  -- back (a repeated Confirm), and neither adds a row
  code := r->>'confirmation_code';
  r := book_slot(pg_temp.slot_provider(s.slot_id), local_start, 'ip2');
  assert r->>'reason' = 'slot_taken', r::text;
  r := book_slot(pg_temp.slot_provider(s.slot_id), local_start, 'ip1');
  assert (r->>'booked')::boolean and r->>'confirmation_code' = code, 'repeat not idempotent: ' || r::text;
  select count(*) into n from appointments;
  assert n = 1, 'double booking created a row';

  -- times the schedule does not offer
  r := book_slot('patel', (tomorrow + time '03:00'), 'ip1');
  assert r->>'reason' = 'invalid_slot', '3 AM: ' || r::text;
  r := book_slot('patel', (tomorrow + 7 + time '09:45'), 'ip1');
  assert r->>'reason' = 'invalid_slot', 'off the slot grid: ' || r::text;
  r := book_slot('nobody', local_start, 'ip1');
  assert r->>'reason' = 'invalid_slot', 'unknown provider: ' || r::text;
  r := book_slot(pg_temp.slot_provider(s.slot_id), local_start + interval '21 days', 'ip1');
  assert r->>'reason' = 'invalid_slot', 'beyond the window: ' || r::text;
  r := book_slot(pg_temp.slot_provider(s.slot_id), local_start - interval '14 days', 'ip1');
  assert r->>'reason' = 'invalid_slot', 'in the past: ' || r::text;
  select count(*) into n from appointments;
  assert n = 1, 'a refused booking created a row';

  -- per-visitor daily limit: ip1 has 1 booking; allow 2, then refuse the third
  update app_settings set value = '2' where key = 'booking_max_per_ip_per_day';
  select * into s from available_slots() limit 1;
  local_start := pg_temp.slot_start(s.slot_id);
  r := book_slot(pg_temp.slot_provider(s.slot_id), local_start, 'ip1');
  assert (r->>'booked')::boolean, r::text;
  select * into s from available_slots() limit 1;
  local_start := pg_temp.slot_start(s.slot_id);
  r := book_slot(pg_temp.slot_provider(s.slot_id), local_start, 'ip1');
  assert r->>'reason' = 'booking_limit', r::text;
  r := book_slot(pg_temp.slot_provider(s.slot_id), local_start, 'ip3');
  assert (r->>'booked')::boolean, 'one visitor at the limit must not block another: ' || r::text;
  -- bookings older than a day stop counting
  update appointments set created_at = now() - interval '25 hours' where client_ip_hash = 'ip1';
  select * into s from available_slots() limit 1;
  local_start := pg_temp.slot_start(s.slot_id);
  r := book_slot(pg_temp.slot_provider(s.slot_id), local_start, 'ip1');
  assert (r->>'booked')::boolean, 'old bookings still counted: ' || r::text;

  -- the table itself refuses a duplicate, whatever the function does
  begin
    insert into appointments (provider, starts_at, visit_type, confirmation_code)
    select provider, starts_at, visit_type, 'FC-DUP' from appointments limit 1;
    raise exception 'duplicate (provider, starts_at) was accepted';
  exception when unique_violation then null;
  end;

  delete from appointments;
  raise notice 'ALL SCHEDULE CHECKS PASSED';
end $$;

-- Anonymous roles are locked out of the schedule tables and functions
set role anon;
do $$ begin
  begin perform 1 from public.appointments; raise exception 'anon could read appointments';
  exception when insufficient_privilege then null; end;
  begin perform 1 from public.providers; raise exception 'anon could read providers';
  exception when insufficient_privilege then null; end;
  begin perform 1 from public.available_slots(); raise exception 'anon could execute available_slots';
  exception when insufficient_privilege then null; end;
  begin perform 1 from public.schedule_slots(current_date, current_date); raise exception 'anon could execute schedule_slots';
  exception when insufficient_privilege then null; end;
  begin perform public.book_slot('patel', now()::timestamp, 'x'); raise exception 'anon could execute book_slot';
  exception when insufficient_privilege then null; end;
  raise notice 'ANON LOCKED OUT OF THE SCHEDULE';
end $$;
reset role;
set role service_role;
select count(*) as service_role_sees_slots from public.available_slots();
select (public.book_slot('patel', now()::timestamp, 'x'))->>'reason' as service_role_can_call_book;
reset role;
