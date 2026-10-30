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

GRENS_GOEDKOOP = 12.0   # c€/kWh — hieronder kleurt een kwartier groen
GRENS_MATIG    = 22.5   # c€/kWh — tussen GRENS_GOEDKOOP en dit kleurt een kwartier donkergroen
GRENS_DUUR     = 27.0   # c€/kWh — hierboven kleurt een kwartier rood
REFERENTIE     = 21.3   # c€/kWh — gele stippellijn: gemiddeld vast contract (Test-Aankoop, okt 2026, energie incl. btw)
REFERENTIE_LABEL = "Gem. vast contract"

# ---- Gezinsblok: toestellen die kunnen wachten ----
# kwh = verbruik per beurt, duur = minuten, max_wacht = hoe lang wachten nog aangenaam is (uren)
TOESTELLEN = [
    {"naam": "Droogkast",     "icoon": "🌀", "kwh": 3.0, "duur": 120, "max_wacht": 12},
    {"naam": "Afwasmachine",  "icoon": "🍽️", "kwh": 1.0, "duur": 150, "max_wacht": 12},
    {"naam": "Wasmachine",    "icoon": "👕", "kwh": 0.7, "duur": 120, "max_wacht": 12},
    {"naam": "Strijkijzer",   "icoon": "👔", "kwh": 1.0, "duur": 60,  "max_wacht": 12},
    {"naam": "Droogoven",     "icoon": "🍄", "kwh": 2.4, "duur": 240, "max_wacht": 24},
]
GEWOON_DOEN = ["Waterkoker", "Airfryer", "Koken & oven voor het eten"]
WACHT_DREMPEL = 0.10    # € — pas vanaf deze besparing per beurt zeggen we "wacht"
VENSTER_UUR   = 2       # lengte van het "beste moment"-blok in uren

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
            resp = requests.get(API_URL, params=params, timeout=45)
            if resp.status_code == 400:
                # ENTSO-E geeft 400 als er (nog) geen data is voor die dag
                return []
            resp.raise_for_status()
            return _parse_xml(resp.text)
        except requests.RequestException as e:
            laatste_fout = e
            print(f"  Poging {poging}/{RETRIES} mislukt: {e}")
            if poging < RETRIES:
                time.sleep(15 * poging)
                
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
        elif eind[i] < GRENS_MATIG:
            kleuren.append('#146c43')      # matig goedkoop
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
        <span><span class="dot" style="background:#146c43"></span>{GRENS_GOEDKOOP:g}–{GRENS_MATIG:g} c€</span>
        <span><span class="dot" style="background:#c3c2b7"></span>Normaal</span>
        <span><span class="dot" style="background:#e34948"></span>&ge; {GRENS_DUUR:g} c€</span>
        <span><span class="dot" style="background:#f59e0b"></span>{REFERENTIE_LABEL} ~{REFERENTIE:g} c€</span>
      </div>'''


def dag_sectie(d, titel, datum_str, datum_iso, chart_id, nu_badge=""):
    if not d:
        return f'''
  <div class="dag" data-datum="{datum_iso}">
    <div class="dag-header">
      <div><div class="dag-titel">{titel}</div><div class="dag-datum">{datum_str}</div></div>
    </div>
    <div class="geen-data">Prijzen voor {titel.lower()} worden verwacht na 13:00.<br>Herlaad de pagina later.</div>
  </div>'''

    return f'''
  <div class="dag" data-datum="{datum_iso}">
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
    return f"try {{ maakChart('{chart_id}', {json.dumps(d['labels'])}, {json.dumps(d['eind'])}, {json.dumps(d['kleuren'])}); }} catch (e) {{ console.warn(e); }}"


def gezin_html():
    gewoon = "".join(f"<span>{g}</span>" for g in GEWOON_DOEN)
    return f'''
<div class="gezin">
  <div class="kaart" id="gezin-status"></div>
  <div class="kaart">
    <h2>Wat doe ik met…</h2>
    <div id="gezin-toestellen"></div>
    <div class="subtitel">Gewoon doen, nooit wachten</div>
    <div class="gewoon">{gewoon}</div>
    <div class="regels">
      <div class="regel"><b>Niet tegelijk:</b> start zware toestellen een kwartier na elkaar.</div>
      <div class="regel"><b>Infrarood:</b> alleen aan waar je zit. Uitstellen helpt niet, uitzetten wel.</div>
    </div>
  </div>
</div>'''


