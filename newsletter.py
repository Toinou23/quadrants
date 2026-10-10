#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Newsletter hebdomadaire des 4 Quadrants — envoyée chaque vendredi soir.
Contenu 100 % factuel :
  1. Cadran actuel, zone de bascule.
  2. Trajectoire sur 12 mois (graphique) + tendance de déplacement (positions passées,
     vitesse sur 3 mois, distance aux frontières, extrapolation naïve, direction).
  3. Variations des 4 actifs du modèle (1 mois / 3 mois).
  4. Titres d'actualité récents liés aux quadrants (Google News RSS, avec liens).
  5. Point backtest des 4 portefeuilles.
Reconstruit la liste des abonnés depuis la boîte Gmail (IMAP) avant l'envoi
(aucun fichier d'abonnés dans le dépôt).
Sans identifiants email : génère un aperçu docs/newsletter_apercu.html sans envoyer.

Robustesse : chaque section non essentielle est isolée (try/except) ; le script
termine en code 0, sauf impossibilité bloquante d'envoyer (clairement loguée).
"""
import json, os, sys, csv, re, traceback, urllib.request, urllib.parse, html as html_mod
from datetime import date
import quadrants as Q
from quadrants import (BASE, DATA, DOCS, process_inbox, send_email, load_subscribers,
                       parse_monthly_adjusted, parse_wti, mail_creds, unsubscribe_footer,
                       QUADRANTS)

SEUIL_BASCULE = 5.0   # % : en-dessous, on signale une zone de bascule possible
SEUIL_STABLE = 1.0    # pts de % par mois : en-dessous, un axe est considéré stable
CHART_PATH = os.path.join(DOCS, "trajectoire.png")
CHART_CID = "trajectoire"

QMETA = {q["id"]: q for q in QUADRANTS.values()}
MOIS_FR = ["janv.", "févr.", "mars", "avr.", "mai", "juin",
           "juil.", "août", "sept.", "oct.", "nov.", "déc."]
MOIS_FR_LONG = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet",
                "août", "septembre", "octobre", "novembre", "décembre"]

# Palette email
INK, MUTED, LINE, SOFT, DARK = "#0f172a", "#64748b", "#e2e8f0", "#f8fafc", "#0b0f14"
FONT = ("-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Helvetica Neue',"
        "Helvetica,Arial,sans-serif")


def log(msg):
    print(msg, flush=True)


def safe(nom, fn, *args, default=""):
    """Exécute une section non essentielle ; en cas d'erreur, log et continue."""
    try:
        return fn(*args)
    except Exception as e:
        log(f"[{nom}] section ignorée : {type(e).__name__}: {e}")
        traceback.print_exc(limit=3)
        return default


# ---------------------------------------------------------------- formats
def fr(x, d=1, signe=True):
    s = f"{x:+.{d}f}" if signe else f"{x:.{d}f}"
    return s.replace("-", "−").replace(".", ",")


def mois_court(m):
    y, mo = m.split("-")
    return f"{MOIS_FR[int(mo) - 1]} {y[2:]}"


def mois_long(m):
    try:
        y, mo = m.split("-")
        return f"{MOIS_FR_LONG[int(mo) - 1]} {y}"
    except Exception:
        return str(m)


def quad_of(eg, ei):
    g = "croissance" if eg > 0 else "recession"
    i = "inflation" if ei > 0 else "deflation"
    return QUADRANTS[(g, i)]


def pct(a, b):
    return (a / b - 1) * 100


# ---------------------------------------------------------------- actualités
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
        if t and l and l.group(1).strip().startswith(("https://", "http://")):
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
            log(f"RSS {label}: échec ({e})")
    return news


# ---------------------------------------------------------------- données
def load_rows():
    path = os.path.join(DOCS, "history.csv")
    with open(path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["eg"] = float(r["ecart_croissance_pct"])
        r["ei"] = float(r["ecart_inflation_pct"])
    if not rows:
        raise ValueError("history.csv vide")
    return rows


def state_from_rows(rows):
    """État minimal reconstruit depuis l'historique (si state.json illisible)."""
    last = rows[-1]
    since = last["mois"]
    for r in reversed(rows):
        if r["quadrant_id"] != last["quadrant_id"]:
            break
        since = r["mois"]
    return {"dernier_mois": last["mois"], "quadrant_id": last["quadrant_id"],
            "quadrant": last["quadrant"], "depuis": since}


def load_state(rows):
    try:
        with open(os.path.join(BASE, "state.json"), encoding="utf-8") as f:
            st = json.load(f)
        if st.get("quadrant_id") in QMETA:
            return st
    except Exception as e:
        log(f"state.json illisible ({e}) — état reconstruit depuis history.csv")
    return state_from_rows(rows)


# ---------------------------------------------------------------- tendance
def compute_trend(rows):
    last = rows[-1]
    eg, ei = last["eg"], last["ei"]

    def back(k):
        return rows[-1 - k] if len(rows) > k else None

    positions = []
    for k, lib in ((12, "Il y a 12 mois"), (6, "Il y a 6 mois"), (3, "Il y a 3 mois"),
                   (1, "Il y a 1 mois"), (0, "Maintenant")):
        r = back(k)
        if r is not None:
            positions.append({"lib": lib, "mois": r["mois"], "eg": r["eg"], "ei": r["ei"],
                              "qid": r["quadrant_id"]})

    r3 = back(3) or rows[0]
    n = max(1, len(rows) - 1 - rows.index(r3))
    axes = []
    for key, nom, ratio, frontiere in (
            ("eg", "Croissance", "ETF World / WTI", "croissance / récession"),
            ("ei", "Inflation", "Or / Obligations longues", "inflation / déflation")):
        now, before = last[key], r3[key]
        v = (now - before) / n
        # même règle que le site (dashboard_template.html, trajSynthese) : le point se
        # rapproche de l'axe si sa vitesse est de signe opposé à sa position
        rapproche = now * v < 0
        stable = abs(v) < SEUIL_STABLE
        eta = None
        if rapproche and not stable:
            eta = abs(now) / abs(v)
        axes.append({"key": key, "nom": nom, "ratio": ratio, "frontiere": frontiere,
                     "now": now, "before": before, "mois_before": r3["mois"],
                     "v": v, "rapproche": rapproche, "stable": stable, "eta": eta})

    ag, ai = axes
    proche = min(axes, key=lambda a: abs(a["now"]))

    cur = QMETA[last["quadrant_id"]]
    if ag["stable"] and ai["stable"]:
        synth = (f"Position stable : déplacement inférieur à {fr(SEUIL_STABLE, 0, False)} pt/mois "
                 f"sur les deux axes depuis 3 mois.")
        cible = cur
    else:
        sx = ag["v"] if not ag["stable"] else eg
        sy = ai["v"] if not ai["stable"] else ei
        cible = quad_of(sx, sy)
        if cible["id"] == cur["id"]:
            synth = (f"Direction : vers l'intérieur du cadran {cur['nom']} "
                     f"(la position s'éloigne des frontières).")
        else:
            synth = f"Direction : vers le cadran {cible['nom']}."
    return {"positions": positions, "axes": axes, "proche": proche,
            "synthese": synth, "cible": cible, "eg": eg, "ei": ei}


# ---------------------------------------------------------------- graphique
def build_chart(rows, path=CHART_PATH, n=12):
    """Trajectoire des n derniers mois dans le plan (écart croissance, écart inflation).
    Échelle « racine signée » : sign(x)·√|x| — garde l'origine au centre, lisible de ±2 % à ±200 %."""
    import math
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle
    from matplotlib import patheffects as pe

    pts = rows[-(n + 1):]
    xs = [r["eg"] for r in pts]
    ys = [r["ei"] for r in pts]

    fwd = lambda a: [math.copysign(math.sqrt(abs(v)), v) for v in a]
    inv = lambda a: [math.copysign(v * v, v) for v in a]
    import numpy as np
    f_np = lambda a: np.sign(a) * np.sqrt(np.abs(a))
    i_np = lambda a: np.sign(a) * a * a

    def borne(vals):
        m = max([abs(v) for v in vals] + [12.0])
        for b in (15, 25, 40, 60, 80, 100, 150, 200, 300, 400, 600, 1000):
            if m * 1.25 <= b:
                return b
        return m * 1.25

    Lx, Ly = borne(xs), borne(ys)  # bornes symétriques par axe : origine toujours au centre

    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 13})
    fig, ax = plt.subplots(figsize=(7, 7), dpi=160)
    fig.patch.set_facecolor("white")
    ax.set_facecolor("white")
    ax.set_xscale("function", functions=(f_np, i_np))
    ax.set_yscale("function", functions=(f_np, i_np))
    ax.set_xlim(-Lx, Lx)
    ax.set_ylim(-Ly, Ly)

    def to_ax(x, y):
        d = ax.transData.transform((x, y))
        return ax.transAxes.inverted().transform(d)

    pts_ax = [to_ax(x, y) for x, y in zip(xs, ys)]

    def libre(cx, cy):
        return min([math.hypot(cx - px, cy - py) for px, py in pts_ax] + [9])

    # zones des 4 cadrans
    zones = [((0, 0), "boom_inflationniste", 1, 1),
             ((-Lx, 0), "recession_inflationniste", -1, 1),
             ((0, -Ly), "boom_deflationniste", 1, -1),
             ((-Lx, -Ly), "depression_deflationniste", -1, -1)]
    cur_id = rows[-1]["quadrant_id"]
    for (x0, y0), qid, sx, sy in zones:
        q = QMETA[qid]
        ax.add_patch(Rectangle((x0, y0), Lx, Ly, facecolor=q["couleur"], alpha=0.09,
                               edgecolor="none", zorder=0))
        # nom du cadran : coin (extérieur/intérieur) le plus éloigné de la trajectoire
        cands = []
        for ox in (0.975, 0.58):
            for oy in (0.975, 0.58):
                cx = 0.5 + sx * (ox - 0.5)
                cy = 0.5 + sy * (oy - 0.5)
                cands.append((libre(cx, cy) + (0.02 if (ox, oy) == (0.975, 0.975) else 0), cx, cy))
        _, cx, cy = max(cands)
        ha = "right" if (sx > 0) == (cx > 0.75 or cx < 0.25) else "left"
        if sx < 0:
            ha = "left" if cx < 0.25 else "right"
        else:
            ha = "right" if cx > 0.75 else "left"
        va = ("top" if cy > 0.75 else "bottom") if sy > 0 else ("bottom" if cy < 0.25 else "top")
        actuel = qid == cur_id
        ax.text(cx, cy, q["nom"].replace(" ", "\n", 1), transform=ax.transAxes, ha=ha, va=va,
                fontsize=13, fontweight="bold", color=q["couleur"], alpha=1.0 if actuel else 0.8,
                zorder=1, linespacing=1.1,
                path_effects=[pe.withStroke(linewidth=4, foreground="white", alpha=0.7)])

    # zone de bascule ±5 %
    s = SEUIL_BASCULE
    ax.axvspan(-s, s, color="#64748b", alpha=0.08, lw=0, zorder=0.5)
    ax.axhspan(-s, s, color="#64748b", alpha=0.08, lw=0, zorder=0.5)

    # graduations (en %)
    cand = [5, 10, 25, 50, 100, 200, 400, 800]
    def ticks(L):
        tk = [t for t in cand if t <= L * 1.001]
        if L >= 60:
            tk = [t for t in tk if t != 5]
        if L >= 300:
            tk = [t for t in tk if t != 25]
        tk = sorted([-t for t in tk] + [0] + tk)
        return tk, [("0" if t == 0 else f"{t:+.0f}".replace("-", "−")) for t in tk]

    tk, lab = ticks(Lx)
    ax.set_xticks(tk); ax.set_xticklabels(lab, fontsize=10.5, color="#64748b")
    tk, lab = ticks(Ly)
    ax.set_yticks(tk); ax.set_yticklabels(lab, fontsize=10.5, color="#64748b")
    ax.minorticks_off()
    ax.tick_params(length=0, pad=6)
    ax.grid(True, color="#cbd5e1", lw=0.6, alpha=0.55, zorder=0.6)
    for sp in ax.spines.values():
        sp.set_visible(False)

    # axes croisés en 0
    ax.axhline(0, color="#1e293b", lw=1.8, zorder=1.5)
    ax.axvline(0, color="#1e293b", lw=1.8, zorder=1.5)

    ax.set_xlabel("← Récession     Croissance : ETF World / WTI (écart %)     Croissance →",
                  fontsize=10.5, color="#334155", labelpad=8)
    ax.set_ylabel("← Déflation     Inflation : Or / Oblig. longues (écart %)     Inflation →",
                  fontsize=10.5, color="#334155", labelpad=6)

    # trajectoire : segments de plus en plus intenses, flèche sur les déplacements visibles
    N = len(pts)
    for i in range(N - 1):
        t = (i + 1) / max(1, N - 1)
        ax.plot([xs[i], xs[i + 1]], [ys[i], ys[i + 1]], color="#0f172a", alpha=0.15 + 0.75 * t,
                lw=1.0 + 1.8 * t, solid_capstyle="round", zorder=3)
        seg = math.hypot(pts_ax[i + 1][0] - pts_ax[i][0], pts_ax[i + 1][1] - pts_ax[i][1])
        if seg > 0.035 or i == N - 2:
            ax.annotate("", xy=(xs[i + 1], ys[i + 1]), xytext=(xs[i], ys[i]), zorder=3.2,
                        arrowprops=dict(arrowstyle="-|>,head_length=0.7,head_width=0.35",
                                        color="#0f172a", alpha=0.15 + 0.75 * t, lw=0,
                                        shrinkA=0, shrinkB=7 if i < N - 2 else 10))
    for i in range(N - 1):
        t = i / max(1, N - 1)
        q = QMETA[pts[i]["quadrant_id"]]
        ax.scatter(xs[i], ys[i], s=30 + 55 * t, color=q["couleur"], alpha=0.30 + 0.65 * t,
                   edgecolors="white", linewidths=1.0, zorder=4)

    # point actuel
    qc = QMETA[pts[-1]["quadrant_id"]]
    ax.scatter(xs[-1], ys[-1], s=650, color=qc["couleur"], alpha=0.20, lw=0, zorder=4.5)
    ax.scatter(xs[-1], ys[-1], s=170, color=qc["couleur"], edgecolors="#0f172a",
               linewidths=2.2, zorder=5)

    def meilleur_offset(i, dist=0.07, extra=()):
        px, py = pts_ax[i]
        best = None
        for dx, dy in ((1, -1), (1, 1), (-1, -1), (-1, 1), (1.3, 0), (-1.3, 0), (0, -1.2), (0, 1.2)):
            cx, cy = px + dx * dist, py + dy * dist
            if not (0.08 < cx < 0.92 and 0.06 < cy < 0.94):
                continue
            others = [p for j, p in enumerate(pts_ax) if j not in (i, N - 1)] + list(extra)
            sc = min([math.hypot(cx - ox, cy - oy) for ox, oy in others] + [9])
            if i != N - 1:  # le point actuel (gros marqueur + étiquette) compte double
                cxn, cyn = pts_ax[N - 1]
                sc = min(sc, math.hypot(cx - cxn, cy - cyn) - 0.08)
            if best is None or sc > best[0]:
                best = (sc, dx, dy)
        return (best[1], best[2]) if best else (1, -1)

    # étiquette du point actuel
    dx, dy = meilleur_offset(N - 1, 0.12)
    ha = "left" if dx > 0 else ("right" if dx < 0 else "center")
    va = "bottom" if dy > 0 else ("top" if dy < 0 else "center")
    ax.annotate(f"{mois_long(pts[-1]['mois'])}\n{fr(xs[-1])} % / {fr(ys[-1])} %",
                (xs[-1], ys[-1]), xytext=(dx * 16, dy * 16), textcoords="offset points",
                ha=ha, va=va, fontsize=11.5, fontweight="bold", color="#0f172a", zorder=7,
                bbox=dict(boxstyle="round,pad=0.4", fc="white", ec=qc["couleur"], lw=1.5))
    cur_lab = (pts_ax[-1][0] + dx * 0.14, pts_ax[-1][1] + dy * 0.08)

    # étiquettes discrètes : il y a 12, 6, 3 mois
    halo = [pe.withStroke(linewidth=3.5, foreground="white")]
    for i in sorted({0, max(0, N - 7), max(0, N - 4)} - {N - 1}):
        dx, dy = meilleur_offset(i, 0.05, extra=[cur_lab])
        ax.annotate(mois_court(pts[i]["mois"]), (xs[i], ys[i]), xytext=(dx * 7, dy * 7),
                    textcoords="offset points", ha="left" if dx > 0 else ("right" if dx < 0 else "center"),
                    va="bottom" if dy > 0 else ("top" if dy < 0 else "center"), fontsize=10, color="#475569",
                    path_effects=halo, zorder=6)

    fig.text(0.5, 0.012, "Écarts vs moyenne mobile 7 ans · échelle en racine carrée (compresse les grands écarts)\n"
             "Bandes grises : zone de bascule ±5 %", fontsize=9, color="#94a3b8", ha="center",
             linespacing=1.4)
    fig.tight_layout(rect=(0, 0.045, 1, 1))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fig.savefig(path, dpi=160, facecolor="white")
    plt.close(fig)
    log(f"Graphique de trajectoire écrit : {path}")
    return path


