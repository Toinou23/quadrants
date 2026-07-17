#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Les 4 Quadrants de Charles Gave — version cloud (GitHub Actions)
================================================================
Chaque soir :
1. Télécharge ACWI, GLD, TLT (mensuel ajusté) et WTI (mensuel) chez Alpha Vantage.
2. Calcule les deux ratios et leur moyenne mobile 7 ans (84 mois) → quadrant.
3. Backteste 4 portefeuilles depuis 2016 (dont la stratégie de Gave).
4. Régénère le site (docs/index.html) + history.csv + state.json.
5. Envoie un EMAIL si changement de quadrant (si secrets configurés).

Secrets attendus (variables d'environnement) :
  ALPHAVANTAGE_KEY    (obligatoire)
  MAIL_USER           (optionnel — adresse Gmail expéditrice)
  MAIL_APP_PASSWORD   (optionnel — mot de passe d'application Gmail)
  MAIL_TO             (optionnel — destinataire ; par défaut MAIL_USER)

Règle du portefeuille « Gave » (P2), déduite du schéma des 4 cadrans :
chaque cadran a son actif roi (CI→or, CD→actions, RD→obligations, RI→cash) ;
l'actif à RETIRER est l'actif roi du cadran diagonalement opposé.
À chaque croisement ratio/MM7a qui change le cadran, l'actif exclu est vendu
et son produit réparti à parts égales entre les trois autres.
"""
import json, os, sys, csv, urllib.request, urllib.parse
from datetime import date

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
DOCS = os.path.join(BASE, "docs")
MA_MONTHS = 84
BACKTEST_START = "2016-01"

QUADRANTS = {
    ("croissance", "inflation"): {
        "id": "boom_inflationniste", "nom": "Croissance inflationniste", "couleur": "#f59e0b",
        "actif_roi": "Or, valeurs de rareté", "exclu": "obligations",
        "allocations": "Or, valeurs de rareté, matières premières, immobilier. À retirer : obligations longues.",
    },
    ("croissance", "deflation"): {
        "id": "boom_deflationniste", "nom": "Croissance déflationniste", "couleur": "#10b981",
        "actif_roi": "Actions (valeurs d'efficacité)", "exclu": "cash",
        "allocations": "Actions de croissance, obligations longues. À retirer : cash.",
    },
    ("recession", "inflation"): {
        "id": "recession_inflationniste", "nom": "Récession inflationniste", "couleur": "#ef4444",
        "actif_roi": "Cash dans la meilleure monnaie", "exclu": "actions",
        "allocations": "Cash, or. À retirer : actions.",
    },
    ("recession", "deflation"): {
        "id": "depression_deflationniste", "nom": "Récession déflationniste", "couleur": "#3b82f6",
        "actif_roi": "Obligations d'État", "exclu": "or",
        "allocations": "Obligations d'État longues, cash. À retirer : or.",
    },
}


# ---------------------------------------------------------------- téléchargement
def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "quadrants-gave/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def refresh_data(key):
    os.makedirs(DATA, exist_ok=True)
    jobs = [
        ("acwi_monthly.json", f"https://www.alphavantage.co/query?function=TIME_SERIES_MONTHLY_ADJUSTED&symbol=ACWI&apikey={key}"),
        ("gld_monthly.json",  f"https://www.alphavantage.co/query?function=TIME_SERIES_MONTHLY_ADJUSTED&symbol=GLD&apikey={key}"),
        ("tlt_monthly.json",  f"https://www.alphavantage.co/query?function=TIME_SERIES_MONTHLY_ADJUSTED&symbol=TLT&apikey={key}"),
        ("wti_monthly.json",  f"https://www.alphavantage.co/query?function=WTI&interval=monthly&apikey={key}"),
    ]
    for fname, url in jobs:
        path = os.path.join(DATA, fname)
        try:
            j = fetch_json(url)
            ok = ("Monthly Adjusted Time Series" in j) or ("data" in j and len(j.get("data", [])) > 50)
            if ok:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(j, f)
                print(f"OK  {fname}")
            else:
                print(f"KO  {fname} (réponse API inattendue) — fichier existant conservé")
        except Exception as e:
            print(f"KO  {fname} ({e}) — fichier existant conservé")


# ---------------------------------------------------------------- parseurs
def parse_monthly_adjusted(path):
    with open(path, encoding="utf-8") as f:
        j = json.load(f)
    key = next(k for k in j if "Monthly" in k and "Time Series" in k)
    out = {}
    for d, row in j[key].items():
        v = row.get("5. adjusted close") or row.get("4. close")
        out[d[:7]] = float(v)
    return out


def parse_wti(path):
    with open(path, encoding="utf-8") as f:
        j = json.load(f)
    out = {}
    for row in j.get("data", []):
        try:
            out[row["date"][:7]] = float(row["value"])
        except (ValueError, KeyError):
            continue
    return out


def trailing_ma(values, n):
    out, s = [], 0.0
    for i, v in enumerate(values):
        s += v
        if i >= n:
            s -= values[i - n]
        out.append(s / n if i >= n - 1 else None)
    return out


# ---------------------------------------------------------------- abonnés & email
SUBS_PATH = os.path.join(BASE, "subscribers.json")
SUBJECT_SUB = "ABONNEMENT-QUADRANTS"
SUBJECT_UNSUB = "STOP-QUADRANTS"


def mail_creds():
    user = os.environ.get("MAIL_USER", "").strip()
    pwd = os.environ.get("MAIL_APP_PASSWORD", "").strip()
    return (user, pwd) if user and pwd else (None, None)


def load_subscribers():
    if os.path.exists(SUBS_PATH):
        with open(SUBS_PATH, encoding="utf-8") as f:
            return json.load(f).get("abonnes", [])
    return []


def save_subscribers(subs):
    with open(SUBS_PATH, "w", encoding="utf-8") as f:
        json.dump({"abonnes": sorted(set(subs))}, f, ensure_ascii=False, indent=2)


def process_inbox():
    """Lit la boîte Gmail (IMAP) : traite les demandes d'abonnement / désabonnement
    envoyées depuis le site ou depuis le lien de désinscription des emails."""
    user, pwd = mail_creds()
    if not user:
        print("IMAP: identifiants absents — traitement des abonnements sauté.")
        return
    import imaplib, email as email_mod, re as re_mod
    from email.utils import parseaddr
    subs = [s.lower() for s in load_subscribers()]
    changed = False
    try:
        box = imaplib.IMAP4_SSL("imap.gmail.com", timeout=30)
        box.login(user, pwd)
        box.select("INBOX")
        for token, action in ((SUBJECT_SUB, "add"), (SUBJECT_UNSUB, "remove")):
            ok, ids = box.search(None, "UNSEEN", f'SUBJECT "{token}"')
            if ok != "OK":
                continue
            for num in ids[0].split():
                ok, data = box.fetch(num, "(RFC822)")
                if ok != "OK":
                    continue
                msg = email_mod.message_from_bytes(data[0][1])
                sender = parseaddr(msg.get("From", ""))[1].lower()
                body = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() == "text/plain":
                            body = part.get_payload(decode=True).decode("utf-8", "ignore")
                            break
                else:
                    body = msg.get_payload(decode=True).decode("utf-8", "ignore")
                found = re_mod.findall(r"[\w.+-]+@[\w-]+\.[\w.-]+", body)
                target = (found[0].lower() if found else sender)
                if not target:
                    continue
                if action == "add" and target not in subs:
                    subs.append(target)
                    changed = True
                    print(f"IMAP: abonné ajouté : {target}")
                elif action == "remove" and target in subs:
                    subs.remove(target)
                    changed = True
                    print(f"IMAP: abonné retiré : {target}")
                box.store(num, "+FLAGS", "\\Seen")
        box.logout()
    except Exception as e:
        print(f"IMAP: erreur ({e}) — abonnements inchangés.")
    if changed:
        save_subscribers(subs)


def unsubscribe_footer(user):
    return (
        f'<hr style="border:none;border-top:1px solid #e2e8f0;margin:24px 0 12px">'
        f'<p style="font-size:12px;color:#64748b;line-height:1.5">'
        f'Cadre théorique de Charles Gave — ne constitue pas un conseil en investissement personnalisé.<br>'
        f'<a href="mailto:{user}?subject={SUBJECT_UNSUB}&body=Merci%20de%20me%20d%C3%A9sabonner." '
        f'style="display:inline-block;margin-top:8px;padding:8px 16px;background:#f1f5f9;'
        f'color:#334155;text-decoration:none;border-radius:8px;font-weight:600">Se désinscrire</a></p>'
    )


def send_email(subject, html, recipients=None):
    """Envoie en copie cachée à tous les abonnés (ou à la liste fournie)."""
    user, pwd = mail_creds()
    if not user:
        print("Email non configuré (MAIL_USER / MAIL_APP_PASSWORD absents) — pas d'envoi.")
        return False
    recipients = recipients if recipients is not None else load_subscribers()
    if not recipients:
        print("Aucun abonné — pas d'envoi.")
        return False
    import smtplib
    from email.mime.text import MIMEText
    msg = MIMEText(html + unsubscribe_footer(user), "html", "utf-8")
    msg["Subject"] = subject
    msg["From"] = f"Les 4 Quadrants <{user}>"
    msg["To"] = user
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
            s.login(user, pwd)
            s.sendmail(user, [user] + list(recipients), msg.as_string())
        print(f"Email « {subject} » envoyé à {len(recipients)} abonné(s).")
        return True
    except Exception as e:
        print(f"Échec envoi email : {e}")
        return False


# ---------------------------------------------------------------- backtest
def max_drawdown(series):
    peak, mdd = series[0], 0.0
    for v in series:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    return mdd * 100


def backtest(months, prices, quad_by_month):
    """4 portefeuilles, départ BACKTEST_START, 4 x 1000 €. Prix mensuels ; cash = 1."""
    bt_months = [m for m in months if m >= BACKTEST_START]
    assets = ["actions", "or", "obligations", "cash"]

    def value(holdings, m):
        return sum(holdings[a] * prices[a][m] for a in assets)

    # P1 : 4 x 1000, aucun changement
    m0 = bt_months[0]
    h1 = {a: 1000 / prices[a][m0] for a in assets}
    p1 = [value(h1, m) for m in bt_months]

    # P2 : règles de Gave — exclusion de l'actif du cadran opposé à chaque bascule
    h2 = {a: 1000 / prices[a][m0] for a in assets}
    p2, prev_q, nb_arbitrages = [], None, 0
    for m in bt_months:
        q = quad_by_month[m]
        if q != prev_q:
            exclu = next(x["exclu"] for x in QUADRANTS.values() if x["id"] == q)
            proceeds = h2[exclu] * prices[exclu][m]
            if proceeds > 0:
                h2[exclu] = 0.0
                others = [a for a in assets if a != exclu]
                for a in others:
                    h2[a] += (proceeds / 3) / prices[a][m]
                nb_arbitrages += 1
            prev_q = q
        p2.append(value(h2, m))

    # P3 : 4000 € d'or ; P4 : 4000 € d'ETF world
    p3 = [4000 / prices["or"][m0] * prices["or"][m] for m in bt_months]
    p4 = [4000 / prices["actions"][m0] * prices["actions"][m] for m in bt_months]

    def stats(serie):
        n = len(serie) - 1
        cagr = ((serie[-1] / serie[0]) ** (12 / n) - 1) * 100 if n > 0 else 0
        return {
            "final": round(serie[-1]), "total_pct": round((serie[-1] / serie[0] - 1) * 100, 1),
            "cagr_pct": round(cagr, 2), "maxdd_pct": round(max_drawdown(serie), 1),
        }

    defs = [
        ("p1", "P1 · 4 × 1000 € figés", "Répartition initiale conservée, aucun arbitrage.", "#94a3b8", p1),
        ("p2", "P2 · Règles de Gave", "À chaque bascule de cadran, l'actif du cadran opposé est vendu et réparti sur les trois autres.", "#f59e0b", p2),
        ("p3", "P3 · 100 % or", "4000 € en or, aucun arbitrage.", "#eab308", p3),
        ("p4", "P4 · 100 % ETF World", "4000 € en ACWI, aucun arbitrage.", "#10b981", p4),
    ]
    portfolios = []
    for pid, nom, desc, coul, serie in defs:
        p = {"id": pid, "nom": nom, "desc": desc, "couleur": coul,
             "serie": [round(v, 1) for v in serie]}
        p.update(stats(serie))
        if pid == "p2":
            p["nb_arbitrages"] = nb_arbitrages
        portfolios.append(p)
    return {"labels": bt_months, "portfolios": portfolios}


# ---------------------------------------------------------------- principal
def main():
    key = os.environ.get("ALPHAVANTAGE_KEY", "").strip()
    if key:
        refresh_data(key)
    else:
        print("ALPHAVANTAGE_KEY absent — calcul sur les données déjà présentes.")

    process_inbox()  # abonnements / désabonnements en attente

    acwi = parse_monthly_adjusted(os.path.join(DATA, "acwi_monthly.json"))
    gld = parse_monthly_adjusted(os.path.join(DATA, "gld_monthly.json"))
    tlt = parse_monthly_adjusted(os.path.join(DATA, "tlt_monthly.json"))
    wti = parse_wti(os.path.join(DATA, "wti_monthly.json"))

    months = sorted(set(acwi) & set(gld) & set(tlt) & set(wti))
    r_growth = [acwi[m] / wti[m] for m in months]
    r_infl = [gld[m] / tlt[m] for m in months]
    ma_growth = trailing_ma(r_growth, MA_MONTHS)
    ma_infl = trailing_ma(r_infl, MA_MONTHS)

    rows = []
    for i, m in enumerate(months):
        if ma_growth[i] is None or ma_infl[i] is None:
            continue
        g = "croissance" if r_growth[i] > ma_growth[i] else "recession"
        inf = "inflation" if r_infl[i] > ma_infl[i] else "deflation"
        q = QUADRANTS[(g, inf)]
        rows.append({
            "mois": m,
            "ratio_croissance": round(r_growth[i], 4),
            "ma7_croissance": round(ma_growth[i], 4),
            "ecart_croissance_pct": round((r_growth[i] / ma_growth[i] - 1) * 100, 2),
            "ratio_inflation": round(r_infl[i], 4),
            "ma7_inflation": round(ma_infl[i], 4),
            "ecart_inflation_pct": round((r_infl[i] / ma_infl[i] - 1) * 100, 2),
            "quadrant_id": q["id"],
            "quadrant": q["nom"],
        })

    transitions = []
    for prev, cur in zip(rows, rows[1:]):
        if cur["quadrant_id"] != prev["quadrant_id"]:
            transitions.append({"mois": cur["mois"], "de": prev["quadrant"], "vers": cur["quadrant"],
                                "de_id": prev["quadrant_id"], "vers_id": cur["quadrant_id"]})

    last = rows[-1]
    since = last["mois"]
    for r in reversed(rows):
        if r["quadrant_id"] != last["quadrant_id"]:
            break
        since = r["mois"]

    # Backtest
    prices = {
        "actions": acwi, "or": gld, "obligations": tlt,
        "cash": {m: 1.0 for m in months},
    }
    quad_by_month = {r["mois"]: r["quadrant_id"] for r in rows}
    bt = backtest([r["mois"] for r in rows], prices, quad_by_month)

    # État + détection de changement
    state_path = os.path.join(BASE, "state.json")
    prev_state = None
    if os.path.exists(state_path):
        with open(state_path, encoding="utf-8") as f:
            prev_state = json.load(f)
    changement = bool(prev_state and prev_state.get("quadrant_id")
                      and prev_state["quadrant_id"] != last["quadrant_id"])

    qmeta = next(q for q in QUADRANTS.values() if q["id"] == last["quadrant_id"])
    state = {
        "date_calcul": date.today().isoformat(),
        "dernier_mois": last["mois"],
        "quadrant_id": last["quadrant_id"],
        "quadrant": last["quadrant"],
        "depuis": since,
        "actif_roi": qmeta["actif_roi"],
        "exclu": qmeta["exclu"],
        "allocations": qmeta["allocations"],
        "ecart_croissance_pct": last["ecart_croissance_pct"],
        "ecart_inflation_pct": last["ecart_inflation_pct"],
        "changement_detecte": changement,
        "quadrant_precedent": prev_state.get("quadrant") if prev_state else None,
        "transitions": transitions[-12:],
    }
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

    os.makedirs(DOCS, exist_ok=True)
    with open(os.path.join(DOCS, "history.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    with open(os.path.join(BASE, "dashboard_template.html"), encoding="utf-8") as f:
        tpl = f.read()
    payload = {"state": state, "rows": rows, "transitions": transitions,
               "quadrants": {q["id"]: q for q in QUADRANTS.values()},
               "backtest": bt}
    with open(os.path.join(DOCS, "index.html"), "w", encoding="utf-8") as f:
        f.write(tpl.replace("__DATA__", json.dumps(payload, ensure_ascii=False)))

    print(f"CHANGEMENT={'OUI' if changement else 'NON'}")
    print(f"QUADRANT={last['quadrant']} | DEPUIS={since}")
    print(f"ECARTS: croissance {last['ecart_croissance_pct']:+.1f}% / inflation {last['ecart_inflation_pct']:+.1f}%")
    for p in bt["portfolios"]:
        print(f"BACKTEST {p['nom']}: {p['final']} € ({p['total_pct']:+.1f}%, CAGR {p['cagr_pct']}%, DD max {p['maxdd_pct']}%)")

    if changement:
        send_email(
            f"🚨 Changement de cadran : {last['quadrant']}",
            f"""<div style="font-family:Arial,sans-serif;max-width:560px;margin:0 auto;color:#0f172a">
            <h2 style="color:{qmeta['couleur']}">🚨 Bascule détectée : {last['quadrant']}</h2>
            <p><b>{prev_state['quadrant']}</b> &rarr; <b>{last['quadrant']}</b> (mois : {last['mois']})</p>
            <p>Écarts vs moyenne mobile 7 ans :<br>
            &bull; croissance <b>{last['ecart_croissance_pct']:+.1f}&nbsp;%</b><br>
            &bull; inflation <b>{last['ecart_inflation_pct']:+.1f}&nbsp;%</b></p>
            <table style="width:100%;border-collapse:collapse;font-size:14px">
              <tr><td style="padding:8px;background:#f8fafc;border:1px solid #e2e8f0"><b>Actif roi</b></td>
                  <td style="padding:8px;border:1px solid #e2e8f0">{qmeta['actif_roi']}</td></tr>
              <tr><td style="padding:8px;background:#f8fafc;border:1px solid #e2e8f0"><b>À retirer (cadran opposé)</b></td>
                  <td style="padding:8px;border:1px solid #e2e8f0">{qmeta['exclu']}</td></tr>
            </table>
            <p style="font-size:14px;color:#475569">Allocation type : {qmeta['allocations']}</p>
            <p style="font-size:14px">Le détail complet est sur le tableau de bord GitHub Pages.</p>
            </div>"""
        )


if __name__ == "__main__":
    main()
