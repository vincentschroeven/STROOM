#!/usr/bin/env python3
"""
EPEX Belgium Stroomtip — genereert index.html met kwartierprijzen
voor vandaag en morgen op basis van ENTSO-E Day-Ahead data.

Draait dagelijks via GitHub Actions (.github/workflows/update.yml).
"""

import json
import os
import sys
import time
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # in GitHub Actions komt de token uit de environment

# ========================== CONFIGURATIE ==========================
ENTSOE_TOKEN = os.getenv("ENTSOE_TOKEN")

# Energie.be dynamische formule: (1,04 x Belpex + 0,50) c€/kWh excl. btw
FACTOR = 1.04     # vermenigvuldiger op EPEX
OPSLAG = 0.50     # vaste opslag in c€/kWh
BTW    = 1.06     # 6% btw

GRENS_GOEDKOOP = 8.0    # c€/kWh — hieronder kleurt een kwartier groen
GRENS_DUUR     = 17.9   # c€/kWh — hierboven kleurt een kwartier rood
REFERENTIE     = 17.9   # c€/kWh — gele stippellijn (vast tarief)

AANTAL_TIPS = 5         # aantal beste/duurste kwartieren in de lijstjes
RETRIES     = 3         # pogingen bij API-fouten
# ==================================================================

BRUSSEL = ZoneInfo("Europe/Brussels")
AREA    = "10YBE----------2"
API_URL = "https://web-api.tp.entsoe.eu/api"

OUTPUT_PATH = Path(__file__).parent / "index.html"


# ---------------------- DATA OPHALEN ----------------------

def fetch_prices(date: datetime) -> list:
    """Haalt Day-Ahead prijzen op. Geeft [(label 'HH:MM', prijs €/MWh), ...].
    Bij een API-fout: probeert opnieuw. Geeft [] terug als er geen data is."""
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

    laatste_fout = None
    for poging in range(1, RETRIES + 1):
        try:
            resp = requests.get(API_URL, params=params, timeout=30)
            if resp.status_code == 400:
                # ENTSO-E geeft 400 als er (nog) geen data is voor die dag
                return []
            resp.raise_for_status()
            return _parse_xml(resp.text)
        except requests.RequestException as e:
            laatste_fout = e
            print(f"  Poging {poging}/{RETRIES} mislukt: {e}")
            if poging < RETRIES:
                time.sleep(10)

    raise RuntimeError(f"ENTSO-E niet bereikbaar na {RETRIES} pogingen: {laatste_fout}")


def _parse_xml(xml_text: str) -> list:
    """Zet ENTSO-E XML om naar kwartierdata. Uurdata wordt uitgesplitst."""
    root = ET.fromstring(xml_text)
    ns   = {"ns": "urn:iec62325.351:tc57wg16:451-3:publicationdocument:7:3"}

    punten = []
    for ts in root.findall(".//ns:TimeSeries", ns):
        period = ts.find("ns:Period", ns)
        if period is None:
            continue
        resolution = period.find("ns:resolution", ns).text
        start_node = period.find("ns:timeInterval/ns:start", ns)
        if start_node is None:
            continue
        base = datetime.fromisoformat(start_node.text.replace("Z", "+00:00"))

        for point in period.findall("ns:Point", ns):
            pos   = int(point.find("ns:position", ns).text)
            price = float(point.find("ns:price.amount", ns).text)
            if resolution == "PT60M":
                dt = (base + timedelta(hours=pos - 1)).astimezone(BRUSSEL)
                for q in range(4):
                    label = (dt + timedelta(minutes=q * 15)).strftime("%H:%M")
                    punten.append((label, price))
            elif resolution == "PT15M":
                dt = (base + timedelta(minutes=(pos - 1) * 15)).astimezone(BRUSSEL)
                punten.append((dt.strftime("%H:%M"), price))

    # dedupliceren (eerste waarde wint) en sorteren op tijd
    seen = {}
    for label, price in punten:
        seen.setdefault(label, price)
    return sorted(seen.items())