# ---------------------------------------------------------------- blocs HTML
def h_section(titre, contenu, sous_titre=""):
    st = (f'<p style="margin:4px 0 0;font-size:13px;color:{MUTED};line-height:1.5">{sous_titre}</p>'
          if sous_titre else "")
    return (f'<div style="padding:26px 28px;border-top:1px solid {LINE}">'
            f'<h2 style="margin:0;font-size:18px;line-height:1.3;color:{INK};font-weight:700">{titre}</h2>'
            f'{st}<div style="margin-top:16px">{contenu}</div></div>')


def dot(couleur, size=10):
    return (f'<span style="color:{couleur};font-size:{size + 4}px;line-height:1;'
            f'margin-right:5px">&#9679;</span>')


def header_html(state, qmeta, eg, ei):
    c = qmeta["couleur"]

    def pill(lib, val):
        col = "#34d399" if val > 0 else "#f87171"
        return (f'<td style="padding:12px 14px;background:#151b23;border-radius:10px;width:50%">'
                f'<div style="font-size:11px;letter-spacing:1px;text-transform:uppercase;color:#94a3b8">{lib}</div>'
                f'<div style="font-size:22px;font-weight:700;color:#f8fafc;margin-top:2px">{fr(val)}&nbsp;%</div>'
                f'<div style="font-size:11px;color:{col}">vs moyenne 7 ans</div></td>')

    return f"""<div style="background:{DARK};padding:28px 28px 24px;border-top:5px solid {c}">
<div style="font-size:11px;letter-spacing:2px;text-transform:uppercase;color:#94a3b8">La news des 4 Quadrants &middot; {date.today().strftime('%d/%m/%Y')}</div>
<div style="margin-top:18px;font-size:12px;color:#94a3b8">Cadran actuel</div>
<div style="font-size:28px;line-height:1.2;font-weight:800;color:{c};margin-top:2px">{state['quadrant']}</div>
<div style="font-size:13px;color:#cbd5e1;margin-top:6px">Inchangé depuis {mois_long(state.get('depuis', ''))} &middot; données jusqu'à {mois_long(state.get('dernier_mois', ''))}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin-top:18px;border-collapse:separate;border-spacing:0">
<tr>{pill('Croissance', eg)}<td style="width:10px"></td>{pill('Inflation', ei)}</tr></table>
<div style="font-size:13px;color:#cbd5e1;margin-top:14px;line-height:1.5"><b style="color:#f8fafc">Actif roi :</b> {qmeta['actif_roi']} &middot; <b style="color:#f8fafc">À retirer :</b> {qmeta['exclu']}</div>
</div>"""


