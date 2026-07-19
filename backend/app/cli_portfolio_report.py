"""组合回测交互 HTML 报告：调仓点、买卖明细、区间收益。"""

from __future__ import annotations

import json
from collections import defaultdict, deque
from pathlib import Path
from typing import Any


def _norm_date(d: Any) -> str:
    s = str(d or "").strip()
    return s[:10] if len(s) >= 10 else s


def _nf(x: Any) -> float | None:
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if v != v:  # NaN
        return None
    return v


def _symbol_name_map(rebalances: list[dict[str, Any]]) -> dict[str, str]:
    names: dict[str, str] = {}
    for rb in rebalances:
        for item in rb.get("selection") or []:
            if not isinstance(item, dict):
                continue
            sym = str(item.get("symbol") or "").strip()
            name = str(item.get("name") or "").strip()
            if sym and name:
                names[sym] = name
    return names


def _equity_map(equity: list[dict[str, Any]]) -> dict[str, float]:
    out: dict[str, float] = {}
    for row in equity:
        d = _norm_date(row.get("date"))
        v = _nf(row.get("equity"))
        if d and v is not None:
            out[d] = v
    return out


def _trade_row(
    trade: dict[str, Any],
    *,
    names: dict[str, str],
) -> dict[str, Any]:
    side = str(trade.get("side") or "").lower()
    price = _nf(trade.get("price")) or 0.0
    shares = _nf(trade.get("shares")) or 0.0
    cost = _nf(trade.get("cost")) or 0.0
    notional = abs(price * shares)
    symbol = str(trade.get("symbol") or "").strip()
    return {
        "date": _norm_date(trade.get("date")),
        "symbol": symbol,
        "name": names.get(symbol, ""),
        "side": side,
        "price": price,
        "shares": shares,
        "notional": notional,
        "cost": cost,
        "cash_after": _nf(trade.get("cash_after")),
    }


def _fifo_round_trips(
    trades: list[dict[str, Any]],
    *,
    names: dict[str, str],
) -> list[dict[str, Any]]:
    """按代码 FIFO 匹配 buy→sell，估算单票回合盈亏。"""
    lots: dict[str, deque[dict[str, float]]] = defaultdict(deque)
    trips: list[dict[str, Any]] = []
    for trade in trades:
        side = str(trade.get("side") or "").lower()
        symbol = str(trade.get("symbol") or "").strip()
        if not symbol:
            continue
        price = _nf(trade.get("price")) or 0.0
        shares = _nf(trade.get("shares")) or 0.0
        if shares <= 0 or price <= 0:
            continue
        day = _norm_date(trade.get("date"))
        if side == "buy":
            lots[symbol].append({"shares": shares, "price": price, "date": day})
            continue
        if side != "sell":
            continue
        remain = shares
        buy_cost = 0.0
        buy_shares = 0.0
        buy_date = day
        while remain > 1e-9 and lots[symbol]:
            lot = lots[symbol][0]
            take = min(remain, lot["shares"])
            buy_cost += take * lot["price"]
            buy_shares += take
            buy_date = str(lot["date"])
            lot["shares"] -= take
            remain -= take
            if lot["shares"] <= 1e-9:
                lots[symbol].popleft()
        if buy_shares <= 0:
            continue
        sell_proceeds = buy_shares * price
        pnl = sell_proceeds - buy_cost
        pnl_pct = pnl / buy_cost if buy_cost > 0 else 0.0
        trips.append(
            {
                "symbol": symbol,
                "name": names.get(symbol, ""),
                "buy_date": buy_date,
                "sell_date": day,
                "shares": buy_shares,
                "buy_price": buy_cost / buy_shares,
                "sell_price": price,
                "pnl": pnl,
                "pnl_pct": pnl_pct,
            }
        )
    return trips


