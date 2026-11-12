-- Assertions for the emailed-link token flow: the class code gate, the per-email cap,
-- one use per link, expiry, replacing an earlier token, hand-minted tokens kept.
\set ON_ERROR_STOP on
do $$
declare r jsonb; n int;
begin
  -- earlier check files leave the kill switch on
  update app_settings set value = 'false' where key = 'kill_switch';
  delete from usage_log;

  r := start_token_request('ada@example.com', 'code', 'link-1', 900, 3, 'ip');
  assert r->>'reason' = 'signups_closed', 'no class code set must mean closed: ' || r::text;

  update app_settings set value = '"code"' where key = 'class_code_hash';
  r := start_token_request('ada@example.com', 'guess', 'link-1', 900, 3, 'ip');
  assert r->>'reason' = 'wrong_code', r::text;
  select count(*) into n from token_requests;
  assert n = 0, 'a wrong code must store nothing';

  r := start_token_request('Ada@Example.com', 'code', 'link-1', 900, 3, 'ip');
  assert (r->>'started')::boolean, r::text;
  select count(*) into n from student_tokens where email = 'ada@example.com';
  assert n = 0, 'no token may exist before the link is opened';

  r := redeem_token_request('not-a-link', 'h-x', 'sk-bootcamp-x');
  assert r->>'reason' = 'link_invalid', r::text;

  r := redeem_token_request('link-1', 'h-a1', 'sk-bootcamp-a1');
  assert (r->>'issued')::boolean and not (r->>'replaced')::boolean, r::text;
  assert r->>'email' = 'ada@example.com', 'email must come from the request, lowercased: ' || r::text;
  r := authorize_request('h-a1');
  assert (r->>'allowed')::boolean, 'a redeemed token must work: ' || r::text;

  -- a link works once
  r := redeem_token_request('link-1', 'h-a9', 'sk-bootcamp-a9');
  assert r->>'reason' = 'link_used', r::text;
  r := authorize_request('h-a9');
  assert r->>'reason' = 'invalid_token', 'a reused link must not store a token: ' || r::text;

  -- a second request replaces the first token, but only once its link is opened
  r := start_token_request('ada@example.com', 'code', 'link-2', 900, 3, 'ip');
  assert (r->>'started')::boolean, r::text;
  r := authorize_request('h-a1');
  assert (r->>'allowed')::boolean, 'asking for a link must not turn off the current token';
  r := redeem_token_request('link-2', 'h-a2', 'sk-bootcamp-a2');
  assert (r->>'issued')::boolean and (r->>'replaced')::boolean, r::text;
  r := authorize_request('h-a1');
  assert r->>'reason' = 'revoked', 'the earlier token must stop working: ' || r::text;

  -- an expired link is refused
  r := start_token_request('ada@example.com', 'code', 'link-3', 900, 3, 'ip');
  update token_requests set expires_at = now() - interval '1 second' where link_hash = 'link-3';
  r := redeem_token_request('link-3', 'h-a3', 'sk-bootcamp-a3');
  assert r->>'reason' = 'link_expired', r::text;

  -- three links an hour per address, then no more; another address is unaffected
  r := start_token_request('ada@example.com', 'code', 'link-4', 900, 3, 'ip');
  assert r->>'reason' = 'too_many_links', r::text;
  r := start_token_request('grace@example.com', 'code', 'link-5', 900, 3, 'ip');
  assert (r->>'started')::boolean, r::text;
  update token_requests set created_at = now() - interval '2 hours' where email = 'ada@example.com';
  r := start_token_request('ada@example.com', 'code', 'link-6', 900, 3, 'ip');
  assert (r->>'started')::boolean, 'requests older than an hour must not count: ' || r::text;

  -- a token the instructor minted by hand is never replaced by the form
  insert into student_tokens (label, email, token_hash, token_prefix)
  values ('Instructor', 'teacher@example.com', 'h-t1', 'sk-bootcamp-t1');
  r := start_token_request('teacher@example.com', 'code', 'link-7', 900, 3, 'ip');
  r := redeem_token_request('link-7', 'h-t2', 'sk-bootcamp-t2');
  assert (r->>'issued')::boolean and not (r->>'replaced')::boolean, r::text;
  r := authorize_request('h-t1');
  assert (r->>'allowed')::boolean, 'hand-minted token was revoked: ' || r::text;

  insert into usage_log (surface, outcome) values ('token', 'issued');
  delete from usage_log; delete from student_tokens; delete from token_requests;
  update app_settings set value = 'null' where key = 'class_code_hash';
  raise notice 'ALL TOKEN REQUEST CHECKS PASSED';
end $$;

set role anon;
do $$
begin
  perform redeem_token_request('l', 'h', 'p');
  raise exception 'anon could call redeem_token_request';
exception when insufficient_privilege then
  raise notice 'ANON LOCKED OUT OF TOKEN REQUESTS';
end $$;
reset role;