def alerte_html(eg, ei):
    if not (abs(eg) < SEUIL_BASCULE or abs(ei) < SEUIL_BASCULE):
        return ""
    axe = "croissance" if abs(eg) < abs(ei) else "inflation"
    return (f'<div style="padding:16px 28px 0"><div style="padding:12px 16px;background:#fef3c7;'
            f'border-left:4px solid #f59e0b;border-radius:8px;font-size:14px;line-height:1.5;color:#78350f">'
            f'<b>⚠️ Zone de bascule possible</b> : l\'écart de l\'axe {axe} est inférieur à '
            f'{SEUIL_BASCULE:.0f}&nbsp;% — une bascule de cadran peut survenir dans les prochaines semaines.'
            f'</div></div>')


def trajectoire_html(img_src):
    return h_section(
        "Trajectoire sur 12 mois",
        f'<img src="{img_src}" width="544" alt="Trajectoire des 12 derniers mois dans les 4 cadrans" '
        f'style="display:block;width:100%;max-width:544px;height:auto;border:0;border-radius:10px">'
        f'<p style="margin:10px 0 0;font-size:12px;color:{MUTED};line-height:1.5">Chaque point = un mois ; '
        f'les flèches indiquent le sens du déplacement, du plus ancien (pâle) au plus récent (cerclé).</p>',
        "Position mensuelle sur les deux axes : à droite la croissance, en haut l'inflation.")


