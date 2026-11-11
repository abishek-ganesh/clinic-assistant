-- Migration 6: students request their own class token on the /token page.
-- The page asks for an email and the class code the instructor shares in class. The server
-- hashes the code and mints the token; this function checks the code and stores the token.

alter table public.usage_log drop constraint usage_log_surface_check;
alter table public.usage_log add constraint usage_log_surface_check
  check (surface in ('endpoint', 'demo', 'booking', 'mcp', 'eval', 'token'));

-- Tokens a student requested themselves. Asking again replaces only these, so nobody can
-- use the form to turn off a token the instructor minted by hand.
alter table public.student_tokens add column self_service boolean not null default false;

-- sha256 hex of the class code. null means sign-ups are closed.
insert into public.app_settings (key, value) values ('class_code_hash', 'null');

-- Check the class code, turn off this email's earlier self-service token, store the new one.
-- One function so the three steps happen together or not at all.
create function public.request_token(
  p_email text,
  p_code_hash text,
  p_token_hash text,
  p_token_prefix text
)
returns jsonb
language plpgsql
set search_path = public
as $$
declare
  v_code_hash text;
  v_replaced int;
begin
  select value #>> '{}' into v_code_hash from app_settings where key = 'class_code_hash';
  if v_code_hash is null or v_code_hash = '' then
    return jsonb_build_object('issued', false, 'reason', 'signups_closed');
  end if;
  if v_code_hash <> p_code_hash then
    return jsonb_build_object('issued', false, 'reason', 'wrong_code');
  end if;

  update student_tokens set revoked_at = now()
   where lower(email) = lower(p_email) and self_service and revoked_at is null;
  get diagnostics v_replaced = row_count;

  insert into student_tokens (label, email, token_hash, token_prefix, self_service)
  values (lower(p_email), lower(p_email), p_token_hash, p_token_prefix, true);

  return jsonb_build_object('issued', true, 'reason', null, 'replaced', v_replaced > 0);
end;
$$;

revoke execute on function public.request_token(text, text, text, text)
  from public, anon, authenticated;
grant execute on function public.request_token(text, text, text, text) to service_role;
