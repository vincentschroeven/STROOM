#!/usr/bin/env python3
"""
EPEX Belgium Stroomtip — HTML Dashboard Generator
Gebruikt ENTSO-E API voor correcte Day-Ahead prijzen per kwartier.
"""

import json
import os
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()
ENTSOE_TOKEN = os.getenv("ENTSOE_TOKEN")

FACTOR = 1.04
OPSLAG = 0.50
BTW    = 1.06
CET    = timezone(timedelta(hours=2))
AREA   = "10YBE----------2"

OUTPUT_PATH = Path(__file__).parent / "index.html"


def fetch_prices(date: datetime) -> list:
    start = date.strftime("%Y%m%d0000")
    end   = (date + timedelta(days=1)).strftime("%Y%m%d0000")
    params = {
        "securityToken": ENTSOE_TOKEN,
        "documentType":  "A44",
        "in_Domain":     AREA,
        "out_Domain":    AREA,
        "periodStart":   start,
        "periodEnd":     end,
    }
    resp = requests.get("https://web-api.tp.entsoe.eu/api", params=params, timeout=30)
    resp.raise_for_status()
    root = ET.fromstring(resp.text)
    ns   = {"ns": "urn:iec62325.351:tc57wg16:451-3:publicationdocument:7:3"}

    resultaat = []
    for ts in root.findall(".//ns:TimeSeries", ns):
        period = ts.find("ns:Period", ns)
        if period is None:
            continue
        resolution = period.find("ns:resolution", ns).text
        start_node = period.find("ns:timeInterval/ns:start", ns)
        if start_node is None:
            continue
        base = datetime.fromisoformat(start_node.text.replace("Z", "+00:00"))

        if resolution == "PT60M":
            for point in period.findall("ns:Point", ns):
                pos   = int(point.find("ns:position", ns).text)
                price = float(point.find("ns:price.amount", ns).text)
                dt    = (base + timedelta(hours=pos - 1)).astimezone(CET)
                for q in range(4):
                    qt = dt + timedelta(minutes=q * 15)
                    resultaat.append((qt.strftime("%H:%M"), price))
        elif resolution == "PT15M":
            for point in period.findall("ns:Point", ns):
                pos   = int(point.find("ns:position", ns).text)
                price = float(point.find("ns:price.amount", ns).text)
                dt    = (base + timedelta(minutes=(pos - 1) * 15)).astimezone(CET)
                resultaat.append((dt.strftime("%H:%M"), price))

    seen = {}
    for label, price in resultaat:
        if label not in seen:
            seen[label] = price
    return sorted(seen.items(), key=lambda x: x[0])


def epex_naar_eind(epex_mwh):
    return round((FACTOR * (epex_mwh / 10) + OPSLAG) * BTW, 2)


def bouw_dag_data(kwartieren, huidig_kwartier=None):
    labels = [k[0] for k in kwartieren]
    eind   = [epex_naar_eind(k[1]) for k in kwartieren]
    n      = len(eind)
    if n == 0:
        return {}

    GRENS_GOEDKOOP = 8.0  # c€/kWh — onder deze grens = groen

    kleuren = []
    for i in range(n):
        if huidig_kwartier and labels[i] == huidig_kwartier:
            kleuren.append('#2a78d6')
        elif eind[i] < GRENS_GOEDKOOP:
            kleuren.append('#1baf7a')
        elif eind[i] >= 17.9:
            kleuren.append('#e34948')
        else:
            kleuren.append('#c3c2b7')

    gem   = sum(eind) / n
    min_p = min(eind); min_i = eind.index(min_p)
    max_p = max(eind); max_i = eind.index(max_p)
    top_goed   = sorted(range(n), key=lambda i: eind[i])[:5]
    top_slecht = sorted(range(n), key=lambda i: eind[i], reverse=True)[:5]

    return {
        "labels": labels, "eind": eind, "kleuren": kleuren,
        "gem": gem,
        "min_p": min_p, "min_label": labels[min_i],
        "max_p": max_p, "max_label": labels[max_i],
        "top_goed":   [(labels[i], eind[i]) for i in top_goed],
        "top_slecht": [(labels[i], eind[i]) for i in top_slecht],
    }