def tendance_html(tr):
    td = f'padding:9px 8px;border-bottom:1px solid {LINE};font-size:13px;color:{INK}'
    th = (f'padding:8px;border-bottom:2px solid {LINE};font-size:11px;color:{MUTED};'
          f'text-transform:uppercase;letter-spacing:.5px;font-weight:600')
    lignes = ""
    for p in tr["positions"]:
        q = QMETA[p["qid"]]
        maintenant = p["lib"] == "Maintenant"
        bg = f"background:{SOFT};" if maintenant else ""
        fw = "font-weight:700;" if maintenant else ""
        lignes += (f'<tr><td style="{td};{bg}{fw}">{p["lib"]}<br><span style="font-size:11px;color:{MUTED};font-weight:400">{mois_long(p["mois"])}</span></td>'
                   f'<td style="{td};{bg}">{dot(q["couleur"])}<span style="color:{q["couleur"]};font-weight:600">{q["nom"]}</span></td>'
                   f'<td style="{td};{bg}{fw}text-align:right;white-space:nowrap">{fr(p["eg"])}&nbsp;%</td>'
                   f'<td style="{td};{bg}{fw}text-align:right;white-space:nowrap">{fr(p["ei"])}&nbsp;%</td></tr>')
    table = (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse">'
             f'<tr><th style="{th};text-align:left">Moment</th><th style="{th};text-align:left">Cadran</th>'
             f'<th style="{th};text-align:right">Croiss.</th><th style="{th};text-align:right">Infl.</th></tr>'
             f'{lignes}</table>')

    cartes = ""
    for a in tr["axes"]:
        fleche = "↗" if a["v"] > 0 else "↘"
        if a["stable"]:
            etat, col = "stable (moins de 1 pt/mois)", MUTED
        elif a["rapproche"]:
            etat, col = "se rapproche de l'axe", "#b45309"
        else:
            etat, col = "s'éloigne de l'axe", "#047857"
        eta = ""
        if a["eta"] is not None:
            nmois = a["eta"]
            delai = f"~{max(1, round(nmois))} mois" if nmois <= 36 else "plus de 3 ans"
            eta = (f'<div style="margin-top:8px;font-size:12px;color:#92400e;background:#fffbeb;'
                   f'padding:6px 8px;border-radius:6px;line-height:1.4">À ce rythme, franchissement '
                   f'possible dans <b>{delai}</b><br><span style="color:{MUTED}">(extrapolation linéaire, pas une prévision)</span></div>')
        cartes += (f'<td valign="top" style="padding:14px;border:1px solid {LINE};border-radius:10px;width:50%">'
                   f'<div style="font-size:11px;text-transform:uppercase;letter-spacing:1px;color:{MUTED}">Axe {a["nom"].lower()}</div>'
                   f'<div style="font-size:12px;color:{MUTED}">{a["ratio"]}</div>'
                   f'<div style="font-size:22px;font-weight:700;color:{INK};margin-top:6px">{fr(a["now"])}&nbsp;%</div>'
                   f'<div style="font-size:12px;color:{MUTED}">il y a 3 mois : {fr(a["before"])}&nbsp;%</div>'
                   f'<div style="margin-top:8px;font-size:14px;font-weight:700;color:{col}">{fleche} {fr(a["v"])} pts/mois</div>'
                   f'<div style="font-size:12px;color:{col}">{etat}</div>{eta}</td>')
        if a["key"] == "eg":
            cartes += '<td style="width:10px"></td>'
    boussoles = (f'<div style="margin-top:22px;font-size:14px;font-weight:700;color:{INK}">Les deux boussoles &middot; vitesse sur 3 mois</div>'
                 f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
                 f'style="margin-top:10px;border-collapse:separate;border-spacing:0"><tr>{cartes}</tr></table>')

    pr = tr["proche"]
    distance = (f'<p style="margin:16px 0 0;font-size:14px;line-height:1.6;color:{INK}">'
                f'<b>Frontière la plus proche :</b> {pr["frontiere"]} (axe {pr["nom"].lower()}), '
                f'à <b>{fr(abs(pr["now"]), 1, False)} pts</b>.</p>')
    c = tr["cible"]["couleur"]
    synth = (f'<div style="margin-top:14px;padding:12px 16px;background:{SOFT};border-left:4px solid {c};'
             f'border-radius:8px;font-size:14px;line-height:1.5;color:{INK}"><b>{tr["synthese"]}</b>'
             f'<br><span style="font-size:12px;color:{MUTED}">Sens du déplacement moyen des 3 derniers mois.</span></div>')
    return h_section("Tendance de déplacement", table + boussoles + distance + synth,
                     "Où était le point, à quelle vitesse il bouge, et vers où.")