def build_portfolio_report_model(out: dict[str, Any]) -> dict[str, Any]:
    """从组合回测 JSON 派生交互报告数据模型。"""
    equity = list(out.get("equity") or [])
    trades_raw = list(out.get("trades") or [])
    rebalances = list(out.get("rebalances") or [])
    metrics = dict(out.get("metrics") or {})
    names = _symbol_name_map(rebalances)
    eq_map = _equity_map(equity)

    trades = [_trade_row(t, names=names) for t in trades_raw if isinstance(t, dict)]
    trades_by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for t in trades:
        if t["date"]:
            trades_by_date[t["date"]].append(t)

    rb_dates = [_norm_date(rb.get("date")) for rb in rebalances if _norm_date(rb.get("date"))]
    equity_dates = [_norm_date(r.get("date")) for r in equity if _norm_date(r.get("date"))]
    end_date = equity_dates[-1] if equity_dates else (rb_dates[-1] if rb_dates else "")

    periods: list[dict[str, Any]] = []
    for i, rb in enumerate(rebalances):
        day = _norm_date(rb.get("date"))
        if not day:
            continue
        next_day = rb_dates[i + 1] if i + 1 < len(rb_dates) else end_date
        eq_start = eq_map.get(day)
        eq_end = eq_map.get(next_day) if next_day else None
        period_pnl = None
        period_ret = None
        if eq_start is not None and eq_end is not None and eq_start > 0:
            period_pnl = eq_end - eq_start
            period_ret = period_pnl / eq_start

        day_trades = trades_by_date.get(day, [])
        buys = [t for t in day_trades if t["side"] == "buy"]
        sells = [t for t in day_trades if t["side"] == "sell"]
        targets = [str(s) for s in (rb.get("targets") or [])]
        selection = []
        for item in rb.get("selection") or []:
            if not isinstance(item, dict):
                continue
            sym = str(item.get("symbol") or "").strip()
            selection.append(
                {
                    "symbol": sym,
                    "name": str(item.get("name") or names.get(sym, "")),
                    "close": _nf(item.get("close")),
                    "float_market_cap": _nf(item.get("float_market_cap")),
                    "rank_market_cap": _nf(item.get("rank_market_cap")),
                }
            )

        periods.append(
            {
                "index": i,
                "date": day,
                "next_date": next_day,
                "equity_start": eq_start,
                "equity_end": eq_end,
                "period_pnl": period_pnl,
                "period_return": period_ret,
                "cash": _nf(rb.get("cash")),
                "targets": targets,
                "selection": selection,
                "buys": buys,
                "sells": sells,
                "buy_count": len(buys),
                "sell_count": len(sells),
            }
        )

    equity_series = [
        {"date": _norm_date(r.get("date")), "equity": _nf(r.get("equity")) or 0.0}
        for r in equity
        if _norm_date(r.get("date"))
    ]
    initial_cash = _nf(metrics.get("initial_cash"))
    if initial_cash is None and equity_series:
        initial_cash = equity_series[0]["equity"] or 1.0
    if not initial_cash or initial_cash <= 0:
        initial_cash = 1.0

    for point in equity_series:
        point["nav"] = point["equity"] / initial_cash

    round_trips = _fifo_round_trips(trades_raw, names=names)

    return {
        "strategy_id": out.get("strategy_id") or "",
        "mode": out.get("mode") or "",
        "asof": out.get("asof"),
        "universe_note": out.get("universe_note") or "",
        "warnings": list(out.get("warnings") or []),
        "disclaimer": out.get("disclaimer") or "",
        "metrics": metrics,
        "initial_cash": initial_cash,
        "equity": equity_series,
        "periods": periods,
        "trades": trades,
        "round_trips": round_trips,
        "rebalance_dates": rb_dates,
    }