def tip_rijen(items, cls):
    rows = ""
    for lbl, p in items:
        rows += f'<div class="tip-row"><span class="uur">{lbl}</span><span class="{cls}">{p:.2f} c€</span></div>'
    return rows


def morgen_sectie(mo, morgen_str):
    if not mo:
        return f'''
  <div class="dag">
    <div class="dag-header">
      <div><div class="dag-titel">Morgen</div><div class="dag-datum">{morgen_str}</div></div>
    </div>
    <div class="geen-data">Prijzen voor morgen worden verwacht na 13:00.<br>Herlaad de pagina later.</div>
  </div>'''

    return f'''
  <div class="dag">
    <div class="dag-header">
      <div><div class="dag-titel">Morgen</div><div class="dag-datum">{morgen_str}</div></div>
    </div>
    <div class="kpis">
      <div class="kpi goed"><div class="label">Goedkoopste</div><div class="val">{mo["min_p"]:.2f} c€</div><div class="sub">{mo["min_label"]}</div></div>
      <div class="kpi"><div class="label">Gemiddelde</div><div class="val">{mo["gem"]:.2f} c€</div><div class="sub">incl. btw</div></div>
      <div class="kpi slecht"><div class="label">Duurste</div><div class="val">{mo["max_p"]:.2f} c€</div><div class="sub">{mo["max_label"]}</div></div>
    </div>
    <div class="chart-box">
      <div class="legend">
        <span><span class="dot" style="background:#1baf7a"></span>Goedkoop</span>
        <span><span class="dot" style="background:#c3c2b7"></span>Normaal</span>
        <span><span class="dot" style="background:#e34948"></span>Duur</span>
        <span><span class="dot" style="background:#f59e0b"></span>Vast ~17.9 c€</span>
      </div>
      <div style="position:relative;height:180px"><canvas id="chart-mo"></canvas></div>
    </div>
    <div class="tips">
      <div class="tip-card goed"><h3>Beste kwartieren</h3>{tip_rijen(mo["top_goed"], "prijs-goed")}</div>
      <div class="tip-card slecht"><h3>Duurste kwartieren</h3>{tip_rijen(mo["top_slecht"], "prijs-slecht")}</div>
    </div>
  </div>'''


def morgen_chart_js(mo):
    if not mo:
        return ""
    labels = json.dumps(mo["labels"])
    eind   = json.dumps(mo["eind"])
    kleur  = json.dumps(mo["kleuren"])
    return f"maakChart('chart-mo', {labels}, {eind}, {kleur});"


