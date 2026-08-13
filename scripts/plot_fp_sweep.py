#!/usr/bin/env python3
"""out/fp_sweep.csv から自己完結の HTML レポートを生成する.

    python scripts/plot_fp_sweep.py out/fp_sweep.csv out/fp_sweep.html

外部リソースを一切参照しない（CSS/JS はインライン、フォントはシステム）。
配色は dataviz スキルの検証済みパレットから取り、ordinal ランプは
validate_palette.py で light/dark 両モードとも全項目 PASS を確認済み。
"""

import csv
import html
import json
import sys

# ── 検証済みパレット ────────────────────────────────────────────────────────
# window は順序のあるパラメータなので categorical ではなく ordinal ランプ。
# 単一色相・単調な明度・隣接 ΔL >= 0.06・淡端のコントラスト >= 2:1 を確認済み。
ORDINAL_LIGHT = ["#86b6ef", "#2a78d6", "#104281"]
ORDINAL_DARK = ["#b7d3f6", "#5598e7", "#184f95"]
SERIES1_LIGHT, SERIES1_DARK = "#2a78d6", "#3987e5"

W, H = 760, 330
# 直接ラベルを置く系列が複数あるときだけ右に余白を取る。
# 単一系列で 78px 空けると図が痩せるだけで何も入らない。
M_MULTI = {"t": 18, "r": 78, "b": 46, "l": 68}
M_SINGLE = {"t": 18, "r": 18, "b": 46, "l": 68}
MIN_LABEL_GAP = 14  # 直接ラベル同士の最小垂直間隔 px


def nice_ticks(lo, hi, n=5):
    if hi <= lo:
        hi = lo + 1
    raw = (hi - lo) / n
    mag = 10 ** (len(str(int(raw))) - 1) if raw >= 1 else 10 ** -3
    for m in (1, 2, 2.5, 5, 10, 20, 25, 50, 100):
        step = m * mag
        if step >= raw:
            break
    start = (int(lo / step)) * step
    ticks, v = [], start
    while v <= hi + step * 0.5:
        if v >= lo - 1e-9:
            ticks.append(round(v, 6))
        v += step
    return ticks


def fmt(v):
    if v == int(v):
        return f"{int(v):,}"
    return f"{v:,.2f}".rstrip("0").rstrip(".")


def esc(s):
    return html.escape(str(s), quote=True)


