import sys
from string import Template

import config as config_module
import state

_PAGE_TEMPLATE = Template("""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Trend Join Long — Trade Log</title>
<style>
  body { font-family: -apple-system, sans-serif; margin: 2rem; color: #1a1a1a; }
  table { border-collapse: collapse; width: 100%; }
  th, td { border: 1px solid #ccc; padding: 0.4rem 0.8rem; text-align: right; }
  th { background: #f0f0f0; }
  td.symbol { text-align: left; font-weight: bold; }
  tr.win { background: #eaffea; }
  tr.loss { background: #ffeaea; }
  .summary { margin-bottom: 1rem; }
</style>
</head>
<body>
<h1>Trend Join Long — Closed Trades</h1>
<div class="summary">
  <p>Trades: $count &nbsp; Win rate: $win_rate% &nbsp; Total R: $total_r &nbsp; Avg R: $avg_r</p>
</div>
<h2>Monthly vs target</h2>
<p style="color:#666;font-size:0.85rem">PnL from actual fills (paper fills are optimistic — no
extra slippage subtracted). Target is a yardstick, not a promise.</p>
<table style="margin-bottom:2rem">
<tr><th>Month</th><th>Trades</th><th>Win rate</th><th>Net PnL</th><th>% of month-start equity</th><th>Target $target_pct%</th></tr>
$month_rows
</table>
<table>
<tr>
  <th>Symbol</th><th>Entry</th><th>Exit</th><th>Qty</th>
  <th>Entry Time</th><th>Exit Time</th><th>R</th><th>Reason</th><th>PnL</th>
</tr>
$rows
</table>
</body>
</html>
""")

_ROW_TEMPLATE = Template(
    '<tr class="$css_class">'
    '<td class="symbol">$symbol</td><td>$entry_price</td><td>$exit_price</td><td>$qty</td>'
    '<td>$entry_time</td><td>$exit_time</td><td>$r_multiple</td><td>$exit_reason</td><td>$realized_pnl</td>'
    "</tr>"
)


def _monthly_rows(conn, trades, target_pct: float) -> str:
    months: dict[str, dict] = {}
    for t in trades:
        month = t["exit_time"][:7]  # YYYY-MM
        m = months.setdefault(month, {"trades": 0, "wins": 0, "pnl": 0.0})
        m["trades"] += 1
        if t["r_multiple"] > 0:
            m["wins"] += 1
        m["pnl"] += t["realized_pnl"]

    rows = []
    for month in sorted(months):
        m = months[month]
        start_equity = state.month_start_equity(conn, month)
        pct = (m["pnl"] / start_equity * 100) if start_equity else None
        pct_str = f"{pct:+.2f}%" if pct is not None else "n/a (no equity log)"
        if pct is None:
            css, verdict = "", "n/a"
        elif pct >= target_pct:
            css, verdict = "win", "HIT"
        elif pct < 0:
            css, verdict = "loss", "MISS (negative)"
        else:
            css, verdict = "", "below target"
        win_rate = m["wins"] / m["trades"] * 100
        rows.append(
            f'<tr class="{css}"><td class="symbol">{month}</td><td>{m["trades"]}</td>'
            f'<td>{win_rate:.1f}%</td><td>{m["pnl"]:+.2f}</td><td>{pct_str}</td><td>{verdict}</td></tr>'
        )
    return "\n".join(rows) if rows else "<tr><td colspan=6>No closed trades yet</td></tr>"


def generate_html(db_path: str = "bot.db", output_path: str = "dashboard.html") -> None:
    cfg = config_module.load_config()
    target_pct = cfg.get("risk_guard", {}).get("monthly_target_pct", 2.0)
    conn = state.init_db(db_path)
    trades = state.get_closed_trades(conn)

    rows = []
    total_r = 0.0
    wins = 0
    for t in trades:
        total_r += t["r_multiple"]
        css_class = "win" if t["r_multiple"] > 0 else "loss"
        if t["r_multiple"] > 0:
            wins += 1
        rows.append(
            _ROW_TEMPLATE.substitute(
                css_class=css_class,
                symbol=t["symbol"],
                entry_price=f"{t['entry_price']:.2f}",
                exit_price=f"{t['exit_price']:.2f}",
                qty=t["qty"],
                entry_time=t["entry_time"],
                exit_time=t["exit_time"],
                r_multiple=f"{t['r_multiple']:.2f}",
                exit_reason=t["exit_reason"],
                realized_pnl=f"{t['realized_pnl']:.2f}",
            )
        )

    count = len(trades)
    win_rate = f"{(wins / count * 100):.1f}" if count else "0.0"
    avg_r = f"{(total_r / count):.2f}" if count else "0.00"

    html = _PAGE_TEMPLATE.substitute(
        count=count,
        win_rate=win_rate,
        total_r=f"{total_r:.2f}",
        avg_r=avg_r,
        rows="\n".join(rows),
        target_pct=f"{target_pct:.1f}",
        month_rows=_monthly_rows(conn, trades, target_pct),
    )

    with open(output_path, "w") as f:
        f.write(html)


if __name__ == "__main__":
    cfg = config_module.load_config()
    generate_html(cfg["paths"]["db_path"], cfg["paths"]["dashboard_output_path"])
    print(f"Dashboard written to {cfg['paths']['dashboard_output_path']}")
    sys.exit(0)
