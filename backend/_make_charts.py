# -*- coding: utf-8 -*-
"""为产出报告每个调仓区间生成「事后最优标的 + 实际持仓」价格图（HTML）。

总览卡片：区间内 最优(蓝) vs 实际(橙) 归一化线。点击卡片打开 K 线详情：
- 切换 最佳/实际 标的的 K 线（OHLC + 成交量）
- 悬停显示该日 OHLC 与当日 日志（决策/买卖事件）
- 拖框缩放（鼠标框选区间放大），双击或按钮复位
"""
import json, os

REPORT = "out/backtest_shared_etf_rotation.json"
CACHE = "data/a_stock_data/klines/day"
OUT = "out/_best_stock_price_charts.html"
POOL = json.load(open("config/etf_core_sub_pool.json", encoding="utf-8"))["names"]


def load_klines():
    c = {}
    for fn in os.listdir(CACHE):
        if not fn.endswith(".json"):
            continue
        pfx = fn[:-5]; sym = pfx[2:]
        d = json.load(open(os.path.join(CACHE, fn), encoding="utf-8"))
        c[sym] = d.get("rows", [])
    return c


def close_map(rows):
    return {r["datetime"][:10]: r["close"] for r in rows}


def close_on_or_before(m, date):
    last = None
    for k in sorted(m):
        if k <= date:
            last = m[k]
        else:
            break
    return last


def max_date(closes):
    mx = None
    for m in closes.values():
        if m:
            d = max(m)
            if mx is None or d > mx:
                mx = d
    return mx


def ohlc_in(rows, d0, d1):
    out = []
    for r in rows:
        d = r["datetime"][:10]
        if d0 <= d <= d1:
            out.append([d, r["open"], r["high"], r["low"], r["close"], r.get("volume", 0)])
    return out


def build_cards(rep, closes, end):
    rebs = rep["rebalances"]
    dates = [r["date"] for r in rebs]
    names = POOL

    # 全局事件索引：trade 事件 + 决策事件
    def trade_events():
        for t in rep["trades"]:
            sym = t["symbol"]
            act = "买入" if t.get("side") == "buy" else "卖出"
            yield {"d": t["date"], "sym": sym, "type": "trade",
                   "text": f"{act} {names.get(sym, sym)} @{t.get('price'):.4f}×{int(t.get('shares') or 0)}股 原因:{t.get('reason','')}"}

    def decision_events():
        for reb in rebs:
            sel = reb.get("selection", [])
            top = ", ".join(f"{s['symbol']} {s['score']:.2f}" for s in sel[:3])
            tgt = reb.get("targets", [])
            tn = ", ".join(names.get(x, x) for x in tgt)
            yield {"d": reb["date"], "sym": tgt[0] if tgt else None, "type": "decision",
                   "text": f"决策日·调仓至[{tn}] | 得分前三: {top}"}

    all_events = list(trade_events()) + list(decision_events())

    cards = []
    for i, reb in enumerate(rebs):
        d = reb["date"]
        nxt = dates[i + 1] if i + 1 < len(dates) else end
        held = list(reb["targets"])

        # 事后最优：全池前向收益最高
        best = None
        for sym in POOL:
            cm = close_map(closes.get(sym, []))
            entry = close_on_or_before(cm, d)
            if entry is None:
                continue
            exitc = close_on_or_before(cm, nxt) or entry
            r = exitc / entry - 1.0
            if best is None or r > best[1]:
                best = (sym, r)
        if best is None:
            continue
        bsym, br = best

        def mk(sym, ret):
            rows = closes.get(sym, [])
            o = ohlc_in(rows, d, nxt)
            return {"name": names.get(sym, sym), "symbol": sym, "ret": ret, "ohlc": o}

        card = {
            "date": d, "next": nxt,
            "best": mk(bsym, br * 100.0),
            "held": [mk(s, (close_on_or_before(close_map(closes.get(s, [])), nxt)
                            / close_on_or_before(close_map(closes.get(s, [])), d) - 1.0) * 100.0)
                     for s in held],
            "heldBest": bsym in held,
            "events": [e for e in all_events if d <= e["d"] <= nxt],
        }
        cards.append(card)
    return cards