_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>组合回测报告 __TITLE__</title>
<style>
:root {
  --bg: #0f1115;
  --panel: #1a1d24;
  --border: #2a2f3a;
  --text: #e8eaed;
  --muted: #9aa0a6;
  --accent: #4fc3f7;
  --buy: #66bb6a;
  --sell: #ef5350;
  --warn: #ffb74d;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  font-family: "Microsoft YaHei", "Segoe UI", sans-serif;
  background: var(--bg);
  color: var(--text);
  line-height: 1.45;
}
header {
  padding: 20px 24px 8px;
  border-bottom: 1px solid var(--border);
}
header h1 {
  margin: 0 0 6px;
  font-size: 20px;
  font-weight: 600;
}
header .meta { color: var(--muted); font-size: 13px; }
.wrap { padding: 16px 24px 40px; max-width: 1400px; margin: 0 auto; }
.stats {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: 10px;
  margin: 16px 0;
}
.stat {
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px 14px;
}
.stat .label { color: var(--muted); font-size: 12px; }
.stat .value { font-size: 18px; margin-top: 4px; font-variant-numeric: tabular-nums; }
.stat .value.pos { color: var(--buy); }
.stat .value.neg { color: var(--sell); }
.panel {
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px 14px;
  margin-bottom: 14px;
}
.panel h2 {
  margin: 0 0 10px;
  font-size: 15px;
  font-weight: 600;
}
#chart {
  width: 100%;
  height: 280px;
  display: block;
  cursor: crosshair;
}
.grid2 {
  display: grid;
  grid-template-columns: 280px 1fr;
  gap: 12px;
}
@media (max-width: 900px) {
  .grid2 { grid-template-columns: 1fr; }
}
.period-list {
  max-height: 420px;
  overflow: auto;
  border: 1px solid var(--border);
  border-radius: 6px;
}
.period-item {
  padding: 10px 12px;
  border-bottom: 1px solid var(--border);
  cursor: pointer;
  font-size: 13px;
}
.period-item:hover { background: #22262f; }
.period-item.active { background: #243040; border-left: 3px solid var(--accent); }
.period-item .d { font-variant-numeric: tabular-nums; }
.period-item .r { float: right; font-variant-numeric: tabular-nums; }
.detail { font-size: 13px; }
.detail .row { margin-bottom: 8px; color: var(--muted); }
.detail .row b { color: var(--text); font-weight: 600; }
.tables { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
@media (max-width: 900px) {
  .tables { grid-template-columns: 1fr; }
}
table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}
th, td {
  border-bottom: 1px solid var(--border);
  padding: 6px 8px;
  text-align: left;
  white-space: nowrap;
}
th { color: var(--muted); font-weight: 500; position: sticky; top: 0; background: var(--panel); }
.scroll { max-height: 260px; overflow: auto; border: 1px solid var(--border); border-radius: 6px; }
.side-buy { color: var(--buy); }
.side-sell { color: var(--sell); }
.pos { color: var(--buy); }
.neg { color: var(--sell); }
.filters {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 10px;
  align-items: center;
}
.filters input {
  background: var(--bg);
  border: 1px solid var(--border);
  color: var(--text);
  border-radius: 6px;
  padding: 6px 10px;
  font-size: 13px;
  min-width: 160px;
}
.note { color: var(--muted); font-size: 12px; margin-top: 8px; }
.empty { color: var(--muted); padding: 12px; }
</style>
</head>
<body>
<header>
  <h1>组合回测报告 · <span id="strategyId"></span></h1>
  <div class="meta" id="headerMeta"></div>
</header>
<div class="wrap">
  <div class="stats" id="stats"></div>
  <div class="panel">
    <h2>净值曲线（点击调仓竖线或下方列表查看详情）</h2>
    <svg id="chart" viewBox="0 0 1000 280" preserveAspectRatio="none"></svg>
    <div class="note" id="chartHint">悬停查看净值；点击蓝色调仓标记切换详情。</div>
  </div>
  <div class="grid2">
    <div class="panel">
      <h2>调仓日</h2>
      <div class="period-list" id="periodList"></div>
    </div>
    <div class="panel detail" id="detail">
      <h2>调仓详情</h2>
      <div class="empty">请选择左侧调仓日</div>
    </div>
  </div>
  <div class="panel">
    <h2>成交明细</h2>
    <div class="filters">
      <input id="filterDate" placeholder="日期含… 如 2020-02" />
      <input id="filterSymbol" placeholder="代码/名称含…" />
    </div>
    <div class="scroll">
      <table>
        <thead>
          <tr>
            <th>日期</th><th>方向</th><th>代码</th><th>名称</th>
            <th>价格</th><th>股数</th><th>成交额</th><th>费用</th>
          </tr>
        </thead>
        <tbody id="tradeBody"></tbody>
      </table>
    </div>
  </div>
  <div class="panel">
    <h2>单票回合盈亏（FIFO 估算）</h2>
    <div class="scroll">
      <table>
        <thead>
          <tr>
            <th>代码</th><th>名称</th><th>买入日</th><th>卖出日</th>
            <th>股数</th><th>买价</th><th>卖价</th><th>盈亏</th><th>收益率</th>
          </tr>
        </thead>
        <tbody id="tripBody"></tbody>
      </table>
    </div>
    <div class="note">费用/滑点已体现在成交价与费用字段中；回合盈亏为买卖价差近似，便于定位贡献来源。</div>
  </div>
  <div class="note" id="footerNote"></div>
</div>
<script>
const DATA = __DATA__;

function pct(v) {
  if (v === null || v === undefined || Number.isNaN(v)) return "-";
  return (v * 100).toFixed(2) + "%";
}
function num(v, d=2) {
  if (v === null || v === undefined || Number.isNaN(v)) return "-";
  return Number(v).toLocaleString("zh-CN", { maximumFractionDigits: d, minimumFractionDigits: 0 });
}
function clsRet(v) {
  if (v === null || v === undefined || Number.isNaN(v)) return "";
  return v >= 0 ? "pos" : "neg";
}
function sideLabel(s) { return s === "buy" ? "买入" : (s === "sell" ? "卖出" : s); }

function renderStats() {
  const m = DATA.metrics || {};
  const items = [
    ["总收益", pct(m.total_return), clsRet(m.total_return)],
    ["年化", pct(m.annualized_return), clsRet(m.annualized_return)],
    ["最大回撤", pct(m.max_drawdown), "neg"],
    ["Sharpe", num(m.sharpe, 2), ""],
    ["最终权益", num(m.final_equity, 0), ""],
    ["调仓次数", String((DATA.periods || []).length), ""],
    ["成交笔数", num(m.num_trades, 0), ""],
    ["胜率(回合)", pct(m.win_rate), ""],
  ];
  document.getElementById("stats").innerHTML = items.map(([label, value, c]) =>
    `<div class="stat"><div class="label">${label}</div><div class="value ${c}">${value}</div></div>`
  ).join("");
}

function renderChart() {
  const svg = document.getElementById("chart");
  const eq = DATA.equity || [];
  const rbSet = new Set(DATA.rebalance_dates || []);
  if (!eq.length) {
    svg.innerHTML = "";
    return;
  }
  const W = 1000, H = 280;
  const pad = { l: 48, r: 16, t: 16, b: 28 };
  const xs = eq.map((_, i) => i);
  const ys = eq.map(p => p.nav);
  const minY = Math.min(...ys);
  const maxY = Math.max(...ys);
  const spanY = (maxY - minY) || 1;
  const xOf = i => pad.l + (i / Math.max(eq.length - 1, 1)) * (W - pad.l - pad.r);
  const yOf = v => pad.t + (1 - (v - minY) / spanY) * (H - pad.t - pad.b);
  const path = eq.map((p, i) => `${i === 0 ? "M" : "L"}${xOf(i).toFixed(2)},${yOf(p.nav).toFixed(2)}`).join(" ");
  let marks = "";
  eq.forEach((p, i) => {
    if (!rbSet.has(p.date)) return;
    const x = xOf(i);
    marks += `<line class="rb-line" data-date="${p.date}" x1="${x}" y1="${pad.t}" x2="${x}" y2="${H - pad.b}"
      stroke="#4fc3f7" stroke-opacity="0.35" stroke-width="1.5" style="cursor:pointer" />`;
    marks += `<circle class="rb-dot" data-date="${p.date}" cx="${x}" cy="${yOf(p.nav)}" r="4"
      fill="#4fc3f7" style="cursor:pointer" />`;
  });
  const y0 = yOf(1);
  svg.innerHTML = `
    <rect x="0" y="0" width="${W}" height="${H}" fill="#0f1115"/>
    <line x1="${pad.l}" y1="${y0}" x2="${W - pad.r}" y2="${y0}" stroke="#9aa0a6" stroke-dasharray="4 4" stroke-opacity="0.5"/>
    <path d="${path}" fill="none" stroke="#4fc3f7" stroke-width="2"/>
    ${marks}
    <text x="${pad.l}" y="14" fill="#9aa0a6" font-size="11">净值</text>
    <text x="${pad.l}" y="${H - 8}" fill="#9aa0a6" font-size="11">${eq[0].date}</text>
    <text x="${W - pad.r}" y="${H - 8}" fill="#9aa0a6" font-size="11" text-anchor="end">${eq[eq.length - 1].date}</text>
  `;
  svg.querySelectorAll(".rb-line, .rb-dot").forEach(el => {
    el.addEventListener("click", () => selectPeriodByDate(el.getAttribute("data-date")));
  });
  svg.addEventListener("mousemove", (ev) => {
    const rect = svg.getBoundingClientRect();
    const rel = (ev.clientX - rect.left) / rect.width;
    const idx = Math.round(rel * (eq.length - 1));
    const p = eq[Math.max(0, Math.min(eq.length - 1, idx))];
    document.getElementById("chartHint").textContent =
      `${p.date} · 净值 ${p.nav.toFixed(4)} · 权益 ${num(p.equity, 0)}` +
      (rbSet.has(p.date) ? " · 调仓日" : "");
  });
}

let selectedIndex = 0;

function selectPeriodByDate(date) {
  const idx = (DATA.periods || []).findIndex(p => p.date === date);
  if (idx >= 0) selectPeriod(idx);
}

function selectPeriod(index) {
  selectedIndex = index;
  const periods = DATA.periods || [];
  document.querySelectorAll(".period-item").forEach((el, i) => {
    el.classList.toggle("active", i === index);
  });
  const p = periods[index];
  const box = document.getElementById("detail");
  if (!p) {
    box.innerHTML = `<h2>调仓详情</h2><div class="empty">无调仓数据</div>`;
    return;
  }
  const sellRows = (p.sells || []).map(t =>
    `<tr><td class="side-sell">卖出</td><td>${t.symbol}</td><td>${t.name || ""}</td>
     <td>${num(t.price, 3)}</td><td>${num(t.shares, 0)}</td><td>${num(t.notional, 0)}</td></tr>`
  ).join("");
  const buyRows = (p.buys || []).map(t =>
    `<tr><td class="side-buy">买入</td><td>${t.symbol}</td><td>${t.name || ""}</td>
     <td>${num(t.price, 3)}</td><td>${num(t.shares, 0)}</td><td>${num(t.notional, 0)}</td></tr>`
  ).join("");
  const selRows = (p.selection || []).map(s =>
    `<tr><td>${s.symbol}</td><td>${s.name || ""}</td><td>${num(s.close, 2)}</td>
     <td>${num(s.float_market_cap || s.rank_market_cap, 0)}</td></tr>`
  ).join("");
  box.innerHTML = `
    <h2>调仓详情 · ${p.date}</h2>
    <div class="row">持有区间至 <b>${p.next_date || "-"}</b>
      · 期初权益 <b>${num(p.equity_start, 0)}</b>
      · 期末权益 <b>${num(p.equity_end, 0)}</b>
      · 区间盈亏 <b class="${clsRet(p.period_pnl)}">${num(p.period_pnl, 0)}</b>
      · 区间收益 <b class="${clsRet(p.period_return)}">${pct(p.period_return)}</b>
    </div>
    <div class="row">卖出 ${p.sell_count} / 买入 ${p.buy_count} · 调仓后现金 ${num(p.cash, 0)}</div>
    <div class="tables">
      <div>
        <h2>当日卖出</h2>
        <div class="scroll"><table>
          <thead><tr><th>方向</th><th>代码</th><th>名称</th><th>价格</th><th>股数</th><th>成交额</th></tr></thead>
          <tbody>${sellRows || '<tr><td colspan="6" class="empty">无卖出</td></tr>'}</tbody>
        </table></div>
      </div>
      <div>
        <h2>当日买入</h2>
        <div class="scroll"><table>
          <thead><tr><th>方向</th><th>代码</th><th>名称</th><th>价格</th><th>股数</th><th>成交额</th></tr></thead>
          <tbody>${buyRows || '<tr><td colspan="6" class="empty">无买入</td></tr>'}</tbody>
        </table></div>
      </div>
    </div>
    <h2 style="margin-top:12px">目标持仓（选股）</h2>
    <div class="scroll"><table>
      <thead><tr><th>代码</th><th>名称</th><th>收盘价</th><th>流通市值</th></tr></thead>
      <tbody>${selRows || '<tr><td colspan="4" class="empty">无</td></tr>'}</tbody>
    </table></div>
  `;
  const active = document.querySelector(`.period-item[data-index="${index}"]`);
  if (active) active.scrollIntoView({ block: "nearest" });
}

function renderPeriodList() {
  const list = document.getElementById("periodList");
  const periods = DATA.periods || [];
  list.innerHTML = periods.map((p, i) => `
    <div class="period-item" data-index="${i}">
      <span class="d">${p.date}</span>
      <span class="r ${clsRet(p.period_return)}">${pct(p.period_return)}</span>
      <div style="color:var(--muted);margin-top:2px">卖${p.sell_count} / 买${p.buy_count}</div>
    </div>
  `).join("") || `<div class="empty">无调仓</div>`;
  list.querySelectorAll(".period-item").forEach(el => {
    el.addEventListener("click", () => selectPeriod(Number(el.getAttribute("data-index"))));
  });
}

function renderTrades() {
  const dateQ = (document.getElementById("filterDate").value || "").trim();
  const symQ = (document.getElementById("filterSymbol").value || "").trim().toLowerCase();
  const rows = (DATA.trades || []).filter(t => {
    if (dateQ && !(t.date || "").includes(dateQ)) return false;
    if (symQ) {
      const blob = `${t.symbol} ${t.name || ""}`.toLowerCase();
      if (!blob.includes(symQ)) return false;
    }
    return true;
  });
  document.getElementById("tradeBody").innerHTML = rows.map(t => `
    <tr>
      <td>${t.date}</td>
      <td class="${t.side === "buy" ? "side-buy" : "side-sell"}">${sideLabel(t.side)}</td>
      <td>${t.symbol}</td><td>${t.name || ""}</td>
      <td>${num(t.price, 3)}</td><td>${num(t.shares, 0)}</td>
      <td>${num(t.notional, 0)}</td><td>${num(t.cost, 2)}</td>
    </tr>
  `).join("") || `<tr><td colspan="8" class="empty">无匹配成交</td></tr>`;
}

function renderTrips() {
  document.getElementById("tripBody").innerHTML = (DATA.round_trips || []).map(t => `
    <tr>
      <td>${t.symbol}</td><td>${t.name || ""}</td>
      <td>${t.buy_date}</td><td>${t.sell_date}</td>
      <td>${num(t.shares, 0)}</td>
      <td>${num(t.buy_price, 3)}</td><td>${num(t.sell_price, 3)}</td>
      <td class="${clsRet(t.pnl)}">${num(t.pnl, 0)}</td>
      <td class="${clsRet(t.pnl_pct)}">${pct(t.pnl_pct)}</td>
    </tr>
  `).join("") || `<tr><td colspan="9" class="empty">无已平仓回合</td></tr>`;
}

function boot() {
  document.getElementById("strategyId").textContent = DATA.strategy_id || "-";
  const eq = DATA.equity || [];
  const start = eq[0]?.date || "-";
  const end = eq[eq.length - 1]?.date || "-";
  document.getElementById("headerMeta").textContent =
    `区间 ${start} ~ ${end} · asof=${DATA.asof || "-"} · mode=${DATA.mode || "-"}`;
  const notes = [];
  if (DATA.universe_note) notes.push(DATA.universe_note);
  (DATA.warnings || []).forEach(w => notes.push(w));
  if (DATA.disclaimer) notes.push(DATA.disclaimer);
  document.getElementById("footerNote").textContent = notes.join(" · ");
  renderStats();
  renderChart();
  renderPeriodList();
  renderTrades();
  renderTrips();
  if ((DATA.periods || []).length) selectPeriod(0);
  document.getElementById("filterDate").addEventListener("input", renderTrades);
  document.getElementById("filterSymbol").addEventListener("input", renderTrades);
}
boot();
</script>
</body>
</html>
"""


def render_portfolio_html(out: dict[str, Any], dest: Path) -> Path:
    """生成自包含交互 HTML 报告，返回写入路径。"""
    model = build_portfolio_report_model(out)
    dest = dest.expanduser().resolve()
    if dest.suffix.lower() != ".html":
        dest = dest.with_suffix(".html")
    dest.parent.mkdir(parents=True, exist_ok=True)
    title = str(model.get("strategy_id") or "portfolio")
    payload = json.dumps(model, ensure_ascii=False)
    # 避免 </script> 截断
    payload = payload.replace("<", "\\u003c")
    html = (
        _HTML_TEMPLATE.replace("__TITLE__", title)
        .replace("__DATA__", payload)
    )
    dest.write_text(html, encoding="utf-8")
    return dest


def render_portfolio_html_from_json(json_path: Path, dest: Path | None = None) -> Path:
    """从已有回测 JSON 离线生成 HTML。"""
    raw = json.loads(json_path.expanduser().resolve().read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("回测 JSON 须为对象")
    out_path = dest
    if out_path is None:
        out_path = json_path.with_suffix(".html")
    return render_portfolio_html(raw, out_path)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="从组合回测 JSON 生成交互 HTML 报告")
    parser.add_argument("json_path", type=Path, help="portfolio 回测结果 JSON")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="HTML 输出路径（默认与 JSON 同名 .html）",
    )
    args = parser.parse_args(argv)
    path = render_portfolio_html_from_json(args.json_path, args.output)
    print(f"报告已保存: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
