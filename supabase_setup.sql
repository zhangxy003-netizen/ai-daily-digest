-- ============================================================
-- Supabase 计数表 — 在 Supabase 控制台 SQL Editor 里执行一次
-- ============================================================
-- 设计:每条内容一行。显示值 = base(你手动设的起始值)+ real(真实累加)。
-- 你之后要"垫数字",直接在 Table Editor 里改 base_views / base_likes 即可。

create table if not exists stats (
  item_id     text primary key,   -- 对应 feed.json 里每条的 id
  base_views  integer not null default 0,   -- 起始浏览量(你后台手动设)
  base_likes  integer not null default 0,   -- 起始点赞数(你后台手动设)
  real_views  integer not null default 0,   -- 真实浏览累加(程序自增)
  real_likes  integer not null default 0,   -- 真实点赞累加(程序自增)
  updated_at  timestamptz default now()
);

-- 开启行级安全
alter table stats enable row level security;

-- 允许任何人读取计数(匿名访客要能看到数字)
create policy "anyone can read stats"
  on stats for select using (true);

-- 允许任何人插入新行(某条内容第一次被访问时自动建行)
create policy "anyone can insert stats"
  on stats for insert with check (true);

-- ============================================================
-- 原子自增函数:避免并发覆盖。前端通过 rpc 调用。
-- ============================================================

-- 浏览 +1(行不存在则建)
create or replace function bump_view(p_id text)
returns void language plpgsql security definer as $$
begin
  insert into stats(item_id, real_views) values (p_id, 1)
  on conflict (item_id) do update set real_views = stats.real_views + 1, updated_at = now();
end; $$;

-- 点赞 +1 / -1(delta 传 1 或 -1)
create or replace function bump_like(p_id text, delta integer)
returns void language plpgsql security definer as $$
begin
  insert into stats(item_id, real_likes) values (p_id, greatest(delta, 0))
  on conflict (item_id) do update
    set real_likes = greatest(stats.real_likes + delta, 0), updated_at = now();
end; $$;

-- 允许匿名角色执行这两个函数
grant execute on function bump_view(text) to anon;
grant execute on function bump_like(text, integer) to anon;

-- ============================================================
-- 可选:批量预置起始值的示例。把 item_id 换成你真实的 id。
-- 之后日常"垫数字"在 Table Editor 直接改 base_views/base_likes 即可,无需写 SQL。
-- ============================================================
-- insert into stats (item_id, base_views, base_likes) values
--   ('paper-2505.01234', 342, 28),
--   ('news-openai-2505', 891, 73)
-- on conflict (item_id) do update
--   set base_views = excluded.base_views, base_likes = excluded.base_likes;