def genereer_html(vd, mo, vandaag_str, morgen_str, gegenereerd_op, huidig_kwartier):
    nu_badge = ""
    if huidig_kwartier and huidig_kwartier in vd["labels"]:
        idx = vd["labels"].index(huidig_kwartier)
        prijs_nu = vd["eind"][idx]
        nu_badge = f'<div class="nu-badge">Nu ({huidig_kwartier}) &nbsp;{prijs_nu:.2f} c€/kWh</div>'

    labels_vd = json.dumps(vd["labels"])
    eind_vd   = json.dumps(vd["eind"])
    kleur_vd  = json.dumps(vd["kleuren"])

    return f"""<!DOCTYPE html>
<html lang="nl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Stroomtip</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.js"></script>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #f5f4f0; color: #0b0b0b; padding: 1.25rem; max-width: 960px; margin: 0 auto; line-height: 1.5; }}
  .top-header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 1.25rem; }}
  .top-header h1 {{ font-size: 18px; font-weight: 500; }}
  .top-header .meta {{ font-size: 11px; color: #898781; text-align: right; }}
  .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 1.25rem; }}
  @media (max-width: 640px) {{ .grid {{ grid-template-columns: 1fr; }} }}
  .dag {{ background: #fff; border-radius: 12px; padding: 1.25rem; border: 0.5px solid rgba(11,11,11,0.10); }}
  .dag-header {{ display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 1rem; flex-wrap: wrap; gap: 8px; }}
  .dag-titel {{ font-size: 15px; font-weight: 500; }}
  .dag-datum {{ font-size: 12px; color: #898781; margin-top: 2px; }}
  .nu-badge {{ background: #e6f1fb; color: #185fa5; font-size: 12px; font-weight: 500; padding: 4px 10px; border-radius: 8px; white-space: nowrap; }}
  .kpis {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; margin-bottom: 1rem; }}
  .kpi {{ background: #f5f4f0; border-radius: 8px; padding: 0.625rem 0.75rem; }}
  .kpi .label {{ font-size: 10px; color: #898781; margin-bottom: 3px; }}
  .kpi .val {{ font-size: 17px; font-weight: 500; }}
  .kpi .sub {{ font-size: 10px; color: #898781; margin-top: 2px; }}
  .kpi.goed .val {{ color: #1baf7a; }}
  .kpi.slecht .val {{ color: #e34948; }}
  .legend {{ display: flex; gap: 10px; font-size: 11px; color: #898781; margin-bottom: 6px; flex-wrap: wrap; }}
  .legend span {{ display: flex; align-items: center; gap: 4px; }}
  .dot {{ width: 8px; height: 8px; border-radius: 2px; flex-shrink: 0; }}
  .chart-box {{ margin-bottom: 1rem; }}
  .tips {{ display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }}
  .tip-card {{ border-radius: 8px; padding: 0.75rem; }}
  .tip-card.goed {{ background: #eaf3de; }}
  .tip-card.slecht {{ background: #fcebeb; }}
  .tip-card h3 {{ font-size: 11px; font-weight: 500; margin-bottom: 6px; }}
  .tip-card.goed h3 {{ color: #3b6d11; }}
  .tip-card.slecht h3 {{ color: #a32d2d; }}
  .tip-row {{ display: flex; justify-content: space-between; font-size: 11px; padding: 3px 0; border-bottom: 0.5px solid rgba(0,0,0,0.07); }}
  .tip-row:last-child {{ border-bottom: none; }}
  .tip-row .uur {{ color: #52514e; }}
  .prijs-goed {{ font-weight: 500; color: #3b6d11; }}
  .prijs-slecht {{ font-weight: 500; color: #a32d2d; }}
  .footer {{ font-size: 10px; color: #898781; text-align: center; margin-top: 1rem; }}
  .geen-data {{ color: #898781; font-size: 13px; padding: 2rem 0; text-align: center; }}
</style>
</head>
<body>
<div class="top-header">
  <h1>⚡ Stroomtip</h1>
  <div class="meta">Bijgewerkt op {gegenereerd_op}</div>
</div>
<div class="grid">
  <div class="dag">
    <div class="dag-header">
      <div><div class="dag-titel">Vandaag</div><div class="dag-datum">{vandaag_str}</div></div>
      {nu_badge}
    </div>
    <div class="kpis">
      <div class="kpi goed"><div class="label">Goedkoopste</div><div class="val">{vd["min_p"]:.2f} c€</div><div class="sub">{vd["min_label"]}</div></div>
      <div class="kpi"><div class="label">Gemiddelde</div><div class="val">{vd["gem"]:.2f} c€</div><div class="sub">incl. btw</div></div>
      <div class="kpi slecht"><div class="label">Duurste</div><div class="val">{vd["max_p"]:.2f} c€</div><div class="sub">{vd["max_label"]}</div></div>
    </div>
    <div class="chart-box">
      <div class="legend">
        <span><span class="dot" style="background:#2a78d6"></span>Nu</span>
        <span><span class="dot" style="background:#1baf7a"></span>Goedkoop</span>
        <span><span class="dot" style="background:#c3c2b7"></span>Normaal</span>
        <span><span class="dot" style="background:#e34948"></span>Duur</span>
        <span><span class="dot" style="background:#f59e0b"></span>Vast ~17.9 c€</span>
      </div>
      <div style="position:relative;height:180px"><canvas id="chart-vd"></canvas></div>
    </div>
    <div class="tips">
      <div class="tip-card goed"><h3>Beste kwartieren</h3>{tip_rijen(vd["top_goed"], "prijs-goed")}</div>
      <div class="tip-card slecht"><h3>Duurste kwartieren</h3>{tip_rijen(vd["top_slecht"], "prijs-slecht")}</div>
    </div>
  </div>
  {morgen_sectie(mo, morgen_str)}
</div>
<div class="footer">Indicatieve eindprijs = (1,04 × EPEX + 0,50) × 1,06 btw · Nettarieven niet inbegrepen · Bron: ENTSO-E</div>
<script>
const REFERENTIE = 17.9;
function maakChart(id, labels, data, kleuren) {{
  new Chart(document.getElementById(id), {{
    type: 'bar',
    data: {{
      labels,
      datasets: [
        {{ data, backgroundColor: kleuren, borderRadius: 2, borderSkipped: 'bottom', order: 2 }},
        {{ type: 'line', data: Array(labels.length).fill(REFERENTIE), borderColor: '#f59e0b', borderWidth: 1.5, borderDash: [4, 3], pointRadius: 0, fill: false, order: 1 }}
      ]
    }},
    options: {{
      responsive: true, maintainAspectRatio: false,
      plugins: {{ legend: {{ display: false }}, tooltip: {{ callbacks: {{ label: ctx => ctx.datasetIndex === 0 ? ctx.parsed.y.toFixed(2) + ' c€/kWh' : 'Vast tarief: 17.9 c€/kWh' }} }} }},
      scales: {{
        x: {{ ticks: {{ maxRotation: 0, maxTicksLimit: 12, color: '#898781', font: {{ size: 9 }} }}, grid: {{ display: false }} }},
        y: {{ ticks: {{ color: '#898781', font: {{ size: 10 }}, callback: v => v.toFixed(0) + ' c€' }}, grid: {{ color: '#e8e7e3' }} }}
      }}
    }}
  }});
}}
maakChart('chart-vd', {labels_vd}, {eind_vd}, {kleur_vd});
{morgen_chart_js(mo)}
</script>
</body>
</html>"""


