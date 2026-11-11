-- Assertions for request_token: closed, wrong code, issued, replaced, hand-minted tokens kept.
\set ON_ERROR_STOP on
do $$
declare r jsonb; n int;
begin
  -- earlier check files leave the kill switch on
  update app_settings set value = 'false' where key = 'kill_switch';
  delete from usage_log;

  r := request_token('ada@example.com', 'code', 'h-a1', 'sk-bootcamp-a1');
  assert r->>'reason' = 'signups_closed', 'no class code set must mean closed: ' || r::text;

  update app_settings set value = '"code"' where key = 'class_code_hash';
  r := request_token('ada@example.com', 'guess', 'h-a1', 'sk-bootcamp-a1');
  assert r->>'reason' = 'wrong_code', r::text;
  select count(*) into n from student_tokens where email = 'ada@example.com';
  assert n = 0, 'a wrong code must store nothing';

  r := request_token('Ada@Example.com', 'code', 'h-a1', 'sk-bootcamp-a1');
  assert (r->>'issued')::boolean and not (r->>'replaced')::boolean, r::text;
  r := authorize_request('h-a1');
  assert (r->>'allowed')::boolean, 'a requested token must work: ' || r::text;

  -- asking again replaces the earlier token, whatever the capitalization
  r := request_token('ada@example.com', 'code', 'h-a2', 'sk-bootcamp-a2');
  assert (r->>'issued')::boolean and (r->>'replaced')::boolean, r::text;
  r := authorize_request('h-a1');
  assert r->>'reason' = 'revoked', 'the earlier token must stop working: ' || r::text;
  r := authorize_request('h-a2');
  assert (r->>'allowed')::boolean, r::text;

  -- a token the instructor minted by hand is never replaced by the form
  insert into student_tokens (label, email, token_hash, token_prefix)
  values ('Instructor', 'teacher@example.com', 'h-t1', 'sk-bootcamp-t1');
  r := request_token('teacher@example.com', 'code', 'h-t2', 'sk-bootcamp-t2');
  assert (r->>'issued')::boolean and not (r->>'replaced')::boolean, r::text;
  r := authorize_request('h-t1');
  assert (r->>'allowed')::boolean, 'hand-minted token was revoked: ' || r::text;

  insert into usage_log (surface, outcome) values ('token', 'issued');
  delete from usage_log; delete from student_tokens;
  update app_settings set value = 'null' where key = 'class_code_hash';
  raise notice 'ALL TOKEN REQUEST CHECKS PASSED';
end $$;

set role anon;
do $$
begin
  perform request_token('a@b.co', 'c', 'h', 'p');
  raise exception 'anon could call request_token';
exception when insufficient_privilege then
  raise notice 'ANON LOCKED OUT OF TOKEN REQUESTS';
end $$;
reset role;