def actifs_html():
    acwi = parse_monthly_adjusted(os.path.join(DATA, "acwi_monthly.json"))
    gld = parse_monthly_adjusted(os.path.join(DATA, "gld_monthly.json"))
    tlt = parse_monthly_adjusted(os.path.join(DATA, "tlt_monthly.json"))
    wti = parse_wti(os.path.join(DATA, "wti_monthly.json"))

    def var_series(s):
        ms = sorted(s)
        return pct(s[ms[-1]], s[ms[-2]]), pct(s[ms[-1]], s[ms[-4]] if len(ms) > 3 else s[ms[0]])

    actifs = [("ETF World (ACWI)", *var_series(acwi)), ("Or (GLD)", *var_series(gld)),
              ("Obligations longues (TLT)", *var_series(tlt)), ("Pétrole WTI", *var_series(wti))]

    def cell(v):
        col = "#047857" if v >= 0 else "#b91c1c"
        return (f'<td style="padding:10px 8px;border-bottom:1px solid {LINE};text-align:right;'
                f'font-size:14px;font-weight:600;color:{col};white-space:nowrap">{fr(v)}&nbsp;%</td>')

    th = f'padding:8px;border-bottom:2px solid {LINE};font-size:11px;color:{MUTED};text-transform:uppercase;letter-spacing:.5px;font-weight:600'
    lignes = "".join(
        f'<tr><td style="padding:10px 8px;border-bottom:1px solid {LINE};font-size:14px;color:{INK}">{n}</td>{cell(v1)}{cell(v3)}</tr>'
        for n, v1, v3 in actifs)
    return h_section("Les 4 actifs du modèle",
                     f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse">'
                     f'<tr><th style="{th};text-align:left">Actif</th><th style="{th};text-align:right">1 mois</th>'
                     f'<th style="{th};text-align:right">3 mois</th></tr>{lignes}</table>')


