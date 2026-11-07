-- Supabase scheduler for the exchange fetcher (run once in the Supabase SQL editor, as the owner).
-- Calls the engine's tick endpoint every 15 minutes; the engine decides whether a fetch is due
-- (auto fetch on, chosen frequency, trading-day window). Monitoring/alerting stays with the
-- GitHub health check; this job does not keep the project from being paused.
--
-- 1. Store the engine's OPS_TOKEN in Supabase Vault (copy it from Render -> Environment).
--    Paste it ONLY here, in your Supabase SQL editor. Never in chat or in the repository.
--      select vault.create_secret('<paste OPS_TOKEN here>', 'margin_ops_token');
--
-- 2. Then run the rest of this file.
create extension if not exists pg_cron;
create extension if not exists pg_net;

select cron.unschedule('margin-fetch-tick') where exists (select 1 from cron.job where jobname = 'margin-fetch-tick');

select cron.schedule(
  'margin-fetch-tick',
  '*/15 * * * *',
  $$
  select net.http_post(
    url     := 'https://margin-engine-staging.onrender.com/api/ops/fetch-tick',
    headers := jsonb_build_object(
                 'Content-Type', 'application/json',
                 'X-Ops-Token', (select decrypted_secret from vault.decrypted_secrets where name = 'margin_ops_token')),
    body    := '{}'::jsonb,
    timeout_milliseconds := 90000
  );
  $$
);

-- Check it:   select jobname, schedule, active from cron.job;
-- Last calls: select status_code, left(content::text, 200), created from net._http_response order by created desc limit 5;
