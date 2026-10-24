# -*- coding: utf-8 -*-
"""生成 GitHub Pages 用的复盘图站点。

用法：python scripts/build_pages.py public
输出：public/index.html + public/<品种>.html（plotly.js 用 CDN，单文件约几十 KB）
"""
import html
import os
import sys
from urllib.parse import quote

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config as cfg
import plot

INDEX_TPL = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>三重滤网 · 复盘图</title>
<style>
  body {{ font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
         margin: 0; padding: 16px; background: #f7f8fa; color: #222; }}
  h1 {{ font-size: 20px; margin: 8px 0 4px; }}
  .meta {{ color: #888; font-size: 13px; margin-bottom: 16px; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(110px, 1fr)); gap: 10px; }}
  a {{ display: block; padding: 14px 8px; text-align: center; background: #fff;
       border-radius: 10px; text-decoration: none; color: #1f6feb; font-size: 15px;
       box-shadow: 0 1px 3px rgba(0,0,0,.06); }}
  a:active {{ background: #eef4ff; }}
  .empty {{ color: #999; padding: 40px 0; text-align: center; }}
</style>
</head>
<body>
<h1>三重滤网 · 价格 / 趋势评分复盘图</h1>
<div class="meta">数据范围 {first} ~ {last} · 共 {rows} 条 · 更新于 {updated}</div>
{body}
</body>
</html>
"""


def build(data_dir, out_dir):
    df = plot.load(data_dir)
    os.makedirs(out_dir, exist_ok=True)

    names = sorted(df["品种"].dropna().unique().tolist()) if not df.empty else []
    links = []
    for name in names:
        try:
            fig = plot.build_figure(df, name)
        except SystemExit:
            continue
        fig.write_html(os.path.join(out_dir, f"{name}.html"),
                       include_plotlyjs="cdn", full_html=True)
        links.append(f'<a href="{quote(name)}.html">{html.escape(name)}</a>')
        print(f"[OK] {name}")

    if df.empty:
        body = '<div class="empty">暂无历史数据</div>'
        first = last = "-"
        rows = 0
    else:
        body = f'<div class="grid">{"".join(links)}</div>'
        first = df["时间"].min().strftime("%Y-%m-%d %H:%M")
        last = df["时间"].max().strftime("%Y-%m-%d %H:%M")
        rows = len(df)

    page = INDEX_TPL.format(
        first=first, last=last, rows=rows,
        updated=__import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M"),
        body=body,
    )
    with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(page)
    print(f"index.html: {len(names)} 个品种")


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "public"
    build(cfg.DATA_DIR, out)
