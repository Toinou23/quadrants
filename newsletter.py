#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Newsletter hebdomadaire des 4 Quadrants — envoyée chaque vendredi soir.
Contenu 100 % factuel :
  1. Cadran actuel, écarts vs MM 7 ans, tendance sur 3 mois, proximité de bascule.
  2. Variations des 4 actifs du modèle (1 mois / 3 mois).
  3. Titres d'actualité récents liés aux quadrants (Google News RSS, avec liens).
  4. Point backtest des 4 portefeuilles.
Traite aussi les abonnements/désabonnements en attente (IMAP) avant l'envoi.
Sans identifiants email : génère un aperçu docs/newsletter_apercu.html sans envoyer.
"""
import json, os, csv, re, urllib.request, urllib.parse, html as html_mod
from datetime import date
from quadrants import (BASE, DATA, DOCS, process_inbox, send_email, load_subscribers,
                       parse_monthly_adjusted, parse_wti, mail_creds, QUADRANTS)

SEUIL_BASCULE = 5.0  # % : en-dessous, on signale une zone de bascule possible


def rss_titles(query, n=2):
    url = ("https://news.google.com/rss/search?q=" + urllib.parse.quote(query)
           + "&hl=fr&gl=FR&ceid=FR:fr")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read().decode("utf-8", "ignore")
    items = re.findall(r"<item>(.*?)</item>", raw, re.S)
    out = []
    for it in items[:n]:
        t = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", it, re.S)
        l = re.search(r"<link>(.*?)</link>", it, re.S)
        if t and l:
            out.append((html_mod.unescape(t.group(1).strip()), l.group(1).strip()))
    return out


def collect_news():
    themes = [
        ("Inflation", "inflation prix consommation"),
        ("Pétrole", "pétrole WTI prix baril"),
        ("Or", "cours de l'or once"),
        ("Obligations & taux", "taux obligations d'État marché obligataire"),
        ("Actions monde", "marchés actions bourse mondiale"),
    ]
    news = []
    for label, q in themes:
        try:
            for t, l in rss_titles(q, 2):
                news.append((label, t, l))
        except Exception as e:
            print(f"RSS {label}: échec ({e})")
    return news


def pct(a, b):
    return (a / b - 1) * 100


def main():
    process_inbox()

    with open(os.path.join(BASE, "state.json"), encoding="utf-8") as f:
        state = json.load(f)
    rows = list(csv.DictReader(open(os.path.join(DOCS, "history.csv"), encoding="utf-8")))
    last, m3 = rows[-1], rows[-4] if len(rows) > 3 else rows[0]

    acwi = parse_monthly_adjusted(os.path.join(DATA, "acwi_monthly.json"))
    gld = parse_monthly_adjusted(os.path.join(DATA, "gld_monthly.json"))
    tlt = parse_monthly_adjusted(os.path.join(DATA, "tlt_monthly.json"))
    wti = parse_wti(os.path.join(DATA, "wti_monthly.json"))

    def var_series(s):
        ms = sorted(s)
        return pct(s[ms[-1]], s[ms[-2]]), pct(s[ms[-1]], s[ms[-4]] if len(ms) > 3 else s[ms[0]])

    actifs = [("ETF World (ACWI)", *var_series(acwi)), ("Or (GLD)", *var_series(gld)),
              ("Obligations longues (TLT)", *var_series(tlt)), ("Pétrole WTI", *var_series(wti))]

    eg, ei = float(last["ecart_croissance_pct"]), float(last["ecart_inflation_pct"])
    eg3, ei3 = float(m3["ecart_croissance_pct"]), float(m3["ecart_inflation_pct"])
    qmeta = next(q for q in QUADRANTS.values() if q["id"] == state["quadrant_id"])

    alerte_zone = ""
    if abs(eg) < SEUIL_BASCULE or abs(ei) < SEUIL_BASCULE:
        axe = "croissance" if abs(eg) < abs(ei) else "inflation"
        alerte_zone = (f'<p style="padding:10px 14px;background:#fef3c7;border-radius:10px;'
                       f'font-size:14px"><b>⚠️ Zone de bascule possible</b> : l\'écart de l\'axe '
                       f'{axe} est inférieur à {SEUIL_BASCULE:.0f} % — une bascule de cadran '
                       f'peut survenir dans les prochaines semaines.</p>')

    def fleche(now, before):
        return "↗ s'éloigne de la moyenne" if abs(now) > abs(before) else "↘ se rapproche de la moyenne"

    news = collect_news()
    news_html = ""
    if news:
        lis = "".join(
            f'<li style="margin-bottom:8px"><span style="font-size:11px;font-weight:700;'
            f'color:#64748b;text-transform:uppercase">{lab}</span><br>'
            f'<a href="{l}" style="color:#1d4ed8;text-decoration:none">{html_mod.escape(t)}</a></li>'
            for lab, t, l in news)
        news_html = (f'<h3 style="margin-top:26px">📰 Dans l\'actualité des quadrants</h3>'
                     f'<ul style="padding-left:18px;font-size:14px;line-height:1.5">{lis}</ul>'
                     f'<p style="font-size:12px;color:#64748b">Titres sélectionnés automatiquement '
                     f'(Google Actualités) pour leur lien avec les deux ratios — sans commentaire éditorial.</p>')

    bt_html = ""
    try:
        idx = open(os.path.join(DOCS, "index.html"), encoding="utf-8").read()
        payload = json.loads(re.search(r"const DATA = (\{.*?\});\n", idx, re.S).group(1))
        lignes = "".join(
            f'<tr><td style="padding:6px 8px;border:1px solid #e2e8f0">{p["nom"]}</td>'
            f'<td style="padding:6px 8px;border:1px solid #e2e8f0;text-align:right"><b>{p["final"]:,} €</b></td>'
            f'<td style="padding:6px 8px;border:1px solid #e2e8f0;text-align:right">{p["total_pct"]:+.1f} %</td></tr>'.replace(",", " ")
            for p in payload["backtest"]["portfolios"])
        bt_html = (f'<h3 style="margin-top:26px">📊 Les 4 portefeuilles (depuis 2016)</h3>'
                   f'<table style="width:100%;border-collapse:collapse;font-size:14px">{lignes}</table>')
    except Exception as e:
        print(f"Backtest non inclus ({e})")

    lignes_actifs = "".join(
        f'<tr><td style="padding:6px 8px;border:1px solid #e2e8f0">{n}</td>'
        f'<td style="padding:6px 8px;border:1px solid #e2e8f0;text-align:right">{v1:+.1f} %</td>'
        f'<td style="padding:6px 8px;border:1px solid #e2e8f0;text-align:right">{v3:+.1f} %</td></tr>'
        for n, v1, v3 in actifs)

    corps = f"""<div style="font-family:Arial,sans-serif;max-width:560px;margin:0 auto;color:#0f172a">
    <p style="font-size:12px;letter-spacing:2px;text-transform:uppercase;color:#64748b">La news des 4 Quadrants · {date.today().strftime('%d/%m/%Y')}</p>
    <h2 style="color:{qmeta['couleur']};margin:4px 0 2px">{state['quadrant']}</h2>
    <p style="color:#64748b;margin-top:0">cadran inchangé depuis {state['depuis']} · données jusqu'à {state['dernier_mois']}</p>
    {alerte_zone}
    <h3>Les deux boussoles</h3>
    <p style="font-size:14px;line-height:1.7">
    <b>Axe croissance</b> (ETF World / WTI) : <b>{eg:+.1f} %</b> vs moyenne 7 ans — {fleche(eg, eg3)} (il y a 3 mois : {eg3:+.1f} %).<br>
    <b>Axe inflation</b> (Or / Obligations) : <b>{ei:+.1f} %</b> vs moyenne 7 ans — {fleche(ei, ei3)} (il y a 3 mois : {ei3:+.1f} %).</p>
    <h3 style="margin-top:26px">Les 4 actifs du modèle</h3>
    <table style="width:100%;border-collapse:collapse;font-size:14px">
      <tr style="background:#f8fafc"><th style="padding:6px 8px;border:1px solid #e2e8f0;text-align:left">Actif</th>
      <th style="padding:6px 8px;border:1px solid #e2e8f0">1 mois</th><th style="padding:6px 8px;border:1px solid #e2e8f0">3 mois</th></tr>
      {lignes_actifs}
    </table>
    {news_html}
    {bt_html}
    <p style="font-size:14px;margin-top:20px">Tableau de bord complet : ton site GitHub Pages.</p>
    </div>"""

    user, _ = mail_creds()
    if user:
        send_email(f"Les 4 Quadrants — la news du {date.today().strftime('%d/%m')}", corps)
    else:
        os.makedirs(DOCS, exist_ok=True)
        with open(os.path.join(DOCS, "newsletter_apercu.html"), "w", encoding="utf-8") as f:
            f.write(corps)
        print(f"Aperçu généré (docs/newsletter_apercu.html) — {len(load_subscribers())} abonné(s), pas d'envoi.")


if __name__ == "__main__":
    main()