def news_html():
    news = collect_news()
    if not news:
        return ""
    lis = "".join(
        f'<tr><td style="padding:10px 0;border-bottom:1px solid {LINE}">'
        f'<div style="font-size:10px;font-weight:700;color:{MUTED};text-transform:uppercase;letter-spacing:1px">{html_mod.escape(lab)}</div>'
        f'<a href="{html_mod.escape(l, quote=True)}" style="color:#1d4ed8;text-decoration:none;font-size:14px;line-height:1.45">{html_mod.escape(t)}</a></td></tr>'
        for lab, t, l in news)
    return h_section("Dans l'actualité des quadrants",
                     f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse">{lis}</table>'
                     f'<p style="margin:10px 0 0;font-size:12px;color:{MUTED}">Titres sélectionnés automatiquement '
                     f'(Google Actualités) pour leur lien avec les deux ratios — sans commentaire éditorial.</p>')


def backtest_html():
    with open(os.path.join(DOCS, "index.html"), encoding="utf-8") as f:
        idx = f.read()
    payload = json.loads(re.search(r"const DATA = (\{.*?\});\n", idx, re.S).group(1))
    lignes = ""
    for p in payload["backtest"]["portfolios"]:
        final = f'{p["final"]:,}'.replace(",", "&nbsp;")
        col = "#047857" if p["total_pct"] >= 0 else "#b91c1c"
        lignes += (f'<tr><td style="padding:10px 8px;border-bottom:1px solid {LINE};font-size:14px;color:{INK}">'
                   f'{dot(p.get("couleur", MUTED), 8)}{p["nom"]}</td>'
                   f'<td style="padding:10px 8px;border-bottom:1px solid {LINE};text-align:right;font-size:14px;font-weight:700;white-space:nowrap">{final}&nbsp;€</td>'
                   f'<td style="padding:10px 8px;border-bottom:1px solid {LINE};text-align:right;font-size:13px;color:{col};white-space:nowrap">{fr(p["total_pct"])}&nbsp;%</td></tr>')
    return h_section("Les 4 portefeuilles (depuis 2016)",
                     f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse">{lignes}</table>',
                     "Valeur actuelle de 4 000 € investis en janvier 2016.")