def line_chart(cid, title, sub, xlabel, ylabel, series, xcat=False):
    """series: [{name, points:[(x,y)], light, dark}] — x は数値。"""
    M = M_MULTI if len(series) > 1 else M_SINGLE
    PW, PH = W - M["l"] - M["r"], H - M["t"] - M["b"]
    xs = sorted({p[0] for s in series for p in s["points"]})
    ys = [p[1] for s in series for p in s["points"]]
    ymax = max(ys) if ys else 1
    yticks = nice_ticks(0, ymax * 1.08 if ymax else 1)
    ytop = max(yticks) if yticks else 1

    def px(x):
        if xcat:
            i = xs.index(x)
            return M["l"] + (PW * i / max(1, len(xs) - 1))
        lo, hi = min(xs), max(xs)
        return M["l"] + (PW * (x - lo) / (hi - lo) if hi > lo else 0)

    def py(y):
        return M["t"] + PH - (PH * y / ytop if ytop else 0)

    out = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="{esc(title)}" preserveAspectRatio="xMidYMid meet">']
    # gridlines（1px 実線・サーフェスから一段だけ外した色。破線は使わない）
    for t in yticks:
        out.append(f'<line class="grid" x1="{M["l"]}" y1="{py(t):.1f}" x2="{M["l"]+PW}" y2="{py(t):.1f}"/>')
        out.append(f'<text class="tick tick-y" x="{M["l"]-10}" y="{py(t)+4:.1f}">{fmt(t)}</text>')
    out.append(f'<line class="axis" x1="{M["l"]}" y1="{M["t"]+PH}" x2="{M["l"]+PW}" y2="{M["t"]+PH}"/>')
    for x in xs:
        out.append(f'<text class="tick" x="{px(x):.1f}" y="{M["t"]+PH+20}">{fmt(x)}</text>')
    out.append(f'<text class="axis-title" x="{M["l"]+PW/2:.1f}" y="{H-8}">{esc(xlabel)}</text>')
    out.append(
        f'<text class="axis-title" transform="translate(16,{M["t"]+PH/2:.1f}) rotate(-90)">{esc(ylabel)}</text>'
    )

    tip = []
    label_ys = []
    for si, s in enumerate(series):
        pts = sorted(s["points"])
        d = " ".join(("M" if i == 0 else "L") + f"{px(x):.1f} {py(y):.1f}" for i, (x, y) in enumerate(pts))
        out.append(f'<path class="ln" style="--c:{s["light"]};--cd:{s["dark"]}" d="{d}"/>')
        for x, y in pts:
            # 2px のサーフェスリング付き。線をまたいでも読める
            out.append(f'<circle class="dot" style="--c:{s["light"]};--cd:{s["dark"]}" cx="{px(x):.1f}" cy="{py(y):.1f}" r="4"/>')
        if len(series) > 1:
            ly = py(pts[-1][1]) + 4
            if all(abs(ly - placed) >= MIN_LABEL_GAP for placed in label_ys):
                label_ys.append(ly)
                out.append(f'<text class="dlabel" x="{px(pts[-1][0]) + 8:.1f}" y="{ly:.1f}">{esc(s["name"])}</text>')
        tip.append({"name": s["name"], "light": s["light"], "dark": s["dark"],
                    "pts": [{"x": x, "y": y, "px": round(px(x), 1), "py": round(py(y), 1)} for x, y in pts]})

    out.append(f'<g class="hover-layer"><line class="crosshair" y1="{M["t"]}" y2="{M["t"]+PH}"/></g>')
    out.append("</svg>")

    legend = ""
    if len(series) > 1:
        items = "".join(
            f'<span class="lg"><span class="lg-key" style="--c:{s["light"]};--cd:{s["dark"]}"></span>{esc(s["name"])}</span>'
            for s in series
        )
        legend = f'<div class="legend">{items}</div>'

    meta = json.dumps({"series": tip, "plot": {"l": M["l"], "t": M["t"], "w": PW, "h": PH},
                       "ylabel": ylabel, "xlabel": xlabel})
    return f"""<figure class="chart" id="{esc(cid)}">
  <figcaption><h3>{esc(title)}</h3><p>{esc(sub)}</p></figcaption>
  {legend}
  <div class="plot" data-meta='{esc(meta)}'>{''.join(out)}<div class="tip" hidden></div></div>
</figure>"""


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else "out/fp_sweep.csv"
    dst = sys.argv[2] if len(sys.argv) > 2 else "out/fp_sweep.html"

    rows = []
    with open(src, newline="") as f:
        for r in csv.DictReader(f):
            if not r.get("false_per_s"):
                continue
            rows.append({"group": r["group"], "x": float(r["x"]), "mev": float(r["mev_per_s"] or 0),
                         "fp": float(r["false_per_s"]), "drop": float(r["nn_drop_pct"] or 0)})

    def grp(name):
        return sorted([r for r in rows if r["group"] == name], key=lambda r: r["x"])

    detect_charts = []
    bias_charts = []
    charts = detect_charts

    # 1) 検知閾値 x 時間窓
    wins = [("window1000", "1 ms"), ("window10000", "10 ms"), ("window100000", "100 ms")]
    series = []
    for i, (g, label) in enumerate(wins):
        pts = [(r["x"], r["fp"]) for r in grp(g)]
        if pts:
            series.append({"name": label, "points": pts, "light": ORDINAL_LIGHT[i], "dark": ORDINAL_DARK[i]})
    if series:
        charts.append(line_chart(
            "c-threshold", "検知閾値と誤検知率", "静止シーンなので検知は全て誤検知。時間窓ごとの 3 本。",
            "検知に必要なイベント数 (--count)", "誤検知 /s", series, xcat=True))

    # 2) 近傍相関フィルタ
    nn = grp("nnfilter")
    if nn:
        charts.append(line_chart(
            "c-nn", "近傍相関フィルタの効き", "--count 2 / --window 10ms 固定。0 はフィルタ無効。",
            "8 近傍の相関とみなす時間差 (us)", "誤検知 /s",
            [{"name": "誤検知", "points": [(r["x"], r["fp"]) for r in nn],
              "light": SERIES1_LIGHT, "dark": SERIES1_DARK}], xcat=True))
        charts.append(line_chart(
            "c-nn-drop", "近傍相関フィルタが捨てたイベントの割合", "同上。捨てすぎると本物の動きも落ちる。",
            "8 近傍の相関とみなす時間差 (us)", "除去率 %",
            [{"name": "除去率", "points": [(r["x"], r["drop"]) for r in nn],
              "light": SERIES1_LIGHT, "dark": SERIES1_DARK}], xcat=True))

    # 3) バイアス（イベントレートと誤検知は桁が違うので必ず別チャートにする。二軸は使わない）
    charts = bias_charts
    biases = [("bias_diff_on", "bias_diff_on", "コントラスト閾値。374 が HAL の許す下限、384 が既定"),
              ("bias_fo", "bias_fo", "フォロワ帯域。既定 1477、上限 1800。1600 以上はイベントレートが 0.003 Mev/s 以下"),
              ("bias_pr", "bias_pr", "フォトレセプタ帯域。既定 1250、下限 975")]
    for g, label, note in biases:
        d = grp(g)
        if not d:
            continue
        charts.append(line_chart(
            f"c-{g}-ev", f"{label} とイベントレート", note, label, "Mev/s",
            [{"name": "イベントレート", "points": [(r["x"], r["mev"]) for r in d],
              "light": SERIES1_LIGHT, "dark": SERIES1_DARK}], xcat=True))
        charts.append(line_chart(
            f"c-{g}-fp", f"{label} と誤検知率", "--count 3 / --window 10ms 固定", label, "誤検知 /s",
            [{"name": "誤検知", "points": [(r["x"], r["fp"]) for r in d],
              "light": SERIES1_LIGHT, "dark": SERIES1_DARK}], xcat=True))

    tbody = "".join(
        f"<tr><td>{esc(r['group'])}</td><td>{fmt(r['x'])}</td><td>{r['mev']:.3f}</td>"
        f"<td>{r['fp']:.2f}</td><td>{r['drop']:.1f}</td></tr>" for r in rows)

    page = f"""<title>SilkyEvCam 誤検知カーブ</title>
<style>
:root {{
  color-scheme: light;
  --page:#f9f9f7; --surface:#fcfcfb;
  --text-primary:#0b0b0b; --text-secondary:#52514e; --muted:#898781;
  --grid:#e1e0d9; --axis:#c3c2b7; --border:rgba(11,11,11,0.10);
  --s1:{SERIES1_LIGHT};
  --mono:ui-monospace,"SF Mono",SFMono-Regular,Menlo,Consolas,monospace;
}}
@media (prefers-color-scheme: dark) {{
  :root:where(:not([data-theme="light"])) {{
    color-scheme: dark;
    --page:#0d0d0d; --surface:#1a1a19;
    --text-primary:#ffffff; --text-secondary:#c3c2b7; --muted:#898781;
    --grid:#2c2c2a; --axis:#383835; --border:rgba(255,255,255,0.10);
    --s1:{SERIES1_DARK};
  }}
}}
:root[data-theme="dark"] {{
  color-scheme: dark;
  --page:#0d0d0d; --surface:#1a1a19;
  --text-primary:#ffffff; --text-secondary:#c3c2b7; --muted:#898781;
  --grid:#2c2c2a; --axis:#383835; --border:rgba(255,255,255,0.10);
  --s1:{SERIES1_DARK};
}}
* {{ box-sizing:border-box; }}
body {{
  margin:0; padding:32px 20px 64px;
  background:var(--page); color:var(--text-primary);
  font-family:system-ui,-apple-system,"Segoe UI",sans-serif; line-height:1.6;
}}
.wrap {{ max-width:860px; margin:0 auto; }}
h1 {{ font-size:1.6rem; margin:0 0 4px; }}
section {{ margin-top:44px; }}
.eyebrow {{ font-family:var(--mono); font-size:0.72rem; letter-spacing:0.12em; text-transform:uppercase;
  color:var(--muted); border-top:1px solid var(--border); padding-top:10px; margin-bottom:2px; }}
h2 {{ font-size:1.1rem; margin:0 0 4px; }}
.sec-lede {{ color:var(--text-secondary); font-size:0.9rem; margin:0 0 16px; }}
.stack {{ display:flex; flex-direction:column; gap:20px; }}
.lede {{ color:var(--text-secondary); margin:0 0 8px; }}
.note {{ color:var(--text-secondary); font-size:0.9rem; border-left:2px solid var(--axis); padding-left:12px; margin:16px 0; }}
code {{ font-family:var(--mono); font-size:0.86em; background:var(--surface); border:1px solid var(--border);
  border-radius:3px; padding:1px 5px; }}
.kpis {{ display:flex; flex-wrap:wrap; gap:12px; margin:24px 0 8px; }}
.kpi {{ flex:1 1 180px; background:var(--surface); border:1px solid var(--border); border-radius:4px; padding:14px 16px; }}
.kpi .k {{ font-size:0.82rem; color:var(--text-secondary); }}
.kpi .v {{ font-size:1.5rem; font-weight:600; font-family:var(--mono); letter-spacing:-0.02em; }}
.kpi .u {{ font-size:0.85rem; color:var(--muted); }}
.chart {{ margin:0; background:var(--surface); border:1px solid var(--border); border-radius:4px; padding:16px 12px 8px; }}
figcaption h3 {{ margin:0 0 2px; font-size:0.98rem; font-family:var(--mono); font-weight:600; letter-spacing:-0.01em; }}
figcaption p {{ margin:0; color:var(--text-secondary); font-size:0.86rem; }}
.legend {{ display:flex; gap:16px; flex-wrap:wrap; margin:10px 0 0; font-size:0.85rem; color:var(--text-secondary); }}
.lg {{ display:inline-flex; align-items:center; gap:6px; }}
.lg-key {{ width:16px; height:2px; background:var(--c); border-radius:1px; }}
.plot {{ position:relative; overflow-x:auto; }}
svg {{ display:block; width:100%; height:auto; min-width:520px; }}
.grid {{ stroke:var(--grid); stroke-width:1; }}
.axis {{ stroke:var(--axis); stroke-width:1; }}
.tick {{ fill:var(--muted); font-size:11px; text-anchor:middle; font-family:var(--mono); }}
.tick-y {{ text-anchor:end; }}
.axis-title {{ fill:var(--text-secondary); font-size:11px; text-anchor:middle; font-family:var(--mono); }}
.ln {{ fill:none; stroke:var(--c); stroke-width:2; stroke-linejoin:round; stroke-linecap:round; }}
.dot {{ fill:var(--c); stroke:var(--surface); stroke-width:2; }}
.dlabel {{ fill:var(--text-secondary); font-size:11px; font-family:var(--mono); }}
.crosshair {{ stroke:var(--axis); stroke-width:1; opacity:0; }}
.plot.on .crosshair {{ opacity:1; }}
.tip {{ position:absolute; pointer-events:none; background:var(--surface); border:1px solid var(--border);
  border-radius:8px; padding:8px 10px; font-size:0.82rem; box-shadow:0 2px 10px rgba(0,0,0,0.14); min-width:120px; }}
.tip .tx {{ color:var(--text-secondary); font-size:0.78rem; margin-bottom:4px; }}
.tip .row {{ display:flex; align-items:center; gap:6px; }}
.tip .k {{ width:14px; height:2px; border-radius:1px; flex:none; }}
.tip .v {{ font-weight:600; font-family:var(--mono); }}
.tip .n {{ color:var(--text-secondary); }}
details {{ margin-top:32px; }}
summary {{ cursor:pointer; color:var(--text-secondary); }}
.tblwrap {{ overflow-x:auto; margin-top:12px; }}
table {{ border-collapse:collapse; font-size:0.82rem; width:100%; min-width:460px; font-family:var(--mono); }}
th,td {{ text-align:right; padding:5px 10px; border-bottom:1px solid var(--border); font-variant-numeric:tabular-nums; }}
th:first-child, td:first-child {{ text-align:left; font-variant-numeric:normal; }}
th {{ color:var(--text-secondary); font-weight:600; }}
@media (prefers-color-scheme: dark) {{
  :root:where(:not([data-theme="light"])) .lg-key,
  :root:where(:not([data-theme="light"])) .ln,
  :root:where(:not([data-theme="light"])) .dot {{ --c:var(--cd); }}
}}
:root[data-theme="dark"] .lg-key,
:root[data-theme="dark"] .ln,
:root[data-theme="dark"] .dot {{ --c:var(--cd); }}
</style>

<div class="wrap">
<h1>SilkyEvCam 動き検知 — 誤検知率の掃引</h1>
<p class="lede">静止した白壁に向けた状態での計測。動くものが無いので、検知は全て誤検知。</p>

<div class="kpis">
  <div class="kpi"><div class="k">既定設定での誤検知</div><div class="v">0.00<span class="u"> /s</span></div></div>
  <div class="kpi"><div class="k">判定処理時間 p99</div><div class="v">6<span class="u"> us</span></div></div>
  <div class="kpi"><div class="k">USB 配送グリッド</div><div class="v">4.00<span class="u"> ms</span></div></div>
  <div class="kpi"><div class="k">静止時イベントレート</div><div class="v">0.02<span class="u"> Mev/s</span></div></div>
</div>

<p class="note">既定設定（<code>--count 10 --window 1000 --cell 16</code>）では誤検知は 0/s。
以下のグラフに現れるのは、応答を見るために意図的に緩めた領域での挙動。
判定処理時間は motion_probe、配送グリッドは trigger_probe（完全周期の基準信号）による計測。</p>

<section>
  <p class="eyebrow">software</p>
  <h2>判定パラメータ</h2>
  <p class="sec-lede">コードだけで変えられる部分。イベントが届いた後の数え方を決める。</p>
  <div class="stack">{{DETECT}}</div>
</section>

<section>
  <p class="eyebrow">sensor</p>
  <h2>センサバイアス</h2>
  <p class="sec-lede">画素がそもそも発火するかどうかを決める部分。<code>I_LL_Biases</code> 経由でカメラに書き込む。
  ここで測っているのは静止シーンでのノイズ応答であって、応答速度の利得ではない。</p>
  <div class="stack">{{BIAS}}</div>
</section>

<details>
  <summary>全データ（表）</summary>
  <div class="tblwrap"><table>
    <thead><tr><th>group</th><th>x</th><th>Mev/s</th><th>誤検知/s</th><th>除去率 %</th></tr></thead>
    <tbody>{tbody}</tbody>
  </table></div>
</details>
</div>

<script>
document.querySelectorAll(".plot").forEach(function (plot) {{
  var meta = JSON.parse(plot.dataset.meta);
  var svg = plot.querySelector("svg");
  var cross = plot.querySelector(".crosshair");
  var tip = plot.querySelector(".tip");
  var dark = function () {{
    var t = document.documentElement.getAttribute("data-theme");
    if (t === "dark") return true;
    if (t === "light") return false;
    return window.matchMedia("(prefers-color-scheme: dark)").matches;
  }};

  function show(ev) {{
    var r = svg.getBoundingClientRect();
    var vx = (ev.clientX - r.left) / r.width * {W};
    var best = null;
    meta.series.forEach(function (s) {{
      s.pts.forEach(function (p) {{
        var d = Math.abs(p.px - vx);
        if (!best || d < best.d) best = {{ d: d, x: p.x, px: p.px }};
      }});
    }});
    if (!best) return;
    plot.classList.add("on");
    cross.setAttribute("x1", best.px);
    cross.setAttribute("x2", best.px);

    tip.textContent = "";
    var head = document.createElement("div");
    head.className = "tx";
    head.textContent = meta.xlabel + ": " + best.x.toLocaleString();
    tip.appendChild(head);
    meta.series.forEach(function (s) {{
      var p = s.pts.filter(function (q) {{ return q.x === best.x; }})[0];
      if (!p) return;
      var row = document.createElement("div");
      row.className = "row";
      var k = document.createElement("span");
      k.className = "k";
      k.style.background = dark() ? s.dark : s.light;
      var v = document.createElement("span");
      v.className = "v";
      v.textContent = p.y.toLocaleString(undefined, {{ maximumFractionDigits: 2 }});
      var n = document.createElement("span");
      n.className = "n";
      n.textContent = s.name;
      row.appendChild(k); row.appendChild(v); row.appendChild(n);
      tip.appendChild(row);
    }});
    tip.hidden = false;
    var left = best.px / {W} * r.width + 14;
    if (left + tip.offsetWidth > r.width) left = best.px / {W} * r.width - tip.offsetWidth - 14;
    tip.style.left = Math.max(0, left) + "px";
    tip.style.top = "8px";
  }}

  function hide() {{ plot.classList.remove("on"); tip.hidden = true; }}
  plot.addEventListener("pointermove", show);
  plot.addEventListener("pointerleave", hide);
  svg.setAttribute("tabindex", "0");
  svg.addEventListener("focus", function () {{
    var r = svg.getBoundingClientRect();
    show({{ clientX: r.left + r.width / 2 }});
  }});
  svg.addEventListener("blur", hide);
}});
</script>
"""
    page = page.replace("{DETECT}", "".join(detect_charts)).replace("{BIAS}", "".join(bias_charts))
    with open(dst, "w") as f:
        f.write(page)
    print(f"wrote {dst}  ({len(rows)} rows, {len(charts)} charts)")


if __name__ == "__main__":
    main()