# ---------------------- BEREKENINGEN ----------------------

def epex_naar_eind(epex_mwh: float) -> float:
    """€/MWh (EPEX) -> c€/kWh eindprijs incl. btw volgens Energie.be formule."""
    return round((FACTOR * (epex_mwh / 10) + OPSLAG) * BTW, 2)


def bouw_dag_data(kwartieren: list, huidig_kwartier: str = None) -> dict:
    """Berekent alles wat de HTML nodig heeft voor één dag."""
    if not kwartieren:
        return {}

    labels = [k[0] for k in kwartieren]
    eind   = [epex_naar_eind(k[1]) for k in kwartieren]
    n      = len(eind)

    kleuren = []
    for i in range(n):
        if huidig_kwartier and labels[i] == huidig_kwartier:
            kleuren.append('#2a78d6')      # nu
        elif eind[i] < GRENS_GOEDKOOP:
            kleuren.append('#1baf7a')      # goedkoop
        elif eind[i] >= GRENS_DUUR:
            kleuren.append('#e34948')      # duur
        else:
            kleuren.append('#c3c2b7')      # normaal

    volgorde = sorted(range(n), key=lambda i: eind[i])
    return {
        "labels":     labels,
        "eind":       eind,
        "kleuren":    kleuren,
        "gem":        sum(eind) / n,
        "min_p":      min(eind), "min_label": labels[eind.index(min(eind))],
        "max_p":      max(eind), "max_label": labels[eind.index(max(eind))],
        "top_goed":   [(labels[i], eind[i]) for i in volgorde[:AANTAL_TIPS]],
        "top_slecht": [(labels[i], eind[i]) for i in volgorde[-AANTAL_TIPS:][::-1]],
    }


# ---------------------- HTML ----------------------

def tip_rijen(items, cls):
    return "".join(
        f'<div class="tip-row"><span class="uur">{lbl}</span><span class="{cls}">{p:.2f} c€</span></div>'
        for lbl, p in items
    )


def kpi_blok(d):
    return f'''
    <div class="kpis">
      <div class="kpi goed"><div class="label">Goedkoopste</div><div class="val">{d["min_p"]:.2f} c€</div><div class="sub">{d["min_label"]}</div></div>
      <div class="kpi"><div class="label">Gemiddelde</div><div class="val">{d["gem"]:.2f} c€</div><div class="sub">incl. btw</div></div>
      <div class="kpi slecht"><div class="label">Duurste</div><div class="val">{d["max_p"]:.2f} c€</div><div class="sub">{d["max_label"]}</div></div>
    </div>'''


def legende(met_nu: bool):
    nu = '<span><span class="dot" style="background:#2a78d6"></span>Nu</span>' if met_nu else ''
    return f'''
      <div class="legend">
        {nu}
        <span><span class="dot" style="background:#1baf7a"></span>&lt; {GRENS_GOEDKOOP:g} c€</span>
        <span><span class="dot" style="background:#c3c2b7"></span>Normaal</span>
        <span><span class="dot" style="background:#e34948"></span>&ge; {GRENS_DUUR:g} c€</span>
        <span><span class="dot" style="background:#f59e0b"></span>Vast ~{REFERENTIE:g} c€</span>
      </div>'''


def dag_sectie(d, titel, datum_str, chart_id, nu_badge=""):
    if not d:
        return f'''
  <div class="dag">
    <div class="dag-header">
      <div><div class="dag-titel">{titel}</div><div class="dag-datum">{datum_str}</div></div>
    </div>
    <div class="geen-data">Prijzen voor {titel.lower()} worden verwacht na 13:00.<br>Herlaad de pagina later.</div>
  </div>'''

    return f'''
  <div class="dag">
    <div class="dag-header">
      <div><div class="dag-titel">{titel}</div><div class="dag-datum">{datum_str}</div></div>
      {nu_badge}
    </div>
    {kpi_blok(d)}
    <div class="chart-box">
      {legende(met_nu=bool(nu_badge))}
      <div style="position:relative;height:180px"><canvas id="{chart_id}"></canvas></div>
    </div>
    <div class="tips">
      <div class="tip-card goed"><h3>Beste kwartieren</h3>{tip_rijen(d["top_goed"], "prijs-goed")}</div>
      <div class="tip-card slecht"><h3>Duurste kwartieren</h3>{tip_rijen(d["top_slecht"], "prijs-slecht")}</div>
    </div>
  </div>'''