def build_corps(state, rows, img_src):
    qmeta = QMETA[state["quadrant_id"]]
    eg, ei = rows[-1]["eg"], rows[-1]["ei"]
    tr = safe("tendance", compute_trend, rows, default=None)
    parts = [
        safe("en-tête", header_html, state, qmeta, eg, ei),
        safe("alerte", alerte_html, eg, ei),
        trajectoire_html(img_src) if img_src else "",
        safe("tendance-html", tendance_html, tr) if tr else "",
        safe("actifs", actifs_html),
        safe("actualités", news_html),
        safe("backtest", backtest_html),
        (f'<div style="padding:22px 28px;border-top:1px solid {LINE};font-size:13px;color:{MUTED};line-height:1.6">'
         f'Tableau de bord complet : ton site GitHub Pages.<br>'
         f'Lecture fondée sur le cadre théorique de Charles Gave — pas un conseil en investissement.</div>'),
    ]
    # NB : les deux <div> enveloppes restent volontairement ouvertes : send_email()
    # ajoute le pied de désinscription à la fin du HTML, il s'affiche ainsi dans la carte.
    return (f'<div style="margin:0;padding:24px 8px;background:#eef2f6;font-family:{FONT};color:{INK}">'
            f'<div style="max-width:600px;margin:0 auto;background:#ffffff;border-radius:14px;overflow:hidden;'
            f'font-family:{FONT};color:{INK};padding-bottom:20px">'
            + "".join(parts)
            + '<div style="padding:0 28px">')


