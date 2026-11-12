-- Migration 7: a class token is now reached through a link sent to the student's email.
-- Step 1 (start_token_request): check the class code and record a pending request. The server
-- then emails a one-time link. Step 2 (redeem_token_request): the link is opened, the request
-- is used up, and the token is stored. Only hashes of the link and the token are kept.

create table public.token_requests (
  id bigint generated always as identity primary key,
  email text not null,
  link_hash text not null unique,
  expires_at timestamptz not null,
  used_at timestamptz,
  client_ip_hash text,
  created_at timestamptz not null default now()
);
create index token_requests_email_created_idx on public.token_requests (email, created_at);

alter table public.token_requests enable row level security;
revoke all on table public.token_requests from anon, authenticated;

-- Check the class code and record one pending request for this email.
-- p_max_per_hour caps links per address, so the form cannot be used to flood someone's inbox.
create function public.start_token_request(
  p_email text,
  p_code_hash text,
  p_link_hash text,
  p_ttl_seconds int,
  p_max_per_hour int,
  p_ip_hash text
)
returns jsonb
language plpgsql
set search_path = public
as $$
declare
  v_code_hash text;
  v_recent int;
begin
  select value #>> '{}' into v_code_hash from app_settings where key = 'class_code_hash';
  if v_code_hash is null or v_code_hash = '' then
    return jsonb_build_object('started', false, 'reason', 'signups_closed');
  end if;
  if v_code_hash <> p_code_hash then
    return jsonb_build_object('started', false, 'reason', 'wrong_code');
  end if;

  select count(*) into v_recent from token_requests
   where email = lower(p_email) and created_at >= now() - interval '1 hour';
  if v_recent >= p_max_per_hour then
    return jsonb_build_object('started', false, 'reason', 'too_many_links');
  end if;

  insert into token_requests (email, link_hash, expires_at, client_ip_hash)
  values (lower(p_email), p_link_hash, now() + make_interval(secs => p_ttl_seconds), p_ip_hash);
  return jsonb_build_object('started', true, 'reason', null);
end;
$$;

-- Use a link exactly once: mark the request used, turn off this email's earlier self-service
-- token, store the new one. "for update" makes two clicks at the same moment take turns, so
-- the second one finds the request already used.
create function public.redeem_token_request(
  p_link_hash text,
  p_token_hash text,
  p_token_prefix text
)
returns jsonb
language plpgsql
set search_path = public
as $$
declare
  v_request public.token_requests%rowtype;
  v_replaced int;
begin
  select * into v_request from token_requests where link_hash = p_link_hash for update;
  if not found then
    return jsonb_build_object('issued', false, 'reason', 'link_invalid');
  end if;
  if v_request.used_at is not null then
    return jsonb_build_object('issued', false, 'reason', 'link_used');
  end if;
  if v_request.expires_at <= now() then
    return jsonb_build_object('issued', false, 'reason', 'link_expired');
  end if;

  update token_requests set used_at = now() where id = v_request.id;

  update student_tokens set revoked_at = now()
   where lower(email) = v_request.email and self_service and revoked_at is null;
  get diagnostics v_replaced = row_count;

  insert into student_tokens (label, email, token_hash, token_prefix, self_service)
  values (v_request.email, v_request.email, p_token_hash, p_token_prefix, true);

  return jsonb_build_object('issued', true, 'reason', null, 'email', v_request.email,
    'replaced', v_replaced > 0);
end;
$$;

revoke execute on function public.start_token_request(text, text, text, int, int, text)
  from public, anon, authenticated;
revoke execute on function public.redeem_token_request(text, text, text)
  from public, anon, authenticated;
grant execute on function public.start_token_request(text, text, text, int, int, text)
  to service_role;
grant execute on function public.redeem_token_request(text, text, text) to service_role;