def chart_js(d, chart_id):
    if not d:
        return ""
    return f"maakChart('{chart_id}', {json.dumps(d['labels'])}, {json.dumps(d['eind'])}, {json.dumps(d['kleuren'])});"


def genereer_html(vd, mo, vandaag_str, morgen_str, gegenereerd_op, huidig_kwartier):
    nu_badge = ""
    if vd and huidig_kwartier in vd["labels"]:
        prijs_nu = vd["eind"][vd["labels"].index(huidig_kwartier)]
        nu_badge = f'<div class="nu-badge">Nu ({huidig_kwartier}) &nbsp;{prijs_nu:.2f} c€/kWh</div>'

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
  {dag_sectie(vd, "Vandaag", vandaag_str, "chart-vd", nu_badge)}
  {dag_sectie(mo, "Morgen", morgen_str, "chart-mo")}
</div>
<div class="footer">Eindprijs = ({FACTOR:g} × EPEX + {OPSLAG:g}) × {BTW:g} btw · Nettarieven niet inbegrepen · Bron: ENTSO-E</div>
<script>
const REFERENTIE = {REFERENTIE};
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
      plugins: {{
        legend: {{ display: false }},
        tooltip: {{ callbacks: {{ label: ctx => ctx.datasetIndex === 0 ? ctx.parsed.y.toFixed(2) + ' c€/kWh' : 'Vast tarief: ' + REFERENTIE + ' c€/kWh' }} }}
      }},
      scales: {{
        x: {{ ticks: {{ maxRotation: 0, maxTicksLimit: 12, color: '#898781', font: {{ size: 9 }} }}, grid: {{ display: false }} }},
        y: {{ ticks: {{ color: '#898781', font: {{ size: 10 }}, callback: v => v.toFixed(0) + ' c€' }}, grid: {{ color: '#e8e7e3' }} }}
      }}
    }}
  }});
}}
{chart_js(vd, "chart-vd")}
{chart_js(mo, "chart-mo")}
</script>
</body>
</html>"""


# ---------------------- MAIN ----------------------

def main():
    if not ENTSOE_TOKEN:
        sys.exit("FOUT: ENTSOE_TOKEN niet gevonden (in .env of environment).")

    nu     = datetime.now(BRUSSEL)
    morgen = nu + timedelta(days=1)

    kwartier_min    = (nu.minute // 15) * 15
    huidig_kwartier = nu.strftime(f"%H:{kwartier_min:02d}")

    print(f"Prijzen ophalen voor vandaag ({nu:%d/%m})...")
    kw_vd = fetch_prices(nu)
    print(f"  {len(kw_vd)} datapunten.")

    print(f"Prijzen ophalen voor morgen ({morgen:%d/%m})...")
    kw_mo = fetch_prices(morgen)
    print(f"  {len(kw_mo)} datapunten.")

    if not kw_vd and not kw_mo:
        # Geen enkele data: laat de bestaande pagina staan en stop netjes
        sys.exit("Geen data ontvangen — bestaande index.html blijft ongewijzigd.")

    vd = bouw_dag_data(kw_vd, huidig_kwartier)
    mo = bouw_dag_data(kw_mo)

    html = genereer_html(
        vd, mo,
        nu.strftime("%-d %B %Y"),
        morgen.strftime("%-d %B %Y"),
        nu.strftime("%d/%m/%Y om %H:%M"),
        huidig_kwartier,
    )
    OUTPUT_PATH.write_text(html, encoding="utf-8")
    print(f"Dashboard klaar: {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