def main():
    rep = json.load(open(REPORT, encoding="utf-8"))
    raw = load_klines()
    closes = {s: close_map(rows) for s, rows in raw.items()}
    end = max_date(closes)
    cards = build_cards(rep, raw, end)
    n_ok = sum(1 for c in cards if c["heldBest"])
    cards_json = json.dumps(cards, ensure_ascii=False)
    html = TEMPLATE.replace("__CARDS__", cards_json) \
                   .replace("__N__", str(len(cards))) \
                   .replace("__N_OK__", str(n_ok)) \
                   .replace("__N_MISS__", str(len(cards) - n_ok)) \
                   .replace("__REPORT__", REPORT)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"写出 {OUT} 共 {len(cards)} 期，持最优 {n_ok} 期，错过 {len(cards) - n_ok} 期")


TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ETF轮动 · 逐区间最优 vs 实际 · K线详情</title>
<style>
.viz-root{color-scheme:light;--surface-1:#fcfcfb;--surface-0:#f9f9f7;--ink:#0b0b0b;--ink2:#52514e;--muted:#898781;
 --grid:#e1e0d9;--baseline:#c3c2b7;--good:#006300;--bad:#c90000;--best:#2a78d6;--held:#eb6834;
 --up:#e34948;--down:#1baf7a;--border:rgba(11,11,11,.10);}
@media (prefers-color-scheme: dark){
:root:where(:not([data-theme="light"])) .viz-root{color-scheme:dark;--surface-1:#1a1a19;--surface-0:#0d0d0d;--ink:#fff;--ink2:#c3c2b7;
 --grid:#2c2c2a;--baseline:#383835;--good:#0ca30c;--bad:#ff8080;--best:#3987e5;--held:#d95926;
 --up:#e66767;--down:#199e70;--border:rgba(255,255,255,.10);}
}
:root[data-theme="dark"] .viz-root{color-scheme:dark;--surface-1:#1a1a19;--surface-0:#0d0d0d;--ink:#fff;--ink2:#c3c2b7;
 --grid:#2c2c2a;--baseline:#383835;--good:#0ca30c;--bad:#ff8080;--best:#3987e5;--held:#d95926;
 --up:#e66767;--down:#199e70;--border:rgba(255,255,255,.10);}
.viz-root{background:var(--surface-0);color:var(--ink);font-family:system-ui,-apple-system,"Segoe UI",sans-serif;padding:24px;}
h1{font-size:20px;margin:0 0 4px;} .sub{color:var(--ink2);font-size:13px;margin-bottom:20px;}
.stats{display:flex;gap:20px;margin-bottom:20px;font-size:14px;} .stats b{font-size:20px;}
.ok b{color:var(--good);} .miss b{color:var(--bad);}
.hint{font-size:12px;color:var(--muted);margin-bottom:12px;}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:14px;}
.card{background:var(--surface-1);border:1px solid var(--border);border-radius:8px;padding:10px;cursor:pointer;transition:transform .05s;}
.card:hover{border-color:var(--baseline);transform:translateY(-1px);}
.card h2{font-size:13px;margin:0 0 6px;font-weight:600;}
.card .meta{font-size:11px;color:var(--muted);margin-bottom:6px;display:flex;justify-content:space-between;}
.badge{font-size:11px;font-weight:700;padding:1px 7px;border-radius:10px;}
.badge.ok{color:var(--good);background:color-mix(in srgb,var(--good) 14%,transparent);}
.badge.miss{color:var(--bad);background:color-mix(in srgb,var(--bad) 14%,transparent);}
.legend{font-size:11px;color:var(--ink2);margin:6px 2px 0;line-height:1.5;}
.legend .b{color:var(--best);font-weight:700;} .legend .h{color:var(--held);font-weight:700;}
.openhint{font-size:11px;color:var(--muted);margin-top:6px;text-align:center;}

/* 详情弹层 */
.mask{position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:40;display:none;align-items:center;justify-content:center;padding:20px;}
.mask.open{display:flex;}
.detail{background:var(--surface-0);border:1px solid var(--border);border-radius:10px;width:min(1080px,96vw);max-height:94vh;
 display:flex;flex-direction:column;box-shadow:0 10px 40px rgba(0,0,0,.25);}
.detail .dhead{display:flex;align-items:center;gap:10px;padding:12px 16px;border-bottom:1px solid var(--border);flex-wrap:wrap;}
.detail .dhead h3{margin:0;font-size:15px;}
.detail .dhead .seg{display:flex;border:1px solid var(--border);border-radius:7px;overflow:hidden;}
.detail .dhead .seg button{background:none;border:none;color:var(--ink2);padding:4px 10px;font-size:12px;cursor:pointer;}
.detail .dhead .seg button.active{background:var(--surface-1);color:var(--ink);}
.detail .dhead .spacer{flex:1;}
.detail .dhead button{background:none;border:1px solid var(--border);color:var(--ink2);border-radius:6px;padding:4px 10px;font-size:12px;cursor:pointer;}
.detail .dhead button:hover{color:var(--ink);border-color:var(--baseline);}
.dbody{display:flex;min-height:0;flex:1;}
.chart-wrap{flex:1;min-width:0;position:relative;padding:10px;}
#kline{width:100%;height:100%;display:block;}
.logpanel{width:300px;min-width:280px;border-left:1px solid var(--border);overflow-y:auto;padding:12px;background:var(--surface-1);}
.logpanel h4{margin:0 0 8px;font-size:12px;color:var(--muted);font-weight:600;}
.log-item{font-size:12px;color:var(--ink2);line-height:1.55;border-left:3px solid var(--border);padding:3px 8px;margin:2px 0;}
.log-item.decision{border-color:var(--best);}
.log-item.trade.buy{border-color:var(--up);}
.log-item.trade.sell{border-color:var(--down);}
.log-item.hover{background:color-mix(in srgb,var(--best) 10%,transparent);border-radius:4px;}
.log-item.dim{opacity:.45;}
.ohlc-readout{font-size:12px;color:var(--ink2);margin-bottom:8px;}
.ohlc-readout b{color:var(--ink);font-weight:600;}
.zoombox{position:absolute;border:1px solid var(--best);background:color-mix(in srgb,var(--best) 14%,transparent);pointer-events:none;display:none;}
table{width:100%;border-collapse:collapse;margin-top:28px;font-size:12px;}
th,td{border-bottom:1px solid var(--border);padding:6px 8px;text-align:left;}
th{color:var(--muted);font-weight:600;}
.n{font-variant-numeric:tabular-nums;} .good{color:var(--good);} .bad{color:var(--bad);}
.toggle{font-size:12px;color:var(--ink2);margin:4px 0 14px;}
.toggle button{background:none;border:1px solid var(--border);color:var(--ink2);border-radius:6px;padding:3px 10px;cursor:pointer;font-size:12px;}
.toggle button.active{color:var(--ink);border-color:var(--baseline);}
</style></head>
<body class="viz-root">
<h1>ETF 动量轮动 · 逐调仓区间：事后最优标的 vs 实际持仓</h1>
<div class="sub">点击卡片查看该区间 K 线详情：悬停显示日志、拖框缩放。前复权，总览线以区间首日归一化为 100。数据源：__REPORT__</div>
<div class="stats">
  <span class="ok">持到最优 <b>__N_OK__</b> 期</span>
  <span class="miss">错过最优 <b>__N_MISS__</b> 期</span>
  <span>共 <b>__N__</b> 期</span>
</div>
<div class="hint">悬停 K 线看当日日志；按住左键拖框可放大区间；双击图表或点「复位」回到全区间。</div>
<div class="toggle"><button data-view="charts" class="active">图表</button><button data-view="table">表格</button></div>
<div id="charts" class="grid"></div>
<div id="table" style="display:none"></div>

<div class="mask" id="mask">
  <div class="detail">
    <div class="dhead">
      <h3 id="dTitle"></h3>
      <div class="seg" id="symSeg"></div>
      <span class="spacer"></span>
      <button id="zoomReset">复位</button>
      <button id="dClose">关闭</button>
    </div>
    <div class="dbody">
      <div class="chart-wrap" id="chartWrap"><svg id="kline"></svg><div class="zoombox" id="zoombox"></div></div>
      <div class="logpanel">
        <h4>当日日志</h4>
        <div class="ohlc-readout" id="ohlcReadout"></div>
        <div id="dayLog"></div>
        <h4 style="margin-top:14px">区间日志（按时间）</h4>
        <div id="journal"></div>
      </div>
    </div>
  </div>
</div>

<script>
const CARDS = __CARDS__;
const COL = {best:getCss('--best'),held:getCss('--held'),up:getCss('--up'),down:getCss('--down'),grid:getCss('--grid')};
function getCss(v){return getComputedStyle(document.body).getPropertyValue(v).trim();}
const W=360,H=190,L=6,R=30,T=8,B=16;

/* ---------- 总览卡片 ---------- */
function fmtRet(v){return (v>=0?'+':'')+v.toFixed(1)+'%';}
function normSer(o){ // ohlc -> 归一化 close 序列
  if(!o||!o.length)return null; const base=o[0][4];
  return o.map(p=>[p[0], p[4]/base*100.0]);
}
function buildCard(c){
  const el=document.createElement('div');el.className='card';
  const badge=c.heldBest?'<span class="badge ok">✓ 持最优</span>':'<span class="badge miss">✗ 错过</span>';
  const bser=normSer(c.best.ohlc);
  el.innerHTML=`<div class="meta"><span>${c.date} → ${c.next}</span>${badge}</div>
    <h2>最佳 ${c.best.name} <span class="good">${fmtRet(c.best.ret)}</span></h2>
    <svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}" preserveAspectRatio="xMidYMid meet"></svg>
    <div class="legend">${bser?`<span class="b">● 最佳 ${c.best.name} ${fmtRet(c.best.ret)}</span>`:''}<br/>
    ${c.held.map(h=>`<span class="h">▲ 实际 ${h.name} ${fmtRet(h.ret)}</span>`).join('<br/>')}</div>
    <div class="openhint">点击查看 K 线详情</div>`;
  const svg=el.querySelector('svg');
  const allSer=[bser,...c.held.map(h=>normSer(h.ohlc))].filter(Boolean);
  let lo=100,hi=100; allSer.forEach(s=>s.forEach(p=>{if(p[1]<lo)lo=p[1];if(p[1]>hi)hi=p[1];}));
  const pad=(hi-lo)*0.08;lo-=pad;hi+=pad;
  if(bser) drawLine(svg,bser,'best',lo,hi);
  c.held.forEach(h=>{const s=normSer(h.ohlc); if(s) drawLine(svg,s,'held',lo,hi);});
  el.addEventListener('click',()=>openDetail(CARDS.indexOf(c)));
  return el;
}
function drawLine(svg,ser,kind,lo,hi){
  const ns='http://www.w3.org/2000/svg';const n=ser.length;
  const x=i=>L+i*(W-L-R)/(n-1||1), y=v=>T+(H-T-B)*(1-(v-lo)/(hi-lo));
  const path=ser.map((p,i)=>`${i?'L':'M'}${x(i).toFixed(1)},${y(p[1]).toFixed(1)}`).join('');
  let g=svg.querySelector(`[data-kind="${kind}"]`);
  if(!g){g=document.createElementNS(ns,'g');g.setAttribute('data-kind',kind);svg.appendChild(g);}
  const poly=document.createElementNS(ns,'path');poly.setAttribute('d',path);poly.setAttribute('fill','none');
  poly.setAttribute('stroke',kind==='best'?COL.best:COL.held);poly.setAttribute('stroke-width','2');
  poly.setAttribute('stroke-linejoin','round');poly.setAttribute('stroke-linecap','round');g.appendChild(poly);
  const dot=document.createElementNS(ns,'circle');dot.setAttribute('cx',x(0));dot.setAttribute('cy',y(100));
  dot.setAttribute('r','2.6');dot.setAttribute('fill',kind==='best'?COL.best:COL.held);g.appendChild(dot);
  const lp=ser[n-1];const lab=document.createElementNS(ns,'text');
  lab.setAttribute('x',x(n-1));lab.setAttribute('y',y(lp[1])-4);
  lab.setAttribute('text-anchor',n>1?'end':'start');lab.setAttribute('font-size','9');
  lab.setAttribute('fill',kind==='best'?COL.best:COL.held);lab.textContent=fmtRet(lp[1]-100);g.appendChild(lab);
}

/* ---------- K 线详情 ---------- */
const kW=760,kH=420,PL=52,PR=14,PT=12,PB=64; // 图表区（价格）
const VH=56;                                   // 成交量区高
let st=null; // {card, symIdx, dates, ohlc, i0, i1, drag:{active,sx,ix0}, hover:i}

function openDetail(idx){
  const c=CARDS[idx];
  st={card:c, symIdx:0, i0:0, i1:0, hover:-1, drag:null};
  st.dates=c.best.ohlc.map(p=>p[0]);
  st.ohlc=c.best.ohlc;
  st.i1=st.dates.length-1;
  buildSymSeg(); renderDetail();
  document.getElementById('mask').classList.add('open');
}
function closeDetail(){document.getElementById('mask').classList.remove('open');st=null;}
function buildSymSeg(){
  const seg=document.getElementById('symSeg');seg.innerHTML='';
  const c=st.card;const syms=[c.best,...c.held];
  syms.forEach((s,i)=>{
    const b=document.createElement('button');
    b.textContent=`${s.symbol} ${s.name}`;b.className=i===st.symIdx?'active':'';
    b.onclick=()=>{st.symIdx=i;st.dates=s.ohlc.map(p=>p[0]);st.ohlc=s.ohlc;st.i0=0;st.i1=st.dates.length-1;
      buildSymSeg();renderDetail();};
    seg.appendChild(b);
  });
  document.getElementById('dTitle').textContent=`${c.date} → ${c.next} · ${c.best.name}(最佳${fmtRet(c.best.ret)}) vs ${c.held[0]?c.held[0].name:''}(${c.held[0]?fmtRet(c.held[0].ret):''})`;
}
function renderDetail(){
  const svg=document.getElementById('kline');svg.innerHTML='';
  if(!st||!st.ohlc.length)return;
  const cur=st.ohlc;const curDates=st.dates;const i0=Math.max(0,st.i0),i1=Math.min(cur.length-1,st.i1);
  const vis=i1-i0+1;
  // 时间轴 & 价格域
  const px=i=>{const rel=(i-i0)/(vis-1||1);return PL+rel*(kW-PL-PR);};
  const n=cur.length;
  let lo=Infinity,hi=-Infinity;
  for(let i=i0;i<=i1;i++){const c=cur[i];if(c[2]<lo)lo=c[2];if(c[3]>hi)hi=c[3];}
  const pad=(hi-lo)*0.08;lo-=pad;hi+=pad;
  const py=v=>PT+(kH-PT-PB-VH)*(1-(v-lo)/(hi-lo));
  const ns='http://www.w3.org/2000/svg';
  const add=(el)=>{svg.appendChild(el);return el;};
  // 网格与价格轴
  for(let g=0;g<=4;g++){
    const val=lo+(hi-lo)*g/4, y=py(val);
    const gl=add(createEl(ns,'line'));gl.setAttribute('x1',PL);gl.setAttribute('x2',kW-PR);
    gl.setAttribute('y1',y);gl.setAttribute('y2',y);gl.setAttribute('stroke',COL.grid);gl.setAttribute('stroke-width','1');
    const tx=add(createEl(ns,'text'));tx.setAttribute('x',PL-4);tx.setAttribute('y',y+3);
    tx.setAttribute('text-anchor','end');tx.setAttribute('font-size','9');tx.setAttribute('fill',getCss('--muted'));
    tx.textContent=val.toFixed(2);
  }
  // 蜡烛 + 成交量
  const candleW=Math.max(2,Math.min(18,(kW-PL-PR)/vis*0.7));
  const vmax=Math.max(...cur.slice(i0,i1+1).map(p=>p[5]||0),1);
  for(let i=i0;i<=i1;i++){
    const c=cur[i];const x=px(i);
    const up=c[4]>=c[1];const col=up?COL.up:COL.down;
    const wick=add(createEl(ns,'line'));wick.setAttribute('x1',x);wick.setAttribute('x2',x);
    wick.setAttribute('y1',py(c[3]));wick.setAttribute('y2',py(c[2]));wick.setAttribute('stroke',col);wick.setAttribute('stroke-width','1');
    const bodyH=Math.abs(py(c[4])-py(c[1]));
    const body=add(createEl(ns,'rect'));body.setAttribute('x',x-candleW/2);body.setAttribute('y',Math.min(py(c[4]),py(c[1])));
    body.setAttribute('width',candleW);body.setAttribute('height',Math.max(1,bodyH));body.setAttribute('fill',col);
    // 成交量
    const vy=kH-PB+VH-(c[5]||0)/vmax*VH;
    const vb=add(createEl(ns,'rect'));vb.setAttribute('x',x-candleW/2);vb.setAttribute('y',vy);
    vb.setAttribute('width',candleW);vb.setAttribute('height',Math.max(1,(kH-PB+VH)-vy));vb.setAttribute('fill',col);
    vb.setAttribute('opacity','0.55');
    // 悬停高亮
    if(i===st.hover){
      const hl=add(createEl(ns,'rect'));hl.setAttribute('x',x-candleW);hl.setAttribute('y',PT);
      hl.setAttribute('width',candleW*2);hl.setAttribute('height',kH-PT-PB);hl.setAttribute('fill',COL.best);hl.setAttribute('opacity','0.12');
    }
  }
  // 决策日标记
  drawDecisionMarkers(svg,curDates,px);
  // 日期轴
  const step=Math.max(1,Math.floor(vis/6));
  for(let i=i0;i<=i1;i+=step){
    const tx=add(createEl(ns,'text'));tx.setAttribute('x',px(i));tx.setAttribute('y',kH-PB+VH+14);
    tx.setAttribute('text-anchor','middle');tx.setAttribute('font-size','9');tx.setAttribute('fill',getCss('--muted'));
    tx.textContent=curDates[i].slice(5);
  }
  // 横线
  const vl=document.createElement('div');vl.id='klineVline';
  const hb=add(createEl(ns,'line'));hb.setAttribute('data-role','hoverbar');hb.setAttribute('y1',PT);hb.setAttribute('y2',kH-PB);
  hb.setAttribute('stroke',getCss('--muted'));hb.setAttribute('stroke-dasharray','2 2');hb.style.display='none';
  updateJournal();
}
function drawDecisionMarkers(svg,dates,px){
  const c=st.card;const ns='http://www.w3.org/2000/svg';
  dates.forEach((d,i)=>{
    if(d===c.date||d===c.next){
      const m=document.createElementNS(ns,'path');
      const x=px(i);const y=kH-PB+8;
      m.setAttribute('d',`M${x},${y} l6,0 l-3,7 z`);
      m.setAttribute('fill',d===c.date?'#8a6d1a':getCss('--muted'));
      svg.appendChild(m);
    }
  });
}
function createEl(ns,t){return document.createElementNS(ns,t);}
function updateJournal(){
  const c=st.card;
  const day=document.getElementById('dayLog');const j=document.getElementById('journal');
  const hover=st.hover>=0&&st.hover<st.dates.length?st.dates[st.hover]:null;
  // 当日日志
  const dayEvents=c.events.filter(e=>e.d===hover);
  const ro=document.getElementById('ohlcReadout');
  if(hover!==null&&st.ohlc[st.hover]){
    const o=st.ohlc[st.hover];
    ro.innerHTML=`<b>${hover}</b> 开 ${o[1].toFixed(3)} 高 ${o[2].toFixed(3)} 低 ${o[3].toFixed(3)} 收 <b>${o[4].toFixed(3)}</b> 量 ${(o[5]/10000).toFixed(0)}万`;
  } else ro.innerHTML='';
  if(dayEvents.length){
    day.innerHTML=dayEvents.map(logItem).join('');
  } else day.innerHTML=hover?`<div class="log-item">当日无调仓/交易事件</div>`:'<div class="log-item">悬停 K 线查看当日日志</div>';
  // 区间日志
  j.innerHTML=c.events.slice().sort((a,b)=>a.d<b.d?-1:a.d>b.d?1:0)
    .map(e=>{
      const cls=logCls(e);
      const hl=e.d===hover?' hover':'';
      return `<div class="log-item ${cls}${hl}">${e.d} · ${e.text}</div>`;
    }).join('');
}
function logCls(e){return e.type==='decision'?'decision':(e.text.startsWith('买入')?'trade buy':'trade sell');}
function logItem(e){return `<div class="log-item ${logCls(e)}">${e.d} · ${e.text}</div>`;}

/* ---------- 详情交互：悬停 + 拖框缩放 ---------- */
function bindDetailInteraction(){
  const svg=document.getElementById('kline');const wrap=document.getElementById('chartWrap');const zb=document.getElementById('zoombox');
  const toIndex=(clientX)=>{
    const r=svg.getBoundingClientRect();const fx=(clientX-r.left)/r.width*(kW-PL-PR)+PL;
    const n=st.dates.length;const i0=st.i0,i1=st.i1,vis=i1-i0+1;
    const rel=(fx-PL)/(kW-PL-PR);let idx=i0+Math.round(rel*(vis-1));
    return Math.max(0,Math.min(n-1,idx));
  };
  svg.addEventListener('mousemove',ev=>{
    if(st.drag)return;
    const idx=toIndex(ev.clientX);st.hover=idx;renderDetail();
    const vline=svg.querySelector('[data-role="hoverbar"]');
    if(vline){
      const n=st.dates.length,i0=st.i0,i1=st.i1,vis=i1-i0+1;
      const x=PL+((idx-i0)/(vis-1||1))*(kW-PL-PR);
      vline.setAttribute('x1',x);vline.setAttribute('x2',x);vline.style.display='';
    }
  });
  svg.addEventListener('mouseleave',()=>{if(!st.drag){st.hover=-1;renderDetail();}});
  svg.addEventListener('mousedown',ev=>{
    st.drag={sx:ev.clientX, ix0:toIndex(ev.clientX)};
    zb.style.display='block';zb.style.left='0';zb.style.top='0';zb.style.width='0';zb.style.height='0';
  });
  svg.addEventListener('mousemove',ev=>{
    if(!st.drag)return;
    const r=svg.getBoundingClientRect();
    const x1=ev.clientX-r.left,x0=st.drag.sx-r.left;
    zb.style.left=Math.min(x0,x1)+'px';zb.style.width=Math.abs(x1-x0)+'px';
    zb.style.top='0';zb.style.height=r.height+'px';
  });
  svg.addEventListener('mouseup',ev=>{
    if(!st.drag)return;
    const a=toIndex(st.drag.sx),b=toIndex(ev.clientX);
    st.drag=null;zb.style.display='none';
    if(a!==b){st.i0=Math.min(a,b);st.i1=Math.max(a,b);renderDetail();}
  });
  svg.addEventListener('dblclick',()=>{st.i0=0;st.i1=st.dates.length-1;renderDetail();});
  document.getElementById('zoomReset').onclick=()=>{st.i0=0;st.i1=st.dates.length-1;renderDetail();};
  document.getElementById('dClose').onclick=closeDetail;
  document.getElementById('mask').addEventListener('click',ev=>{if(ev.target.id==='mask')closeDetail();});
}
document.addEventListener('keydown',ev=>{if(ev.key==='Escape')closeDetail();});

/* ---------- 表格 & 视图切换 ---------- */
function renderTable(){
  const tb=document.getElementById('table');
  let rows='<table><thead><tr><th>区间</th><th>最佳标的</th><th>最佳收益</th><th>实际持仓</th><th>实际收益</th><th>判定</th></tr></thead><tbody>';
  CARDS.forEach(c=>{
    const heldRet=c.held.map(h=>`${h.name} ${fmtRet(h.ret)}`).join('；');
    const hv=c.held.length?c.held[0].ret:null;
    const hcell=hv===null?'—':`<span class="n ${hv>=0?'good':'bad'}">${fmtRet(hv)}</span>`;
    rows+=`<tr><td class="n">${c.date}→${c.next}</td><td>${c.best.name}</td><td class="n ${c.best.ret>=0?'good':'bad'}">${fmtRet(c.best.ret)}</td><td>${heldRet}</td><td>${hcell}</td><td>${c.heldBest?'✓ 持最优':'✗ 错过'}</td></tr>`;
  });
  rows+='</tbody></table>';tb.innerHTML=rows;
}
document.querySelectorAll('.toggle button').forEach(b=>b.onclick=()=>{
  document.querySelectorAll('.toggle button').forEach(x=>x.classList.remove('active'));b.classList.add('active');
  const v=b.dataset.view;
  document.getElementById('charts').style.display=v==='charts'?'grid':'none';
  document.getElementById('table').style.display=v==='table'?'block':'none';
});

/* ---------- 初始化 ---------- */
function renderOverview(){const g=document.getElementById('charts');g.innerHTML='';CARDS.forEach(c=>g.appendChild(buildCard(c)));}
renderOverview();renderTable();bindDetailInteraction();
</script>
</body></html>
"""


if __name__ == "__main__":
    main()
