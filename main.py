import asyncio
import json
import os
import smtplib
import sys
from datetime import datetime, time, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

TZ = ZoneInfo("Asia/Shanghai")

# ============ 配置区：目前保持默认，等拿到日志后再填 ============
CITY_ID = "" # 待校准
MAIN_STATION_NAME = "绿云石都"
BACKUP_STATION_NAME = "恒胜"

MAIN_LINES = [
    {"name": "891", "direction": "外河坪枢纽站", "line_id": "", "station_id": "", "target_order": 0},
    {"name": "204", "direction": "重庆育才实验学校", "line_id": "", "station_id": "", "target_order": 0},
    {"name": "428", "direction": "杨家坪西郊①", "line_id": "", "station_id": "", "target_order": 0},
]
BACKUP_LINE = {"name": "426", "direction": "双峰山路", "line_id": "", "station_id": "", "target_order": 0}

WINDOW_START = time(8, 20)
WINDOW_END = time(8, 28)
LOOKAHEAD_MIN = 30
# ================================================================

SERVER_PARAMS = StdioServerParameters(
    command="npx",
    args=["-y", "chelaile-mcp"],
)

async def call_tool(session, name, args):
    try:
        result = await session.call_tool(name, args)
        texts = []
        for c in result.content:
            if hasattr(c, "text"):
                texts.append(c.text)
        raw = "\n".join(texts)
        try:
            return json.loads(raw), raw
        except json.JSONDecodeError:
            return None, raw
    except Exception as e:
        print(f"调用工具 {name} 失败: {e}", file=sys.stderr)
        return None, str(e)

async def discover(session):
    print("=== 可用工具列表 ===", file=sys.stderr)
    try:
        tools = await session.list_tools()
        for t in tools.tools:
            print(f"- {t.name}: {t.description}", file=sys.stderr)
    except Exception as e:
        print(f"获取工具列表失败: {e}", file=sys.stderr)

    print("\n=== 搜索主站：绿云石都 ===", file=sys.stderr)
    _, raw = await call_tool(session, "bus_search", {"keyword": MAIN_STATION_NAME})
    print(raw, file=sys.stderr)

    print("\n=== 搜索备用站：恒胜 ===", file=sys.stderr)
    _, raw = await call_tool(session, "bus_search", {"keyword": BACKUP_STATION_NAME})
    print(raw, file=sys.stderr)

    for line in MAIN_LINES + [BACKUP_LINE]:
        print(f"\n=== 搜索线路 {line['name']} ===", file=sys.stderr)
        _, raw = await call_tool(session, "bus_search", {"keyword": line["name"]})
        print(raw, file=sys.stderr)

def parse_etas(payload):
    out = []
    if not payload:
        return out
    lines = payload.get("lines") or payload.get("data", {}).get("lines") or []
    if isinstance(payload, list):
        lines = payload
    for ln in lines:
        for bus in ln.get("buses", []) or ln.get("busList", []) or []:
            eta = bus.get("eta") or {}
            travel = eta.get("travelTime") or eta.get("travel_time")
            if travel is None:
                continue
            arrival = datetime.now(TZ) + timedelta(seconds=int(travel))
            out.append(arrival)
    return sorted(out)

async def query_line_etas(session, cfg):
    args = {
        "lineId": cfg["line_id"],
        "stationId": cfg["station_id"],
        "targetOrder": cfg["target_order"],
    }
    payload, raw = await call_tool(session, "bus_refresh_lines", args)
    return parse_etas(payload)