def genereer_html(vd, mo, vandaag_str, morgen_str, vandaag_iso, morgen_iso, gegenereerd_op, huidig_kwartier):
    dag_data = []
    if vd:
        dag_data.append({"datum": vandaag_iso, "labels": vd["labels"], "eind": vd["eind"]})
    if mo:
        dag_data.append({"datum": morgen_iso, "labels": mo["labels"], "eind": mo["eind"]})
    nu_badge = ""
    if vd and huidig_kwartier in vd["labels"]:
        prijs_nu = vd["eind"][vd["labels"].index(huidig_kwartier)]
        nu_badge = f'<div class="nu-badge">Nu ({huidig_kwartier}) &nbsp;{prijs_nu:.2f} c€/kWh</div>'

    return f"""<!DOCTYPE html>
<html lang="nl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Stroomtip</title>
<link rel="manifest" href="manifest.webmanifest">
<meta name="theme-color" content="#f5f4f0">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Stroomtip">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<link rel="apple-touch-icon" href="icons/apple-touch-icon.png">
<link rel="icon" type="image/png" sizes="192x192" href="icons/icon-192.png">
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.js"></script>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #f5f4f0; color: #0b0b0b; padding: max(1.25rem, env(safe-area-inset-top)) 1.25rem max(1.25rem, env(safe-area-inset-bottom)); max-width: 960px; margin: 0 auto; line-height: 1.5; }}
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

  /* ---- gezinsblok ---- */
  .gezin {{ display: grid; grid-template-columns: 1fr 1fr; gap: 1.25rem; margin-bottom: 1.25rem; }}
  @media (max-width: 640px) {{ .gezin {{ grid-template-columns: 1fr; }} }}
  .kaart {{ background: #fff; border-radius: 12px; padding: 1.25rem; border: 0.5px solid rgba(11,11,11,0.10); }}
  .status {{ display: flex; gap: 14px; align-items: center; }}
  .lamp {{ width: 52px; height: 52px; border-radius: 50%; flex-shrink: 0; background: #c3c2b7; box-shadow: 0 0 0 6px rgba(195,194,183,0.25); }}
  .status.goedkoop .lamp {{ background: #1baf7a; box-shadow: 0 0 0 6px rgba(27,175,122,0.18); }}
  .status.normaal .lamp {{ background: #f59e0b; box-shadow: 0 0 0 6px rgba(245,158,11,0.18); }}
  .status.duur .lamp {{ background: #e34948; box-shadow: 0 0 0 6px rgba(227,73,72,0.18); }}
  .status-titel {{ font-size: 22px; font-weight: 600; line-height: 1.2; }}
  .status-sub {{ font-size: 13px; color: #52514e; margin-top: 4px; }}
  .venster {{ margin-top: 1.1rem; background: #eaf3de; border-radius: 10px; padding: 0.8rem 1rem; }}
  .venster .label {{ font-size: 11px; color: #3b6d11; font-weight: 500; text-transform: uppercase; letter-spacing: .04em; }}
  .venster .tijd {{ font-size: 20px; font-weight: 600; color: #1d4a06; margin-top: 2px; }}
  .venster .sub {{ font-size: 12px; color: #3b6d11; }}
  .kaart h2 {{ font-size: 15px; font-weight: 600; margin-bottom: 0.75rem; }}
  .toestel {{ display: flex; align-items: center; gap: 12px; padding: 10px 0; border-bottom: 0.5px solid rgba(0,0,0,0.08); }}
  .toestel:last-of-type {{ border-bottom: none; }}
  .toestel .ico {{ font-size: 22px; width: 32px; text-align: center; }}
  .toestel .naam {{ font-size: 14px; font-weight: 500; }}
  .toestel .uitleg {{ font-size: 12px; color: #52514e; }}
  .toestel .wat {{ margin-left: auto; text-align: right; flex-shrink: 0; }}
  .chip {{ display: inline-block; font-size: 12px; font-weight: 600; padding: 4px 10px; border-radius: 999px; white-space: nowrap; }}
  .chip.wacht {{ background: #eaf3de; color: #1d4a06; }}
  .chip.start {{ background: #f0efea; color: #52514e; }}
  .chip.nu {{ background: #e6f1fb; color: #185fa5; }}
  .gewoon {{ display: flex; flex-wrap: wrap; gap: 6px; margin-top: 4px; }}
  .gewoon span {{ font-size: 12px; background: #f0efea; border-radius: 999px; padding: 4px 10px; }}
  .regels {{ margin-top: 1rem; display: grid; gap: 8px; }}
  .regel {{ font-size: 12px; color: #52514e; background: #f5f4f0; border-radius: 8px; padding: 8px 10px; }}
  .regel b {{ color: #0b0b0b; font-weight: 600; }}
  .subtitel {{ font-size: 13px; font-weight: 600; margin: 1rem 0 0.25rem; color: #52514e; }}
  .details-kop {{ font-size: 13px; font-weight: 600; color: #52514e; margin: 0.5rem 0 0.75rem; }}
</style>
</head>
<body>
<div class="top-header">
  <h1>⚡ Stroomtip</h1>
  <div class="meta">Bijgewerkt op {gegenereerd_op}</div>
</div>
{gezin_html()}
<div class="details-kop">Prijzen per kwartier</div>
<div class="grid">
  {dag_sectie(vd, "Vandaag", vandaag_str, vandaag_iso, "chart-vd", nu_badge)}
  {dag_sectie(mo, "Morgen", morgen_str, morgen_iso, "chart-mo")}
</div>
<div class="footer">Eindprijs = ({FACTOR:g} × EPEX + {OPSLAG:g}) × {BTW:g} btw · Nettarieven niet inbegrepen · Bron: ENTSO-E</div>
<script>
const REFERENTIE = {REFERENTIE};
const GRENS_GOEDKOOP = {GRENS_GOEDKOOP};
const GRENS_MATIG = {GRENS_MATIG};
const GRENS_DUUR = {GRENS_DUUR};
const CHARTS = {{}};
const DATA = {json.dumps(dag_data)};
const TOESTELLEN = {json.dumps(TOESTELLEN, ensure_ascii=False)};
const WACHT_DREMPEL = {WACHT_DREMPEL};
const VENSTER_UUR = {VENSTER_UUR};
function maakChart(id, labels, data, kleuren) {{
  CHARTS[id] = new Chart(document.getElementById(id), {{
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
        tooltip: {{ callbacks: {{ label: ctx => ctx.datasetIndex === 0 ? ctx.parsed.y.toFixed(2) + ' c€/kWh' : '{REFERENTIE_LABEL}: ' + REFERENTIE + ' c€/kWh' }} }}
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

(function() {{
  const pad = n => String(n).padStart(2, '0');
  const isoVan = dt => `${{dt.getFullYear()}}-${{pad(dt.getMonth() + 1)}}-${{pad(dt.getDate())}}`;
  const kleurVoor = prijs => {{
    if (prijs < GRENS_GOEDKOOP) return '#1baf7a';
    if (prijs < GRENS_MATIG) return '#146c43';
    if (prijs >= GRENS_DUUR) return '#e34948';
    return '#c3c2b7';
  }};

  const nuDt = new Date();
  const vandaagISO = isoVan(nuDt);
  const morgenDt = new Date();
  morgenDt.setDate(morgenDt.getDate() + 1);
  const morgenISO = isoVan(morgenDt);
  const huidigKwartier = `${{pad(nuDt.getHours())}}:${{pad(Math.floor(nuDt.getMinutes() / 15) * 15)}}`;

  const grid = document.querySelector('.grid');
  const secties = Array.from(grid.querySelectorAll('.dag'));
  const zichtbaar = [];

  secties.forEach(el => {{
    const datum = el.dataset.datum;
    if (datum < vandaagISO) {{
      el.remove();
      return;
    }}
    const titelEl = el.querySelector('.dag-titel');
    if (datum === vandaagISO) titelEl.textContent = 'Vandaag';
    else if (datum === morgenISO) titelEl.textContent = 'Morgen';

    const canvas = el.querySelector('canvas');
    const chart = canvas && CHARTS[canvas.id];
    if (chart) {{
      const labels = chart.data.labels;
      const data = chart.data.datasets[0].data;
      const idx = datum === vandaagISO ? labels.indexOf(huidigKwartier) : -1;
      chart.data.datasets[0].backgroundColor = data.map((prijs, i) => i === idx ? '#2a78d6' : kleurVoor(prijs));
      chart.update('none');

      let badge = el.querySelector('.nu-badge');
      if (idx !== -1) {{
        if (!badge) {{
          badge = document.createElement('div');
          badge.className = 'nu-badge';
          el.querySelector('.dag-header').appendChild(badge);
        }}
        badge.innerHTML = `Nu (${{huidigKwartier}}) &nbsp;${{data[idx].toFixed(2)}} c€/kWh`;
      }} else if (badge) {{
        badge.remove();
      }}
    }}

    zichtbaar.push(el);
  }});

  zichtbaar.sort((a, b) => a.dataset.datum.localeCompare(b.dataset.datum));
  zichtbaar.forEach(el => grid.appendChild(el));

  // ---------- gezinsblok ----------
  const euro = v => '€' + v.toFixed(2).replace('.', ',');
  const cent = v => v.toFixed(1).replace('.', ',') + ' c€';
  const dagWoord = iso => iso === vandaagISO ? 'vandaag' : (iso === morgenISO ? 'morgen' : iso);

  // reeks vanaf het huidige kwartier: vandaag (rest) + morgen
  const reeks = [];
  const vd = DATA.find(d => d.datum === vandaagISO);
  const mo = DATA.find(d => d.datum === morgenISO);
  if (vd) vd.labels.forEach((l, i) => {{ if (l >= huidigKwartier) reeks.push({{ dag: vd.datum, label: l, prijs: vd.eind[i] }}); }});
  if (mo) mo.labels.forEach((l, i) => reeks.push({{ dag: mo.datum, label: l, prijs: mo.eind[i] }}));

  const eindLabel = (r, n) => {{
    const [h, m] = r.label.split(':').map(Number);
    const t = h * 60 + m + n * 15;
    return `${{pad(Math.floor(t / 60) % 24)}}:${{pad(t % 60)}}`;
  }};

  // beste aaneengesloten blok binnen de komende 'maxQ' kwartieren
  const besteStart = (n, maxQ) => {{
    let best = null;
    for (let s = 0; s + n <= reeks.length && s <= maxQ; s++) {{
      let som = 0;
      for (let j = s; j < s + n; j++) som += reeks[j].prijs;
      const gem = som / n;
      if (!best || gem < best.gem - 1e-9) best = {{ s, gem }};
    }}
    return best;
  }};

  const statusEl = document.getElementById('gezin-status');
  const toestelEl = document.getElementById('gezin-toestellen');
  if (vd && reeks.length && reeks[0].dag === vandaagISO) {{
    const nuPrijs = reeks[0].prijs;
    const gesorteerd = [...vd.eind].sort((a, b) => a - b);
    const rang = gesorteerd.filter(p => p < nuPrijs).length / gesorteerd.length;
    const stand = rang < 1/3 ? 'goedkoop' : (rang >= 2/3 ? 'duur' : 'normaal');
    const titels = {{ goedkoop: 'Nu is stroom goedkoop', normaal: 'Nu is stroom gewoon', duur: 'Nu is stroom duur' }};
    const subs = {{ goedkoop: 'Goed moment voor de zware toestellen.', normaal: 'Kan, maar straks is het goedkoper.', duur: 'Zware toestellen beter even laten wachten.' }};

    const venQ = VENSTER_UUR * 4;
    const ven = besteStart(Math.min(venQ, reeks.length), 96);
    let venHtml = '';
    if (ven) {{
      const r = reeks[ven.s];
      const nuTekst = ven.s === 0 ? 'nu' : `${{dagWoord(r.dag)}} ${{r.label}}`;
      venHtml = `<div class="venster"><div class="label">Beste moment</div>
        <div class="tijd">${{ven.s === 0 ? 'Nu' : dagWoord(r.dag).replace(/^./, c => c.toUpperCase()) + ' ' + r.label}} – ${{eindLabel(r, venQ)}}</div>
        <div class="sub">gemiddeld ${{cent(ven.gem)}}/kWh</div></div>`;
    }}

    const wachters = [];
    toestelEl.innerHTML = TOESTELLEN.map(t => {{
      const n = Math.max(1, Math.round(t.duur / 15));
      if (reeks.length < n) return '';
      let som = 0;
      for (let j = 0; j < n; j++) som += reeks[j].prijs;
      const kostNu = t.kwh * (som / n) / 100;
      const b = besteStart(n, t.max_wacht * 4);
      const kostBest = t.kwh * b.gem / 100;
      const winst = kostNu - kostBest;
      let wat, uitleg;
      if (b.s === 0) {{
        wat = `<span class="chip nu">Start nu</span>`;
        uitleg = `Nu is het goedkoopst · ${{euro(kostNu)}} per beurt`;
      }} else if (winst >= WACHT_DREMPEL) {{
        wachters.push(t.naam.toLowerCase());
        const r = reeks[b.s];
        wat = `<span class="chip wacht">Wacht tot ${{r.dag === vandaagISO ? '' : 'morgen '}}${{r.label}}</span>`;
        uitleg = `Nu ${{euro(kostNu)}} → dan ${{euro(kostBest)}} · bespaart ${{euro(winst)}}`;
      }} else {{
        wat = `<span class="chip start">Start maar</span>`;
        uitleg = `Wachten scheelt maar ${{euro(Math.max(winst, 0))}}`;
      }}
      return `<div class="toestel"><div class="ico">${{t.icoon}}</div>
        <div><div class="naam">${{t.naam}}</div><div class="uitleg">${{uitleg}}</div></div>
        <div class="wat">${{wat}}</div></div>`;
    }}).join('');
    const lijst = wachters.length > 1 ? wachters.slice(0, -1).join(', ') + ' en ' + wachters.at(-1) : wachters[0];
    const advies = wachters.length
      ? `Laat de ${{lijst}} even wachten.`
      : (stand === 'goedkoop' ? subs.goedkoop : 'Wachten levert nu nergens echt iets op.');
    statusEl.innerHTML = `<div class="status ${{stand}}"><div class="lamp"></div>
      <div><div class="status-titel">${{titels[stand]}}</div>
      <div class="status-sub">${{cent(nuPrijs)}}/kWh · ${{advies}}</div></div></div>${{venHtml}}`;


  }} else {{
    document.querySelector('.gezin').style.display = 'none';
  }}

  if (!zichtbaar.some(el => el.dataset.datum === vandaagISO)) {{
    const msg = document.createElement('div');
    msg.className = 'geen-data';
    msg.style.gridColumn = '1 / -1';
    msg.textContent = 'Gegevens voor vandaag worden binnenkort bijgewerkt.';
    grid.appendChild(msg);
  }}
}})();
</script>
<script>
  if ('serviceWorker' in navigator) {{
    navigator.serviceWorker.register('sw.js').catch(() => {{}});
  }}
  // Als de app na een tijdje terug geopend wordt: vers laden, zodat advies en prijzen kloppen.
  const GELADEN = Date.now();
  document.addEventListener('visibilitychange', () => {{
    if (document.visibilityState === 'visible' && Date.now() - GELADEN > 10 * 60 * 1000) location.reload();
  }});
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
        nu.strftime("%Y-%m-%d"),
        morgen.strftime("%Y-%m-%d"),
        nu.strftime("%d/%m/%Y om %H:%M"),
        huidig_kwartier,
    )
    OUTPUT_PATH.write_text(html, encoding="utf-8")
    print(f"Dashboard klaar: {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
