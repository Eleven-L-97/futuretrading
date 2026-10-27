/**
 * 三重滤网 · 外部定时器（Cloudflare Worker）
 *
 * 作用：替代 GitHub Actions 的 schedule，按北京时间（UTC+8）在固定时点
 * 调用 GitHub 的 repository_dispatch 接口，GitHub 只负责跑工作流。
 *
 * 事件类型（与 .github/workflows 里的 types 对应）：
 *   realtime-tick   日内盯盘（交易时段每 5 分钟）
 *   daily-snapshot  收盘快照（每交易日 18:10 北京时间）
 *   pages-build     复盘图发布（每交易日 15:10 北京时间）
 *   monthly-perf    绩效指标（每月 28-31 日 18:20 北京时间）
 *
 * 部署：
 *   npm i -g wrangler
 *   wrangler login
 *   wrangler secret put GH_TOKEN     # 粘贴 fine-grained token（Contents: Read and write）
 *   wrangler deploy
 */

const OWNER = "Eleven-L-97";
const REPO = "futuretrading";

async function dispatch(env, eventType) {
  const resp = await fetch(`https://api.github.com/repos/${OWNER}/${REPO}/dispatches`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.GH_TOKEN}`,
      Accept: "application/vnd.github+json",
      "User-Agent": "futures-cron",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ event_type: eventType }),
  });
  console.log(`${eventType} -> ${resp.status}`);
}

export default {
  // 浏览器打开 Worker 地址即可手动测试：
  //   https://<worker>.<子域>.workers.dev/                  -> 触发实时盯盘
  //   https://<worker>.<子域>.workers.dev/?event=daily-snapshot
  async fetch(request, env) {
    const url = new URL(request.url);
    const ev = url.searchParams.get("event") || "realtime-tick";
    await dispatch(env, ev);
    return new Response(`已触发 ${ev}，请到 GitHub Actions 查看运行。\n`, {
      headers: { "Content-Type": "text/plain; charset=utf-8" },
    });
  },

  async scheduled(event, env, ctx) {
    const now = new Date();
    const dow = now.getUTCDay();              // 0=周日
    const h = now.getUTCHours();
    const m = now.getUTCMinutes();
    const mins = h * 60 + m;
    const weekday = dow >= 1 && dow <= 5;

    // 北京时间 09:00-11:30 / 13:30-15:00 / 21:00-23:00  ->  UTC 01:00-03:30 / 05:30-07:00 / 13:00-15:00
    const sessions = [[60, 210], [330, 420], [780, 900]];
    const inSession = sessions.some(([s, e]) => mins >= s && mins < e);

    if (weekday && inSession && m % 5 === 0) {
      ctx.waitUntil(dispatch(env, "realtime-tick"));
    }
    // 18:10 北京时间 = 10:10 UTC
    if (weekday && mins === 10 * 60 + 10) {
      ctx.waitUntil(dispatch(env, "daily-snapshot"));
    }
    // 15:10 北京时间 = 07:10 UTC
    if (weekday && mins === 7 * 60 + 10) {
      ctx.waitUntil(dispatch(env, "pages-build"));
    }
    // 18:20 北京时间 = 10:20 UTC，每月 28-31 日（脚本内部再判断是否月末）
    if (weekday && mins === 10 * 60 + 20 && now.getUTCDate() >= 28) {
      ctx.waitUntil(dispatch(env, "monthly-perf"));
    }
  },
};
