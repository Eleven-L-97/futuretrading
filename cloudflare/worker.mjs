/**
 * 三重滤网 · 外部定时器（Cloudflare Worker）
 *
 * 两种用法：
 * 1) Cloudflare 自带 Cron（每分钟）—— 调用 scheduled()
 * 2) 外部定时服务（如 cron-job.org）每分钟访问 ?auto=1 —— 走 fetch() 里的同一套判断
 *    这样即使 Cloudflare cron 不工作，只要外部服务能访问到 Worker 就能按时触发
 *
 * 手动测试：
 *   /                    -> 立即触发一次实时盯盘
 *   /?event=daily-snapshot 等
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
  return resp.status;
}

/** 按 UTC 时间判断该触发什么；返回触发了的事件列表。 */
async function autoTick(env) {
  const now = new Date();
  const dow = now.getUTCDay();                       // 0=周日
  const mins = now.getUTCHours() * 60 + now.getUTCMinutes();
  const m = now.getUTCMinutes();
  const weekday = dow >= 1 && dow <= 5;
  const fired = [];

  if (!weekday) return fired;

  // 北京 09:00-11:30 / 13:30-15:00 / 21:00-23:00 -> UTC 01:00-03:30 / 05:30-07:00 / 13:00-15:00
  const sessions = [[60, 210], [330, 420], [780, 900]];
  const inSession = sessions.some(([s, e]) => mins >= s && mins < e);

  if (inSession && m % 5 === 0) {
    await dispatch(env, "realtime-tick");
    fired.push("realtime");
  }
  if (mins === 10 * 60 + 10) {          // 18:10 北京
    await dispatch(env, "daily-snapshot");
    fired.push("snapshot");
  }
  if (mins === 7 * 60 + 10) {           // 15:10 北京
    await dispatch(env, "pages-build");
    fired.push("pages");
  }
  if (mins === 10 * 60 + 20 && now.getUTCDate() >= 28) {   // 月末 18:20
    await dispatch(env, "monthly-perf");
    fired.push("monthly");
  }
  return fired;
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.searchParams.get("auto")) {
      const fired = await autoTick(env);
      return new Response(`auto: ${fired.join(",") || "nothing"}\n`, {
        headers: { "Content-Type": "text/plain; charset=utf-8" },
      });
    }
    const ev = url.searchParams.get("event") || "realtime-tick";
    const status = await dispatch(env, ev);
    return new Response(`已触发 ${ev}（GitHub 返回 ${status}）\n`, {
      headers: { "Content-Type": "text/plain; charset=utf-8" },
    });
  },

  async scheduled(event, env, ctx) {
    console.log(`cron tick ${new Date().toISOString()}`);
    ctx.waitUntil(autoTick(env));
  },
};