def main():
    if not ENTSOE_TOKEN:
        raise ValueError("ENTSOE_TOKEN niet gevonden in .env")

    nu     = datetime.now(CET)
    morgen = nu + timedelta(days=1)

    kwartier_min    = (nu.minute // 15) * 15
    huidig_kwartier = nu.strftime(f"%H:{kwartier_min:02d}")

    vandaag_str    = nu.strftime("%-d %B %Y")
    morgen_str     = morgen.strftime("%-d %B %Y")
    gegenereerd_op = nu.strftime("%d/%m/%Y om %H:%M")

    print(f"Prijzen ophalen voor vandaag ({vandaag_str})...")
    kw_vd = fetch_prices(nu)
    print(f"{len(kw_vd)} datapunten ontvangen voor vandaag.")

    print(f"Prijzen ophalen voor morgen ({morgen_str})...")
    kw_mo = fetch_prices(morgen)
    print(f"{len(kw_mo)} datapunten ontvangen voor morgen.")

    vd = bouw_dag_data(kw_vd, huidig_kwartier=huidig_kwartier)
    mo = bouw_dag_data(kw_mo) if kw_mo else {}

    html = genereer_html(vd, mo, vandaag_str, morgen_str, gegenereerd_op, huidig_kwartier)
    OUTPUT_PATH.write_text(html, encoding="utf-8")
    print(f"Dashboard klaar: {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