FERMETURE = "</div></div></div>"


# ---------------------------------------------------------------- principal
def main():
    # Filet de sécurité : si l'état ou l'historique n'existe pas encore
    # (robot quotidien pas encore passé), on les génère à partir des données du dépôt.
    if not os.path.exists(os.path.join(BASE, "state.json")) \
            or not os.path.exists(os.path.join(DOCS, "history.csv")):
        log("state.json / history.csv absents — génération préalable via quadrants.py")
        safe("quadrants.main", Q.main)

    safe("abonnements IMAP", process_inbox)

    try:
        rows = load_rows()
    except Exception as e:
        log(f"ERREUR BLOQUANTE : historique des quadrants indisponible ({e}) — newsletter non envoyée.")
        return 1
    state = load_state(rows)

    chart = safe("graphique", build_chart, rows, default=None)
    if not chart:
        log("Newsletter générée sans graphique de trajectoire.")

    sujet = f"Les 4 Quadrants — la news du {date.today().strftime('%d/%m')}"
    user, _ = mail_creds()

    # Aperçu local (toujours écrit) : image en chemin relatif, même dossier docs/
    try:
        apercu = build_corps(state, rows, "trajectoire.png" if chart else None)
        os.makedirs(DOCS, exist_ok=True)
        with open(os.path.join(DOCS, "newsletter_apercu.html"), "w", encoding="utf-8") as f:
            f.write('<!DOCTYPE html><html lang="fr"><head><meta charset="utf-8">'
                    '<meta name="viewport" content="width=device-width,initial-scale=1">'
                    f'<title>{html_mod.escape(sujet)}</title></head><body style="margin:0">'
                    + apercu + unsubscribe_footer(user or "exemple@gmail.com")
                    + FERMETURE + "</body></html>")
        log("Aperçu écrit : docs/newsletter_apercu.html")
    except Exception as e:
        log(f"[aperçu] non écrit ({e})")

    if not user:
        log(f"Email non configuré — aperçu seulement, {len(load_subscribers())} abonné(s), pas d'envoi.")
        return 0
    if not load_subscribers():
        log("Aucun abonné — pas d'envoi.")
        return 0

    corps = build_corps(state, rows, f"cid:{CHART_CID}" if chart else None)
    images = {CHART_CID: chart} if chart else None
    ok = send_email(sujet, corps, images=images)
    if not ok and images:
        log("Nouvel essai d'envoi sans image…")
        ok = send_email(sujet, build_corps(state, rows, None))
    if not ok:
        log("ERREUR BLOQUANTE : l'envoi SMTP a échoué (voir message ci-dessus) — newsletter non envoyée.")
        return 1
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except Exception:
        traceback.print_exc()
        log("ERREUR inattendue (détail ci-dessus) — fin du script sans plantage.")
        code = 0
    sys.exit(code)