def build_html(main_results, backup_results, now):
    def fmt(dt):
        return dt.strftime("%H:%M")

    rows = []
    hit_window = False
    for name, direction, etas in main_results:
        limited = [e for e in etas if e <= now + timedelta(minutes=LOOKAHEAD_MIN)]
        if limited:
            first = limited[0]
            remain = int((first - now).total_seconds() // 60)
            in_window = WINDOW_START <= first.time() <= WINDOW_END
            if in_window:
                hit_window = True
            cls = "hit" if in_window else ""
            rows.append(
                f'<tr><td>{name} 路</td><td>{direction}</td>'
                f'<td class="{cls}">{fmt(first)}</td><td>{remain} 分钟</td></tr>'
            )
        else:
            rows.append(
                f'<tr><td>{name} 路</td><td>{direction}</td>'
                f'<td class="miss">30 分钟内暂无</td><td>—</td></tr>'
            )

    main_table = f"""
    <table>
      <thead><tr><th>线路</th><th>方向</th><th>预计到站</th><th>剩余</th></tr></thead>
      <tbody>{''.join(rows)}</tbody>
    </table>
    """

    backup_block = ""
    if not hit_window and backup_results is not None:
        name, direction, etas = backup_results
        limited = [e for e in etas if e <= now + timedelta(minutes=LOOKAHEAD_MIN)]
        brows = ""
        if limited:
            for e in limited[:5]:
                remain = int((e - now).total_seconds() // 60)
                brows += f"<tr><td>{name} 路</td><td>{direction}</td><td>{fmt(e)}</td><td>{remain} 分钟</td></tr>"
        else:
            brows = f'<tr><td>{name} 路</td><td>{direction}</td><td class="miss">30 分钟内暂无</td><td>—</td></tr>'
        backup_block = f"""
        <div class="backup">
          <p class="note">⚠️ 三辆主线车在 8:20–8:28 窗口内均无到站，以下为备用方案：</p>
          <table>
            <thead><tr><th>线路</th><th>方向</th><th>预计到站（恒胜站）</th><th>剩余</th></tr></thead>
            <tbody>{brows}</tbody>
          </table>
        </div>
        """

    return f"""
    <html><head><meta charset="utf-8">
    <style>
      body {{ font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif; color: #333; }}
      table {{ border-collapse: collapse; width: 100%; max-width: 620px; margin-top: 8px; }}
      th {{ background: #f0f0f0; text-align: left; padding: 8px; border-bottom: 2px solid #ddd; font-size: 14px; }}
      td {{ padding: 8px; border-bottom: 1px solid #eee; font-size: 14px; }}
      tr:nth-child(even) td {{ background: #fafafa; }}
      .hit {{ color: #1a7f37; font-weight: bold; }}
      .miss {{ color: #999; }}
      .backup {{ margin-top: 20px; }}
      .note {{ color: #b45309; font-size: 13px; }}
    </style></head><body>
      <p>查询时间：{now.strftime('%Y-%m-%d %H:%M:%S')}（北京时间）</p>
      {main_table}
      {backup_block}
    </body></html>
    """

def send_mail(html):
    sender = os.environ["QQ_EMAIL"]
    auth = os.environ["QQ_AUTH_CODE"]
    to = os.environ["TO_EMAIL"]
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"公交到站推送 {datetime.now(TZ).strftime('%H:%M')}"
    msg["From"] = sender
    msg["To"] = to
    msg.attach(MIMEText(html, "html", "utf-8"))
    with smtplib.SMTP_SSL("smtp.qq.com", 465) as s:
        s.login(sender, auth)
        s.sendmail(sender, [to], msg.as_string())

async def main():
    now = datetime.now(TZ)
    print(f"开始运行，模式: {'DISCOVER' if os.getenv('DISCOVER') == '1' else 'NORMAL'}", file=sys.stderr)
    
    async with stdio_client(SERVER_PARAMS) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            if os.getenv("DISCOVER") == "1":
                await discover(session)
                return

            # 正常模式，如果没填 ID 会报错，等后面拿到 ID 再运行
            main_results = []
            for cfg in MAIN_LINES:
                etas = await query_line_etas(session, cfg)
                main_results.append((cfg["name"], cfg["direction"], etas))

            hit_window = False
            for _, _, etas in main_results:
                for e in etas:
                    if e <= now + timedelta(minutes=LOOKAHEAD_MIN) and WINDOW_START <= e.time() <= WINDOW_END:
                        hit_window = True
                        break

            backup_results = None
            if not hit_window:
                etas = await query_line_etas(session, BACKUP_LINE)
                backup_results = (BACKUP_LINE["name"], BACKUP_LINE["direction"], etas)

            html = build_html(main_results, backup_results, now)
            send_mail(html)
            print(f"[OK] {now.strftime('%H:%M')} 推送完成，窗口命中={hit_window}", file=sys.stderr)

if __name__ == "__main__":
    asyncio.run(main())
