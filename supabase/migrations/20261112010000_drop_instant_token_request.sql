-- Migration 8: remove the first version of token requests, which stored a token as soon as
-- the class code matched. Migration 7's emailed link replaces it, and a function nothing
-- calls is one more thing to keep safe.

drop function public.request_token(text, text, text, text);
