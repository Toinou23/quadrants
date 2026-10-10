#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Les indicateurs de Charles Gave — section pédagogique du site
=============================================================
Reconstruit, à partir de données réelles (Alpha Vantage), six indicateurs macro
que Charles Gave a commentés dans ses émissions (« Université de l'Épargne »,
ex-Institut des Libertés) et ses articles de juin à octobre 2026
(voir recherche_gave.md, section « Sélection pour le site »), plus quatre
indicateurs tirés des graphiques affichés dans cinq vidéos récentes
(voir videos_gave.md) : taux réel court, impôt énergétique, actions contre
obligations et largeur du marché.

Budget Alpha Vantage (offre gratuite : 25 appels par jour), par nuit :
- quadrants.py : 4 appels (ACWI, GLD, TLT, WTI) ;
- ici, prix de marché (au plus 1 fois par jour) : HYG, IEF, EUR/USD, SPY, RSP = 5 ;
- ici, séries macro (au plus 1 fois par semaine) : taux 10 ans, taux 3 mois,
  IPC, PIB réel = 4 ;
→ 9 appels les soirs ordinaires, 13 au maximum le soir où les séries macro
  sont rafraîchies ; plafond de sécurité MAX_APPELS = 11 ici (4 + 11 = 15).

Appel depuis quadrants.main() :
    indicateurs, barometre = construire(cle_api, quadrant_id_actuel)

Principes de robustesse :
- chaque série n'est re-téléchargée que si elle est « périmée » (date du dernier
  téléchargement réussi notée dans data/indicateurs_meta.json — on ne se fie pas
  à la date des fichiers, remise à zéro à chaque checkout GitHub) ;
- appels espacés (12 s) pour respecter la limite de débit d'Alpha Vantage ;
- une réponse « Note » / « Information » / « Error Message » ne remplace jamais
  le fichier existant ;
- aucune exception ne remonte : un indicateur sans données est marqué
  « disponible: False » et la page s'affiche quand même.
"""
import json, math, os, re, time, traceback, urllib.request
from datetime import datetime, timedelta

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
META_PATH = os.path.join(DATA, "indicateurs_meta.json")

AV = "https://www.alphavantage.co/query?"
PAUSE_S = 12            # secondes entre deux appels (limite de débit)
AGE_MACRO = 6           # jours : séries macro (mensuelles / trimestrielles)
AGE_MARCHE = 0.8        # jours : prix de marché mensuels (mois en cours)
DEBUT_AFFICHAGE = "2010-01"
DEBUT_STOCKAGE = "1998-01"  # les fichiers data/ ne gardent que ces observations (dépôt plus léger)
MA_LONGUE = 84          # 7 ans, comme les deux axes du site
MA_COURTE = 12          # 1 an, pour les signaux d'alerte
MAX_APPELS = 11         # plafond d'appels par nuit pour ce module (quadrants.py en fait 4)

# (fichier, paramètres de requête, type de réponse attendu, âge max en jours)
SERIES = [
    ("treasury10y_monthly.json", "function=TREASURY_YIELD&interval=monthly&maturity=10year", "data", AGE_MACRO),
    ("treasury3m_monthly.json", "function=TREASURY_YIELD&interval=monthly&maturity=3month", "data", AGE_MACRO),
    ("cpi_monthly.json", "function=CPI&interval=monthly", "data", AGE_MACRO),
    ("real_gdp_quarterly.json", "function=REAL_GDP&interval=quarterly", "data", AGE_MACRO),
    ("hyg_monthly.json", "function=TIME_SERIES_MONTHLY_ADJUSTED&symbol=HYG", "ts", AGE_MARCHE),
    ("ief_monthly.json", "function=TIME_SERIES_MONTHLY_ADJUSTED&symbol=IEF", "ts", AGE_MARCHE),
    ("eurusd_monthly.json", "function=FX_MONTHLY&from_symbol=EUR&to_symbol=USD", "fx", AGE_MARCHE),
    ("spy_monthly.json", "function=TIME_SERIES_MONTHLY_ADJUSTED&symbol=SPY", "ts", AGE_MARCHE),
    ("rsp_monthly.json", "function=TIME_SERIES_MONTHLY_ADJUSTED&symbol=RSP", "ts", AGE_MARCHE),
]

VERT, ROUGE, AMBRE, BLEU, GRIS = "#10b981", "#ef4444", "#f59e0b", "#3b82f6", "#94a3b8"


def log(msg):
    print(f"[indicateurs] {msg}", flush=True)


# ---------------------------------------------------------------- téléchargement
def _lire_meta():
    try:
        with open(META_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _reponse_valide(j, genre):
    if not isinstance(j, dict):
        return False
    if any(k in j for k in ("Note", "Information", "Error Message")):
        return False
    if genre == "data":
        return len(j.get("data", [])) > 12
    if genre == "ts":
        return len(j.get("Monthly Adjusted Time Series", {})) > 12
    if genre == "fx":
        return len(j.get("Time Series FX (Monthly)", {})) > 12
    return False


def _alleger(j):
    """Ne garde que les observations depuis DEBUT_STOCKAGE (même format que l'API)."""
    try:
        if isinstance(j.get("data"), list):
            j["data"] = [x for x in j["data"] if str(x.get("date", "")) >= DEBUT_STOCKAGE]
        else:
            for k in [k for k in j if "Time Series" in k]:
                j[k] = {d: v for d, v in j[k].items() if d >= DEBUT_STOCKAGE}
    except Exception:
        pass  # en cas de doute, on garde la réponse complète
    return j


def actualiser(cle):
    """Télécharge les séries périmées. Ne lève jamais d'exception."""
    if not cle:
        log("pas de clé API — calcul sur les fichiers déjà présents.")
        return
    try:
        os.makedirs(DATA, exist_ok=True)
        meta = _lire_meta()
        maintenant = datetime.utcnow()
        appels = 0
        for fichier, params, genre, age_max in SERIES:
            chemin = os.path.join(DATA, fichier)
            try:
                derniere = datetime.fromisoformat(meta.get(fichier, "2000-01-01T00:00:00"))
            except ValueError:
                derniere = datetime(2000, 1, 1)
            if os.path.exists(chemin) and maintenant - derniere < timedelta(days=age_max):
                log(f"{fichier} : à jour (téléchargé le {derniere:%Y-%m-%d}) — pas d'appel.")
                continue
            if appels >= MAX_APPELS:
                log(f"{fichier} : plafond de {MAX_APPELS} appels atteint — reporté au prochain passage.")
                continue
            # pause avant chaque appel (y compris le premier : quadrants.py vient d'en faire 4)
            time.sleep(PAUSE_S)
            appels += 1
            try:
                req = urllib.request.Request(f"{AV}{params}&apikey={cle}",
                                             headers={"User-Agent": "quadrants-gave/1.0"})
                with urllib.request.urlopen(req, timeout=60) as r:
                    j = json.loads(r.read().decode("utf-8"))
                if _reponse_valide(j, genre):
                    with open(chemin, "w", encoding="utf-8") as f:
                        json.dump(_alleger(j), f, separators=(",", ":"))
                    meta[fichier] = maintenant.isoformat(timespec="seconds")
                    log(f"OK  {fichier}")
                else:
                    raison = next((j[k] for k in ("Note", "Information", "Error Message") if k in j),
                                  "réponse inattendue")
                    log(f"KO  {fichier} ({str(raison)[:90]}…) — fichier existant conservé")
            except Exception as e:
                log(f"KO  {fichier} ({type(e).__name__}: {str(e).replace(cle, '***')}) — fichier existant conservé")
        log(f"{appels} appel(s) Alpha Vantage pour les indicateurs ce soir.")
        with open(META_PATH, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log(f"actualisation interrompue ({e}) — fichiers existants conservés")


# ---------------------------------------------------------------- parseurs
def _charger(fichier):
    with open(os.path.join(DATA, fichier), encoding="utf-8") as f:
        return json.load(f)


def serie_data(fichier):
    """Format {"data":[{"date","value"}]} (TREASURY_YIELD, CPI, REAL_GDP, WTI)."""
    out = {}
    for row in _charger(fichier).get("data", []):
        try:
            out[row["date"][:7]] = float(row["value"])
        except (ValueError, KeyError, TypeError):
            continue
    if not out:
        raise ValueError(f"{fichier} vide")
    return out


def serie_ajustee(fichier):
    """TIME_SERIES_MONTHLY_ADJUSTED → cours ajusté (dividendes réinvestis)."""
    j = _charger(fichier)
    cle = next(k for k in j if "Monthly" in k and "Time Series" in k)
    out = {}
    for d, row in j[cle].items():
        try:
            v = float(row.get("5. adjusted close") or row.get("4. close"))
        except (ValueError, TypeError):
            continue  # valeur "." ou vide : mois ignoré
        if v > 0:
            out[d[:7]] = v
    if not out:
        raise ValueError(f"{fichier} vide")
    return out


def serie_fx(fichier):
    j = _charger(fichier)
    out = {}
    for d, row in j["Time Series FX (Monthly)"].items():
        try:
            v = float(row["4. close"])
        except (ValueError, TypeError, KeyError):
            continue
        if v > 0:
            out[d[:7]] = v
    if not out:
        raise ValueError(f"{fichier} vide")
    return out


def trimestriel_vers_mensuel(q):
    """REAL_GDP trimestriel (date = 1er mois du trimestre) → valeur par mois du trimestre."""
    out = {}
    for m, v in q.items():
        y, mo = int(m[:4]), int(m[5:7])
        for k in range(3):
            mm = mo + k
            out[f"{y + (mm - 1) // 12}-{(mm - 1) % 12 + 1:02d}"] = v
    return out


# ---------------------------------------------------------------- outils de calcul
def mois_moins(m, n):
    y, mo = int(m[:4]), int(m[5:7])
    t = y * 12 + (mo - 1) - n
    return f"{t // 12}-{t % 12 + 1:02d}"


def glissement_annuel(s):
    """Variation sur 12 mois en %."""
    return {m: (v / s[mois_moins(m, 12)] - 1) * 100 for m, v in s.items() if mois_moins(m, 12) in s}


def moyenne_mobile(mois, vals, n):
    out, acc = [], 0.0
    for i, v in enumerate(vals):
        acc += v
        if i >= n:
            acc -= vals[i - n]
        out.append(acc / n if i >= n - 1 else None)
    return out


def variation(vals, n, mode):
    """mode 'pct' : variation relative en % ; 'pts' : différence en points."""
    if len(vals) <= n or vals[-1 - n] in (None, 0):
        return None
    a, b = vals[-1 - n], vals[-1]
    return round((b / a - 1) * 100, 2) if mode == "pct" else round(b - a, 2)


def r(x, d=3):
    return None if x is None else round(x, d)


def fr_txt(s):
    """Écriture française des nombres d'un texte : 4.68 → 4,68 ; -0.78 → −0,78."""
    s = re.sub(r"(?<=\d)\.(?=\d)", ",", s)
    return re.sub(r"(?<![\w])-(?=\d)", "−", s)


def mois_fr(m):
    noms = ["janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août",
            "sept.", "oct.", "nov.", "déc."]
    return f"{noms[int(m[5:7]) - 1]} {m[:4]}"


def mois_plus(m, n):
    return mois_moins(m, -n)


def serie_affichee(mois, vals, ref=None, aux=None, debut=DEBUT_AFFICHAGE):
    """Découpe à partir de `debut` (DEBUT_AFFICHAGE par défaut) et arrondit."""
    i0 = next((i for i, m in enumerate(mois) if m >= debut), 0)
    out = {"labels": mois[i0:], "valeurs": [r(v, 4) for v in vals[i0:]]}
    out["reference"] = [r(v, 4) for v in ref[i0:]] if ref else None
    if aux:
        out["aux"] = [r(v, 4) for v in aux[i0:]]
    return out


# ---------------------------------------------------------------- sources (recherche_gave.md)
SRC = {
    "tracasse": {"titre": "« Le sujet qui me tracasse » — Institut des Libertés", "date": "8 juin 2026",
                 "url": "https://institutdeslibertes.org/le-sujet-qui-me-tracasse/"},
    "spacex": {"titre": "« Introduction de Space X : un peu de comptabilité en partie double »", "date": "14 juin 2026",
               "url": "https://institutdeslibertes.org/introduction-de-space-x-un-peu-de-comptabilite-en-partie-double/"},
    "ruine": {"titre": "« Comment vous êtes-vous ruiné ? Lentement au début, très vite à la fin »", "date": "6 sept. 2026",
              "url": "https://institutdeslibertes.org/comment-vous-etes-vous-ruine-lentement-au-debut-tres-vite-a-la-fin/"},
    "crise": {"titre": "« Déroulement standard d'une crise financière »", "date": "14 sept. 2026",
              "url": "https://institutdeslibertes.org/19125-2/"},
    "souverainete": {"titre": "« Le Grand Retour de la Souveraineté »", "date": "4 oct. 2026",
                     "url": "https://institutdeslibertes.org/19155-2/"},
    "e202": {"titre": "Émission #202 « Crise de liquidité en approche »", "date": "16 juin 2026",
             "url": "https://podcasts.apple.com/fr/podcast/202-crise-de-liquidit%C3%A9-en-approche-spacex-openai/id1719302582?i=1000772996255"},
    "e204": {"titre": "Émission #204 « Dette de la France : le chiffre qui condamne »", "date": "10 sept. 2026",
             "url": "https://podcasts.apple.com/fr/podcast/204-dette-de-la-france-le-chiffre-qui-condamne-par/id1719302582?i=1000788900963"},
    "e206": {"titre": "Émission #206 « L'or a perdu 20 % : faut-il vendre maintenant ? »", "date": "24 sept. 2026",
             "url": "https://podcasts.apple.com/fr/podcast/206-lor-a-perdu-20-faut-il-vendre-maintenant/id1719302582?i=1000791421986"},
    "e208": {"titre": "Émission #208 « Votre assurance-vie est pleine d'un actif qui ne protège plus »", "date": "oct. 2026",
             "url": "https://podcasts.apple.com/fr/podcast/208-votre-assurance-vie-est-pleine-dun-actif-qui-ne/id1719302582?i=1000793187238"},
    # Vidéos analysées (videos_gave.md) : date de publication inconnue, lien vers leur fiche sur le site.
    "v_remous": {"titre": "Vidéo « Actions américaines : le remous qu'on ne voit pas à la surface »",
                 "date": "fiche vidéo", "url": "#video-remous"},
    "v_fourmis": {"titre": "Vidéo « Comment les banques centrales volent l'épargne des « petites fourmis » »",
                  "date": "fiche vidéo", "url": "#video-fourmis"},
    "v_catastrophe": {"titre": "Vidéo « La CATASTROPHE que personne ne voit ! »",
                      "date": "fiche vidéo", "url": "#video-catastrophe"},
    "v_or": {"titre": "Vidéo « Les Banques Centrales Vendent des DOLLARS pour Acheter de l'OR ! »",
             "date": "fiche vidéo", "url": "#video-or"},
}


# ---------------------------------------------------------------- les 6 indicateurs
# Chaque fonction renvoie la partie « données » de l'indicateur ; les textes fixes
# (titre, pédagogie, sources) sont dans DEFINITIONS ci-dessous.
# "penche" : vote pour le baromètre — axe ('croissance' | 'inflation'), sens (+1 / -1 / 0).

def calc_r_contre_g(ctx):
    t10 = serie_data("treasury10y_monthly.json")
    cpi = serie_data("cpi_monthly.json")
    pib = trimestriel_vers_mensuel(glissement_annuel_trim(serie_data("real_gdp_quarterly.json")))
    infl = glissement_annuel(cpi)
    # Le PIB est publié avec ~1 trimestre de retard : on fige la dernière croissance connue.
    dernier_pib = max(pib)
    mois = sorted(m for m in set(t10) & set(infl) if m >= min(pib))
    g_nom = []
    for m in mois:
        gr = pib.get(m, pib[dernier_pib] if m > dernier_pib else None)
        g_nom.append(None if gr is None else ((1 + gr / 100) * (1 + infl[m] / 100) - 1) * 100)
    paires = [(m, t10[m], g) for m, g in zip(mois, g_nom) if g is not None]
    mois = [p[0] for p in paires]
    taux = [p[1] for p in paires]
    g1 = [p[2] for p in paires]
    g7 = moyenne_mobile(mois, g1, MA_LONGUE)
    i0 = next(i for i, v in enumerate(g7) if v is not None)
    mois, taux, g1, g7 = mois[i0:], taux[i0:], g1[i0:], g7[i0:]
    ecart = [a - b for a, b in zip(taux, g7)]
    e = ecart[-1]
    d3 = variation(ecart, 3, "pts")
    if e > 0:
        signal, couleur, sens = "Signal : trappe à dette active (R > G)", ROUGE, -1
    elif e > -0.5:
        signal, couleur, sens = "Signal : R rejoint G — zone d'alerte", AMBRE, -1 if (d3 or 0) > 0 else 0
    else:
        signal, couleur, sens = "Signal : croissance au-dessus des taux (G > R)", VERT, +1
    detail = (f"Taux à 10 ans {taux[-1]:.2f} % contre croissance nominale lissée sur 7 ans "
              f"{g7[-1]:.2f} % (sur 1 an : {g1[-1]:.2f} %). PIB connu jusqu'au {['1er', '2e', '3e', '4e'][(int(dernier_pib[5:7]) - 1) // 3]} trimestre {dernier_pib[:4]}.")
    s = serie_affichee(mois, taux, g7, g1)
    s.update({"type": "deux_courbes", "label_valeurs": "R · taux à 10 ans",
              "label_reference": "G · croissance nominale (moy. 7 ans)",
              "label_aux": "G sur 1 an", "seuil": None})
    return {"valeur_actuelle": round(e, 2), "unite": "pts", "format": "pts",
            "mois": mois[-1], "var_3m": d3, "var_12m": variation(ecart, 12, "pts"),
            "signal": signal, "couleur": couleur, "detail": detail,
            "penche": {"axe": "croissance", "sens": sens}, "series": s}


def glissement_annuel_trim(q):
    """Croissance sur un an d'une série trimestrielle (trimestre t / t-4)."""
    out = {}
    for m, v in q.items():
        prev = mois_moins(m, 12)
        if prev in q and q[prev]:
            out[m] = (v / q[prev] - 1) * 100
    return out


def calc_taux_reel(ctx):
    t10 = serie_data("treasury10y_monthly.json")
    infl = glissement_annuel(serie_data("cpi_monthly.json"))
    mois = sorted(set(t10) & set(infl))
    reel = [t10[m] - infl[m] for m in mois]
    ma = moyenne_mobile(mois, reel, MA_LONGUE)
    v, m7 = reel[-1], ma[-1]
    d3 = variation(reel, 3, "pts")
    if v < 0:
        signal, couleur, sens = "Signal : taux réels négatifs — l'épargne obligataire perd", AMBRE, +1
    elif v > m7 and (d3 or 0) > 0:
        signal, couleur, sens = "Signal : taux réels en hausse — pression sur l'or et les débiteurs", ROUGE, -1
    elif v > m7:
        signal, couleur, sens = "Signal : taux réels élevés mais stabilisés", AMBRE, -1
    else:
        signal, couleur, sens = "Signal : taux réels sous leur moyenne — favorable aux actifs réels", VERT, +1
    detail = (f"Taux à 10 ans {t10[mois[-1]]:.2f} % − inflation sur 1 an {infl[mois[-1]]:.2f} % "
              f"= {v:.2f} % (moyenne 7 ans : {m7:.2f} %).")
    s = serie_affichee(mois, reel, ma)
    s.update({"type": "courbe_moyenne", "label_valeurs": "Taux réel à 10 ans",
              "label_reference": "Moyenne mobile 7 ans", "seuil": 0})
    return {"valeur_actuelle": round(v, 2), "unite": "%", "format": "pct_niveau",
            "mois": mois[-1], "var_3m": d3, "var_12m": variation(reel, 12, "pts"),
            "signal": signal, "couleur": couleur, "detail": detail,
            "penche": {"axe": "inflation", "sens": sens}, "series": s}


def _ratio_vs_moyenne(num, den, n_ma, base=None):
    mois = sorted(set(num) & set(den))
    ratio = [num[m] / den[m] for m in mois]
    if base:
        b = next((ratio[i] for i, m in enumerate(mois) if m >= base), ratio[0])
        ratio = [x / b * 100 for x in ratio]
    ma = moyenne_mobile(mois, ratio, n_ma)
    return mois, ratio, ma


def calc_or_actions(ctx):
    mois, ratio, ma = _ratio_vs_moyenne(serie_ajustee("gld_monthly.json"),
                                        serie_ajustee("acwi_monthly.json"), MA_LONGUE)
    ecart = (ratio[-1] / ma[-1] - 1) * 100
    d3 = variation(ratio, 3, "pct")
    if ecart > 0 and (d3 or 0) >= 0:
        signal, couleur, sens = "Signal : l'or bat les actions — régime de défiance", AMBRE, +1
    elif ecart > 0:
        signal, couleur, sens = "Signal : or au-dessus de sa tendance, mais en repli", AMBRE, +1
    elif (d3 or 0) > 0:
        signal, couleur, sens = "Signal : actions devant, l'or se reprend", VERT, -1
    else:
        signal, couleur, sens = "Signal : les actions mènent — régime de confiance", VERT, -1
    detail = f"Ratio {ratio[-1]:.3f}, soit {ecart:+.1f} % par rapport à sa moyenne 7 ans ({ma[-1]:.3f})."
    s = serie_affichee(mois, ratio, ma)
    s.update({"type": "courbe_moyenne", "label_valeurs": "Or / actions mondiales (GLD / ACWI)",
              "label_reference": "Moyenne mobile 7 ans", "seuil": None})
    return {"valeur_actuelle": round(ratio[-1], 3), "unite": "", "format": "ratio",
            "ecart_moyenne_pct": round(ecart, 1),
            "mois": mois[-1], "var_3m": d3, "var_12m": variation(ratio, 12, "pct"),
            "signal": signal, "couleur": couleur, "detail": detail,
            "penche": {"axe": "inflation", "sens": sens}, "series": s}


def calc_stress_credit(ctx):
    mois, ratio, ma = _ratio_vs_moyenne(serie_ajustee("hyg_monthly.json"),
                                        serie_ajustee("ief_monthly.json"), MA_COURTE, base="2010-01")
    ecart = (ratio[-1] / ma[-1] - 1) * 100
    d6 = variation(ratio, 6, "pct")
    d3 = variation(ratio, 3, "pct")
    if ecart < 0 and (d3 or 0) < -3:
        signal, couleur, sens = "Signal : spreads qui s'écartent vite — phase « exponentielle »", ROUGE, -1
    elif ecart < 0:
        signal, couleur, sens = "Signal : premiers signes de méfiance sur le crédit", AMBRE, -1
    else:
        signal, couleur, sens = "Signal : crédit calme — pas de stress visible", VERT, +1
    detail = (f"Ratio {ratio[-1]:.1f} (base 100 en janv. 2010), {ecart:+.1f} % par rapport à sa moyenne 12 mois ; "
              f"variation sur 6 mois : {d6:+.1f} %." if d6 is not None else "")
    s = serie_affichee(mois, ratio, ma)
    s.update({"type": "courbe_moyenne", "label_valeurs": "Obligations risquées / Trésor (HYG / IEF, base 100)",
              "label_reference": "Moyenne mobile 12 mois", "seuil": None})
    return {"valeur_actuelle": round(ratio[-1], 1), "unite": "", "format": "indice",
            "ecart_moyenne_pct": round(ecart, 1),
            "mois": mois[-1], "var_3m": d3, "var_12m": variation(ratio, 12, "pct"),
            "signal": signal, "couleur": couleur, "detail": detail,
            "penche": {"axe": "croissance", "sens": sens}, "series": s}


def calc_euro_dollar(ctx):
    fx = serie_fx("eurusd_monthly.json")
    mois = sorted(fx)
    vals = [fx[m] for m in mois]
    ma = moyenne_mobile(mois, vals, MA_COURTE)
    v, m12 = vals[-1], ma[-1]
    d12 = variation(vals, 12, "pct")
    d3 = variation(vals, 3, "pct")
    if v < m12 and (d3 or 0) < 0:
        signal, couleur, sens = "Signal : l'euro glisse — défiance envers la zone euro", ROUGE, +1
    elif v < m12:
        signal, couleur, sens = "Signal : euro sous sa tendance d'un an", AMBRE, +1
    else:
        signal, couleur, sens = "Signal : euro solide face au dollar", VERT, -1
    detail = f"1 € = {v:.4f} $ (moyenne 12 mois : {m12:.4f} $)."
    s = serie_affichee(mois, vals, ma)
    s.update({"type": "courbe_moyenne", "label_valeurs": "EUR / USD",
              "label_reference": "Moyenne mobile 12 mois", "seuil": None})
    return {"valeur_actuelle": round(v, 4), "unite": "$", "format": "fx",
            "mois": mois[-1], "var_3m": d3, "var_12m": d12,
            "signal": signal, "couleur": couleur, "detail": detail,
            "penche": {"axe": "inflation", "sens": sens}, "series": s}


def calc_energie_obligations(ctx):
    mois, ratio, ma = _ratio_vs_moyenne(serie_data("wti_monthly.json"),
                                        serie_ajustee("tlt_monthly.json"), MA_LONGUE, base="2008-01")
    ecart = (ratio[-1] / ma[-1] - 1) * 100
    d3 = variation(ratio, 3, "pct")
    if ecart > 0:
        signal, couleur, sens = "Signal : l'énergie protège mieux que les obligations", AMBRE, +1
    else:
        signal, couleur, sens = "Signal : les obligations redeviennent la protection", BLEU, -1
    detail = f"Ratio {ratio[-1]:.1f} (base 100 en janv. 2008), {ecart:+.1f} % par rapport à sa moyenne 7 ans."
    s = serie_affichee(mois, ratio, ma)
    s.update({"type": "courbe_moyenne", "label_valeurs": "Pétrole WTI / obligations longues (base 100)",
              "label_reference": "Moyenne mobile 7 ans", "seuil": None})
    return {"valeur_actuelle": round(ratio[-1], 1), "unite": "", "format": "indice",
            "ecart_moyenne_pct": round(ecart, 1),
            "mois": mois[-1], "var_3m": d3, "var_12m": variation(ratio, 12, "pct"),
            "signal": signal, "couleur": couleur, "detail": detail,
            "penche": {"axe": "inflation", "sens": sens}, "series": s}


# ---------------------------------------------------------------- indicateurs tirés des vidéos
def calc_taux_reel_court(ctx):
    """Vidéo « petites fourmis » : bons du Trésor à 3 mois − inflation sur 1 an."""
    t3 = serie_data("treasury3m_monthly.json")
    infl = glissement_annuel(serie_data("cpi_monthly.json"))
    mois = sorted(set(t3) & set(infl))
    if len(mois) < 24:
        raise ValueError("historique insuffisant")
    reel = [t3[m] - infl[m] for m in mois]
    ctx["reel_court"] = dict(zip(mois, reel))  # zones « taux réel négatif » pour actions/obligations
    v, m = reel[-1], mois[-1]
    d3 = variation(reel, 3, "pts")
    depuis = [x for mm, x in zip(mois, reel) if mm >= "2002-01"]
    part_neg = sum(1 for x in depuis if x < 0) / max(1, len(depuis)) * 100
    duree = 0
    for x in reversed(reel):
        if (x < 0) != (v < 0):
            break
        duree += 1
    if v < 0:
        signal, couleur, sens = "Signal : taux réel court négatif — l'épargne sans risque perd du pouvoir d'achat", AMBRE, +1
    elif v < 1:
        signal, couleur, sens = "Signal : taux réel court à peine positif — l'épargnant est tout juste protégé", AMBRE, 0
    else:
        signal, couleur, sens = "Signal : taux réel court nettement positif — l'épargne est rémunérée", BLEU, -1
    detail = (f"Bons du Trésor à 3 mois {t3[m]:.2f} % − inflation sur 1 an {infl[m]:.2f} % = {v:.2f} %. "
              f"Taux réel négatif {part_neg:.0f} % des mois depuis 2002 ; "
              f"{'négatif' if v < 0 else 'positif'} depuis {duree} mois.")
    s = serie_affichee(mois, reel, None, [infl[x] for x in mois], debut="2000-01")
    s.update({"type": "courbe_seuil", "label_valeurs": "Taux réel court (3 mois − inflation)",
              "label_reference": None, "label_aux": "Inflation sur 1 an", "seuil": 0,
              "label_dessous": "zone ambre : taux réel négatif"})
    return {"valeur_actuelle": round(v, 2), "unite": "%", "format": "pct_niveau",
            "mois": m, "var_3m": d3, "var_12m": variation(reel, 12, "pts"),
            "signal": signal, "couleur": couleur, "detail": detail,
            "penche": {"axe": "inflation", "sens": sens}, "series": s}


def calc_impot_energetique(ctx):
    """Vidéo « La CATASTROPHE que personne ne voit ! » : PIB nominal / baril, avancé de 12 mois."""
    gdp = trimestriel_vers_mensuel(serie_data("real_gdp_quarterly.json"))
    cpi = serie_data("cpi_monthly.json")
    wti = {m: v for m, v in serie_data("wti_monthly.json").items() if v > 0}
    dernier_pib = max(gdp)
    # Le PIB réel est publié avec retard : on prolonge son dernier niveau au plus 6 mois
    # (le prix, lui, continue de suivre l'IPC).
    mois = sorted(m for m in set(cpi) & set(wti)
                  if min(gdp) <= m <= mois_plus(dernier_pib, 6))
    if len(mois) < MA_LONGUE + 12:
        raise ValueError("historique insuffisant")
    brut = [gdp.get(m, gdp[dernier_pib]) * cpi[m] / wti[m] for m in mois]
    ratio = [x / brut[0] * 100 for x in brut]
    ma = moyenne_mobile(mois, ratio, MA_LONGUE)
    ecart = (ratio[-1] / ma[-1] - 1) * 100
    d12 = variation(ratio, 12, "pct")
    # Actions : le S&P 500 (SPY), comme dans la vidéo ; à défaut ACWI. Pas de CAPE dans Alpha Vantage :
    # on prend le cours rapporté à sa moyenne mobile 7 ans (100 = sur sa moyenne).
    try:
        act, nom_act = serie_ajustee("spy_monthly.json"), "S&P 500"
    except Exception:
        act, nom_act = serie_ajustee("acwi_monthly.json"), "Actions mondiales"
    am = sorted(act)
    ama = moyenne_mobile(am, [act[x] for x in am], MA_LONGUE)
    act_rel = {x: act[x] / a * 100 for x, a in zip(am, ama) if a}
    decale = {mois_plus(x, 12): v for x, v in zip(mois, ratio)}
    labels = sorted(l for l in set(decale) | set(act_rel) if l >= DEBUT_AFFICHAGE)
    # corrélation (sur les mois communs) entre l'impôt énergétique avancé et les actions
    communs = [l for l in labels if l in decale and l in act_rel]
    corr = None
    if len(communs) > 24:
        xs, ys = [decale[l] for l in communs], [act_rel[l] for l in communs]
        mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
        cov = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
        vx = sum((a - mx) ** 2 for a in xs) ** .5
        vy = sum((b - my) ** 2 for b in ys) ** .5
        corr = cov / (vx * vy) if vx and vy else None
    if ecart > 0 and (d12 or 0) >= 0:
        signal, couleur, sens = "Signal : l'« impôt énergétique » s'allège — vent porteur pour les actions à 12 mois", VERT, +1
    elif ecart > 0:
        signal, couleur, sens = "Signal : énergie encore légère pour l'économie, mais l'« impôt » remonte", AMBRE, +1
    elif (d12 or 0) < 0:
        signal, couleur, sens = "Signal : l'« impôt énergétique » s'alourdit — risque pour les actions d'ici 12 mois", ROUGE, -1
    else:
        signal, couleur, sens = "Signal : énergie chère pour l'économie, mais en voie d'allègement", AMBRE, -1
    detail = (f"PIB nominal approché / prix du baril : {ratio[-1]:.1f} (base 100 en {mois_fr(mois[0])}), "
              f"{ecart:+.1f} % par rapport à sa moyenne 7 ans, {d12:+.1f} % sur 12 mois. "
              f"Point de {mois_fr(mois[-1])}, placé en {mois_fr(mois_plus(mois[-1], 12))} sur le graphique."
              + (f" Corrélation avec la courbe des actions ({nom_act} rapporté à sa moyenne 7 ans) depuis 2010 : {corr:.2f}." if corr is not None else ""))
    s = {"labels": labels,
         "valeurs": [r(decale.get(l), 2) for l in labels],
         "reference": None,
         "aux": [r(act_rel.get(l), 2) for l in labels],
         "type": "deux_axes", "label_valeurs": "PIB / baril, avancé de 12 mois",
         "label_reference": None, "label_aux": f"{nom_act} / sa moyenne 7 ans",
         "seuil": None, "marqueur": max(act_rel) if act_rel else None}
    return {"valeur_actuelle": round(ratio[-1], 1), "unite": "", "format": "indice",
            "ecart_moyenne_pct": round(ecart, 1),
            "mois": mois[-1], "var_3m": variation(ratio, 3, "pct"), "var_12m": d12,
            "signal": signal, "couleur": couleur, "detail": detail,
            # recoupe l'axe croissance du modèle (pétrole au dénominateur) : affiché, pas compté
            "vote": False,
            "penche": {"axe": "croissance", "sens": sens}, "series": s}


def calc_actions_obligations(ctx):
    """Vidéo « petites fourmis » : actions américaines / Trésor 7-10 ans, dividendes et coupons réinvestis."""
    mois, ratio, ma = _ratio_vs_moyenne(serie_ajustee("spy_monthly.json"),
                                        serie_ajustee("ief_monthly.json"), MA_LONGUE, base="1990-01")
    if ma[-1] is None:
        raise ValueError("historique insuffisant")
    ecart = (ratio[-1] / ma[-1] - 1) * 100
    d3 = variation(ratio, 3, "pct")
    if ecart > 0 and (d3 or 0) >= 0:
        signal, couleur, sens = "Signal : les actions battent les obligations — régime de croissance", VERT, +1
    elif ecart > 0:
        signal, couleur, sens = "Signal : actions devant, mais leur avance s'effrite", AMBRE, +1
    elif (d3 or 0) > 0:
        signal, couleur, sens = "Signal : obligations devant, les actions se reprennent", AMBRE, -1
    else:
        signal, couleur, sens = "Signal : les obligations battent les actions — régime de repli", ROUGE, -1
    detail = (f"Ratio {ratio[-1]:.1f} (base 100 en {mois_fr(mois[0])}), "
              f"{ecart:+.1f} % par rapport à sa moyenne 7 ans ({ma[-1]:.1f}).")
    s = serie_affichee(mois, ratio, ma)
    zones = ctx.get("reel_court")
    if zones:
        s["zones"] = [bool(zones.get(m, 1) < 0) for m in s["labels"]]
        s["label_zones"] = "taux réel court négatif"
        # performance annualisée du ratio selon le régime de taux réel (mois m-1 → m)
        neg, pos = [], []
        for i in range(1, len(mois)):
            if mois[i - 1] in zones:
                (neg if zones[mois[i - 1]] < 0 else pos).append(ratio[i] / ratio[i - 1])
        if len(neg) > 12 and len(pos) > 12:
            g = lambda xs: (math.prod(xs) ** (12 / len(xs)) - 1) * 100
            detail += (f" Depuis {mois[0][:4]}, les actions ont battu les obligations de {g(neg):+.1f} % par an "
                       f"pendant les mois de taux réel court négatif, contre {g(pos):+.1f} % par an le reste du temps "
                       f"(période courte : à lire avec prudence).")
    s.update({"type": "courbe_moyenne", "label_valeurs": "Actions US / Trésor 7-10 ans (base 100)",
              "label_reference": "Moyenne mobile 7 ans", "seuil": None})
    return {"valeur_actuelle": round(ratio[-1], 1), "unite": "", "format": "indice",
            "ecart_moyenne_pct": round(ecart, 1),
            "mois": mois[-1], "var_3m": d3, "var_12m": variation(ratio, 12, "pct"),
            "signal": signal, "couleur": couleur, "detail": detail,
            "penche": {"axe": "croissance", "sens": sens}, "series": s}


def calc_largeur_marche(ctx):
    """Vidéo « le remous qu'on ne voit pas à la surface » : S&P 500 équipondéré / pondéré (proxy)."""
    mois, ratio, ma = _ratio_vs_moyenne(serie_ajustee("rsp_monthly.json"),
                                        serie_ajustee("spy_monthly.json"), MA_COURTE, base="1990-01")
    ecart = (ratio[-1] / ma[-1] - 1) * 100
    d3 = variation(ratio, 3, "pct")
    i_max = max(range(len(ratio)), key=lambda i: ratio[i])
    recul = (ratio[-1] / ratio[i_max] - 1) * 100
    if ecart < 0 and (d3 or 0) < 0:
        signal, couleur, sens = "Signal : la hausse se concentre sur quelques géants — remous sous la surface", ROUGE, -1
    elif ecart < 0:
        signal, couleur, sens = "Signal : l'action « moyenne » fait moins bien que l'indice", AMBRE, 0
    else:
        signal, couleur, sens = "Signal : hausse bien répartie entre les titres", VERT, +1
    detail = (f"Ratio {ratio[-1]:.1f} (base 100 en {mois_fr(mois[0])}), {ecart:+.1f} % par rapport à sa moyenne 12 mois ; "
              f"{recul:+.1f} % depuis son plus haut de {mois_fr(mois[i_max])}.")
    s = serie_affichee(mois, ratio, ma)
    s.update({"type": "courbe_moyenne", "label_valeurs": "S&P 500 à poids égaux / officiel (base 100)",
              "label_reference": "Moyenne mobile 12 mois", "seuil": None})
    return {"valeur_actuelle": round(ratio[-1], 1), "unite": "", "format": "indice",
            "ecart_moyenne_pct": round(ecart, 1),
            "mois": mois[-1], "var_3m": d3, "var_12m": variation(ratio, 12, "pct"),
            "signal": signal, "couleur": couleur, "detail": detail,
            "penche": {"axe": "croissance", "sens": sens}, "series": s}


DEFINITIONS = [
    {
        "id": "r_contre_g", "calc": calc_r_contre_g,
        "titre": "R contre G : la « trappe à dette »",
        "sous_titre": "Taux à 10 ans contre croissance nominale · États-Unis",
        "formule": "Écart = taux du Trésor à 10 ans − croissance nominale (PIB réel sur 1 an × inflation sur 1 an), lissée sur 7 ans",
        "axe_quadrant": "Axe croissance",
        "transposition": "Transposition américaine : Charles Gave trace ce graphique pour la France (OAT, croissance française, dette/PIB), données absentes d'Alpha Vantage.",
        "pedago": {
            "ce_que_dit_gave": "Pour Charles Gave, tout se joue entre R, le taux auquel on emprunte, et G, la croissance de l'économie en monnaie courante. Quand le taux dépasse durablement la croissance, la dette grossit plus vite que la richesse qui doit la rembourser : c'est la « trappe à dette ». Il juge la France prise dans ce piège, comme au temps du franc fort des années 1990, et estime que la seule issue est de remettre la croissance au-dessus des taux.",
            "comment_lire": "Imaginez emprunter à 5 % pour un projet qui rapporte 3 % : vous vous appauvrissez chaque année. Sur le graphique, quand la courbe R passe au-dessus de G (zone rouge), l'argent coûte plus cher qu'il ne rapporte : l'investissement freine et la dette publique s'alourdit. Quand G est au-dessus (zone verte), la dette se rembourse presque toute seule. Le signal fort, c'est le croisement.",
            "lien_quadrants": "Cet indicateur éclaire l'axe croissance. Un écart qui devient positif annonce un glissement vers les cadrans « récession » : inflationniste si les prix restent fermes, déflationniste sinon.",
        },
        "sources": ["tracasse", "e204", "ruine", "souverainete"],
    },
    {
        "id": "taux_reel_10a", "calc": calc_taux_reel,
        "titre": "Le taux d'intérêt réel à 10 ans",
        "sous_titre": "Ce que rapporte vraiment une obligation, inflation déduite · États-Unis",
        "formule": "Taux réel = taux du Trésor à 10 ans − inflation des prix à la consommation sur 1 an",
        "axe_quadrant": "Axe inflation",
        "transposition": "Calcul fait sur les données américaines ; Gave parle des taux réels en général (États-Unis et Europe).",
        "pedago": {
            "ce_que_dit_gave": "Charles Gave estime que le monde manque d'épargne pour financer l'IA, l'énergie et la reconstruction : les taux réels devraient donc monter. Dans ses émissions de juin à octobre 2026, il en fait le vrai danger de la période, plus que la correction de l'or : une bonne nouvelle pour l'épargnant, une très mauvaise pour les pays très endettés. Dans la vidéo sur les « petites fourmis », un graphique oppose un indice « Walmart » (alimentation, énergie, logement : les dépenses obligées des ménages modestes) à l'inflation moyenne de l'économie : depuis 1974, et plus encore depuis 2000, il monte bien plus vite. L'écran appelle les périodes de taux réels négatifs l'« euthanasie du rentier » : l'épargnant prudent s'y appauvrit sans bruit.",
            "comment_lire": "Si l'État vous paie 4 % alors que les prix montent de 3 %, vous ne gagnez que 1 % de pouvoir d'achat : c'est le taux réel. Au-dessus de sa moyenne et en hausse, il pèse sur l'or, sur les actifs chers et sur les débiteurs. Négatif ou en baisse, il favorise l'or et les actifs réels.",
            "lien_quadrants": "Il éclaire l'axe inflation et la politique monétaire. Des taux réels qui montent vite accompagnent souvent la sortie des cadrans inflationnistes, ou une crise de liquidité où « le cash est roi ».",
        },
        "sources": ["tracasse", "e202", "e206", "e208", "v_fourmis"],
    },
    {
        "id": "or_contre_actions", "calc": calc_or_actions,
        "titre": "L'or contre les actions mondiales",
        "sous_titre": "Combien de « parts du marché mondial » vaut une once d'or ?",
        "formule": "Ratio = GLD (or) / ACWI (actions mondiales), comparé à sa moyenne mobile 7 ans",
        "axe_quadrant": "Les deux axes",
        "transposition": "Gave utilise l'indice MSCI World ; nous prenons l'ETF ACWI (monde entier, émergents compris), très proche.",
        "pedago": {
            "ce_que_dit_gave": "En juin 2026, Charles Gave a expliqué la chute de ce ratio par des ventes d'or d'investisseurs institutionnels, qui libéraient des liquidités pour souscrire aux introductions en bourse géantes de l'IA. La banque centrale chinoise continuant d'acheter, il attendait un rebond vif de l'or une fois ces opérations terminées. Dans la vidéo sur les banques centrales et l'or, un graphique mesure le prix d'une maison américaine en onces d'or : en dollars, elle a été multipliée par 25 environ depuis 1970 ; en or, elle oscille depuis plus de 50 ans entre 200 et 400 onces. L'or sert ainsi de mètre étalon : ce qui monte en dollars ne s'enrichit pas forcément.",
            "comment_lire": "Quand la courbe monte au-dessus de sa moyenne, l'or protège mieux que les actions : c'est souvent le signe que les investisseurs craignent l'inflation, la crise ou les dettes publiques. Quand elle passe en dessous et baisse, les actions mènent : la confiance domine.",
            "lien_quadrants": "L'or est l'actif roi de la croissance inflationniste, les actions celui de la croissance déflationniste. Ce ratio montre donc vers lequel de ces deux régimes le marché penche.",
        },
        "sources": ["spacex", "v_or"],
    },
    {
        "id": "stress_credit", "calc": calc_stress_credit,
        "titre": "Le thermomètre des spreads",
        "sous_titre": "Obligations d'entreprises risquées contre Trésor américain",
        "formule": "Ratio = HYG (obligations à haut rendement) / IEF (Trésor 7-10 ans), base 100, comparé à sa moyenne mobile 12 mois",
        "axe_quadrant": "Axe croissance",
        "transposition": "Proxy américain : Charles Gave suit l'écart de taux France / Pays-Bas, absent d'Alpha Vantage. Ce graphique mesure le même phénomène sur le crédit des entreprises américaines ; ce n'est pas le graphique qu'il a montré.",
        "pedago": {
            "ce_que_dit_gave": "Charles Gave décrit les crises en trois temps : déni, prise de conscience, panique. L'écart de taux entre un mauvais emprunteur (la France) et un bon (les Pays-Bas) monte d'abord lentement, puis de façon exponentielle, et alors seulement les actions décrochent. Cet écart, d'environ 25 points de base en moyenne de 2012 à 2024, aurait triplé depuis juin 2024.",
            "comment_lire": "Quand une crise approche, les prêteurs exigent d'abord davantage des emprunteurs fragiles. Les obligations risquées baissent alors face aux obligations d'État : la courbe plonge sous sa moyenne. Une baisse lente est un avertissement ; une baisse qui accélère signale la phase « exponentielle ».",
            "lien_quadrants": "Il éclaire l'axe croissance côté risque. Un stress de crédit qui monte précède souvent la bascule vers un cadran « récession ».",
        },
        "sources": ["ruine", "crise", "e204"],
    },
    {
        "id": "euro_dollar", "calc": calc_euro_dollar,
        "titre": "L'euro face au dollar",
        "sous_titre": "« L'égout collecteur » des erreurs d'un pays",
        "formule": "Cours EUR/USD de fin de mois, comparé à sa moyenne mobile 12 mois",
        "axe_quadrant": "Axe inflation (vu d'Europe)",
        "transposition": "Seul le volet change du graphique de Gave est reproduit : l'écart de taux Allemagne-France qu'il lui associe n'existe pas dans Alpha Vantage.",
        "pedago": {
            "ce_que_dit_gave": "Reprenant Jacques Rueff, pour qui le taux de change est « l'égout collecteur » des erreurs d'un pays, Charles Gave observe en octobre 2026 que la tension sur la dette française semble désormais peser sur l'euro : la crise française « s'internationalise ». Si l'euro baisse à cause de la France, toute la zone paiera plus cher son énergie facturée en dollars.",
            "comment_lire": "Quand les marchés doutent de la dette d'un État, ils vendent sa monnaie. Un euro sous sa moyenne d'un an et en baisse traduit une défiance envers la zone euro. Pour un Européen, c'est aussi un pétrole et un gaz plus chers, donc plus d'inflation.",
            "lien_quadrants": "Il éclaire l'axe inflation pour un épargnant européen : un euro faible avec un pétrole cher pousse vers les cadrans inflationnistes en zone euro.",
        },
        "sources": ["souverainete", "tracasse"],
    },
    {
        "id": "energie_contre_obligations", "calc": calc_energie_obligations,
        "titre": "L'énergie a remplacé les obligations",
        "sous_titre": "Pétrole contre obligations longues américaines",
        "formule": "Ratio = WTI ($/baril) / TLT (Trésor 20 ans et +), base 100 en janv. 2008, comparé à sa moyenne mobile 7 ans",
        "axe_quadrant": "Axe inflation",
        "transposition": "Graphique construit pour illustrer une thèse de Gave ; il ne reproduit pas un graphique qu'il aurait montré. Il partage le pétrole avec l'axe croissance du site (ACWI/WTI) : c'est un éclairage, pas un troisième axe.",
        "pedago": {
            "ce_que_dit_gave": "Pendant quarante ans, l'obligation d'État a été l'assurance de l'épargnant. Charles Gave estime que ce n'est plus vrai : avec les guerres, le détroit d'Ormuz et une IA gourmande en électricité, c'est l'énergie qui protège désormais, et le portefeuille « 60/40 » serait devenu une ligne Maginot. Il situe le point de rupture vers 130 à 150 $ le baril.",
            "comment_lire": "Quand la courbe monte au-dessus de sa moyenne, l'énergie protège mieux que les obligations longues : c'est un régime inflationniste, où l'obligation ne joue plus son rôle d'amortisseur. Quand elle baisse, l'inflation reflue et l'obligation redevient la protection.",
            "lien_quadrants": "Il éclaire l'axe inflation, comme un miroir du ratio or/obligations du site avec le pétrole à la place de l'or. Un ratio en hausse conforte les cadrans inflationnistes.",
        },
        "sources": ["e208", "e202"],
    },
    # ---- indicateurs reconstruits à partir des graphiques des vidéos (videos_gave.md)
    {
        "id": "taux_reel_court", "source_video": True, "calc": calc_taux_reel_court,
        "titre": "Le taux réel court : l'épargne des « petites fourmis »",
        "sous_titre": "Bons du Trésor à 3 mois, inflation déduite · États-Unis",
        "formule": "Taux réel court = taux des bons du Trésor à 3 mois − inflation des prix à la consommation sur 1 an",
        "axe_quadrant": "Axe inflation",
        "transposition": "Graphique central de la vidéo « Comment les banques centrales volent l'épargne des « petites fourmis » » (1970-2026, présidents de la Fed en surimpression). Notre version démarre en 2000 et ne reproduit ni les mandats ni les récessions.",
        "pedago": {
            "ce_que_dit_gave": "L'écran de la vidéo montre ce que rapporte le placement le plus sûr qui soit, le bon du Trésor à 3 mois, une fois l'inflation déduite. Sous Volcker et Greenspan, il est resté positif : l'épargnant s'enrichissait. Sous Burns, puis presque sans interruption de 2002 à 2022, il a été négatif. Les annotations parlent d'« euthanasie du rentier » : sans rien voir, la « petite fourmi » perd chaque année du pouvoir d'achat.",
            "comment_lire": "Au-dessus de zéro, le livret ou le fonds monétaire bat l'inflation. En dessous (zone ambre), l'épargne sans risque fond en silence : c'est un impôt caché, qui pousse vers les actifs réels (or, actions, immobilier). La ligne pointillée verte rappelle l'inflation sur un an.",
            "lien_quadrants": "Il éclaire l'axe inflation : un taux réel court négatif accompagne en général les cadrans inflationnistes ; nettement positif, il annonce plutôt la désinflation. Le baromètre le classe « neutre » entre 0 et 1 %.",
        },
        "sources": ["v_fourmis"],
    },
    {
        "id": "impot_energetique", "source_video": True, "calc": calc_impot_energetique,
        "titre": "L'« impôt énergétique »",
        "sous_titre": "Ce que le pétrole prélève sur l'économie, avec un an d'avance",
        "formule": "PIB nominal approché (PIB réel × indice des prix) / prix du baril WTI, base 100, décalé de 12 mois vers la droite ; comparé au S&P 500 rapporté à sa moyenne mobile 7 ans",
        "axe_quadrant": "Axe croissance (hors vote)",
        "transposition": "Version approchée du graphique de la vidéo « La CATASTROPHE que personne ne voit ! » : le CAPE de Shiller (38,9 à l'écran, chiffre affiché dans la vidéo, non actualisé) n'existe pas gratuitement ; nous le remplaçons par le S&P 500 rapporté à sa moyenne 7 ans, qui n'est pas un multiple de valorisation. Le PIB nominal est approché par le PIB réel multiplié par l'indice des prix à la consommation. Très proche de l'axe croissance du site (ACWI / pétrole) : il n'est donc pas compté dans le baromètre.",
        "pedago": {
            "ce_que_dit_gave": "La règle de lecture écrite à l'écran est simple : quand la ligne « PIB / prix du baril » baisse, c'est un impôt qui augmente, et suivent la baisse des actions et la récession ; quand elle monte, l'impôt baisse, les actions montent et la croissance revient. Le graphique de la vidéo couvre 1950-2026 et se termine par une flèche dessinée vers le bas jusqu'en 2030 : son auteur attend une baisse des valorisations.",
            "comment_lire": "Un baril cher agit comme une taxe que chaque ménage et chaque entreprise paie à la pompe. La courbe orange mesure la richesse produite par baril : quand elle monte, l'énergie pèse moins. Elle est décalée d'un an vers la droite : sa valeur d'aujourd'hui se lit sous la date de l'an prochain, comme une prévision à comparer à la courbe des actions.",
            "lien_quadrants": "C'est presque l'axe croissance du site (actions mondiales / pétrole) vu sous un autre angle : un « impôt » qui s'alourdit annonce le passage vers les cadrans « récession ». Pour ne pas compter deux fois le pétrole, il reste hors du baromètre.",
        },
        "sources": ["v_catastrophe"],
    },
    {
        "id": "actions_contre_obligations", "source_video": True, "calc": calc_actions_obligations,
        "titre": "Les actions contre les obligations",
        "sous_titre": "Ce que gagnent les détenteurs d'actions face aux épargnants en obligations · États-Unis",
        "formule": "Ratio = SPY (S&P 500) / IEF (Trésor 7-10 ans), cours ajustés (dividendes et coupons réinvestis), base 100, comparé à sa moyenne mobile 7 ans",
        "axe_quadrant": "Axe croissance",
        "transposition": "Approche du graphique de la vidéo « petites fourmis » (MSCI USA / indice du Trésor à 10 ans, depuis 1970). SPY et IEF sont les ETF les plus proches ; nous les préférons à ACWI / TLT car ils correspondent aux mêmes marchés que l'original et remontent à 2003 au lieu de 2008. Les bandes ambre marquent les mois de taux réel court négatif.",
        "pedago": {
            "ce_que_dit_gave": "L'annotation à l'écran est sans détour : quand les taux réels sont négatifs, les actions (détenues par les riches) écrasent les placements obligataires (détenus par les modestes). Le ratio de la vidéo passe d'environ 130 en 2009 à 1 200 en fin de graphique : selon cette annotation, l'argent trop bon marché creuse les écarts de patrimoine.",
            "comment_lire": "Quand la courbe monte, les actions rapportent plus que les obligations d'État, revenus compris. Au-dessus de sa moyenne 7 ans, l'économie est dans un régime de croissance ; en dessous, les obligations reprennent la main, ce qui accompagne souvent une récession. Comparez la pente de la courbe dans les bandes ambre et en dehors.",
            "lien_quadrants": "Les actions sont l'actif roi de la croissance déflationniste, les obligations celui de la récession déflationniste : ce ratio éclaire donc l'axe croissance.",
        },
        "sources": ["v_fourmis"],
    },
    {
        "id": "largeur_marche", "source_video": True, "calc": calc_largeur_marche,
        "titre": "Le remous sous la surface",
        "sous_titre": "La hausse de la bourse américaine est-elle large ou portée par quelques géants ?",
        "formule": "Ratio = RSP (S&P 500 à poids égaux) / SPY (S&P 500 pondéré par les capitalisations), base 100, comparé à sa moyenne mobile 12 mois",
        "axe_quadrant": "Axe croissance",
        "transposition": "Approximation : la vidéo « Actions américaines : le remous qu'on ne voit pas à la surface » montre la part des 500 titres au-dessus de leur moyenne mobile 50 jours (27,4 % à l'écran, chiffre affiché dans la vidéo, non actualisé), ce qui demande les cours quotidiens des 500 actions. Nous comparons plutôt l'indice où chaque titre pèse autant à l'indice officiel : la question est la même, la mesure diffère.",
        "pedago": {
            "ce_que_dit_gave": "La vidéo montre un S&P 500 proche de son record alors qu'environ trois titres sur quatre sont sous leur moyenne de court terme. L'indice, dominé par quelques géants, masque une baisse de la majorité des actions : c'est le « remous » qu'on ne voit pas à la surface.",
            "comment_lire": "Si le S&P 500 « à poids égaux » fait moins bien que l'indice officiel, la courbe baisse : la hausse repose sur peu de sociétés, ce qui la rend fragile. Une courbe qui remonte au-dessus de sa moyenne signale au contraire une hausse partagée par le plus grand nombre.",
            "lien_quadrants": "Il éclaire l'axe croissance côté solidité : une hausse étroite et qui se rétrécit précède souvent les phases de repli. Il ne dit rien de l'inflation.",
        },
        "sources": ["v_remous"],
    },
]


# ---------------------------------------------------------------- baromètre
QUADRANT_AXES = {
    "boom_inflationniste": (+1, +1), "boom_deflationniste": (+1, -1),
    "recession_inflationniste": (-1, +1), "depression_deflationniste": (-1, -1),
}
NOMS_Q = {
    "boom_inflationniste": "Croissance inflationniste", "boom_deflationniste": "Croissance déflationniste",
    "recession_inflationniste": "Récession inflationniste", "depression_deflationniste": "Récession déflationniste",
}


def barometre(indicateurs, quadrant_id):
    axes = {"croissance": {"pour": [], "contre": [], "neutre": []},
            "inflation": {"pour": [], "contre": [], "neutre": []}}
    hors_vote = []
    for ind in indicateurs:
        if not ind.get("disponible"):
            continue
        p = ind.get("penche") or {}
        if ind.get("vote") is False or p.get("axe") not in axes:
            hors_vote.append(ind["titre_court"])  # affiché sur le site, mais pas compté
            continue
        cle = {1: "pour", -1: "contre", 0: "neutre"}[p["sens"]]
        axes[p["axe"]][cle].append(ind["titre_court"])
    mod = QUADRANT_AXES.get(quadrant_id)
    res = {"axes": {}, "cadran_modele": NOMS_Q.get(quadrant_id), "cadran_modele_id": quadrant_id,
           "hors_vote": hors_vote,
           "nb_votants": sum(len(v) for a in axes.values() for v in a.values())}
    accords = 0
    for nom, (lib_p, lib_c) in (("croissance", ("croissance", "récession")),
                                ("inflation", ("inflation", "déflation"))):
        a = axes[nom]
        n_p, n_c = len(a["pour"]), len(a["contre"])
        sens = 1 if n_p > n_c else -1 if n_c > n_p else 0
        attendu = mod[0 if nom == "croissance" else 1] if mod else 0
        if sens == 0:
            verdict = "partagé"
        else:
            verdict = lib_p if sens > 0 else lib_c
        coherent = (sens == attendu) if sens else None
        if coherent:
            accords += 1
        res["axes"][nom] = {"pour": a["pour"], "contre": a["contre"], "neutre": a["neutre"],
                            "libelle_pour": lib_p, "libelle_contre": lib_c,
                            "sens": sens, "verdict": verdict, "coherent_modele": coherent}
    sg, si = res["axes"]["croissance"]["sens"], res["axes"]["inflation"]["sens"]
    q_ind = None
    if sg and si:
        q_ind = next(k for k, v in QUADRANT_AXES.items() if v == (sg, si))
    res["cadran_indicateurs"] = NOMS_Q.get(q_ind) if q_ind else None
    res["cadran_indicateurs_id"] = q_ind
    if q_ind and q_ind == quadrant_id:
        res["coherence"], res["couleur"] = "cohérent", VERT
        res["texte"] = (f"Les indicateurs de Gave penchent dans le même sens que le modèle : "
                        f"{NOMS_Q[quadrant_id].lower()}.")
    elif accords == 1 or (q_ind is None and accords >= 1):
        res["coherence"], res["couleur"] = "partiellement cohérent", AMBRE
        res["texte"] = ("Les indicateurs confirment le modèle sur un axe seulement : "
                        "à surveiller, une bascule se prépare souvent ainsi.")
    else:
        res["coherence"], res["couleur"] = "divergent", ROUGE
        res["texte"] = ("Les indicateurs de Gave contredisent le cadran du modèle : "
                        "signal de prudence, le marché pourrait être en train de tourner.")
    return res


# ---------------------------------------------------------------- point d'entrée
TITRES_COURTS = {
    "r_contre_g": "R contre G", "taux_reel_10a": "Taux réel", "or_contre_actions": "Or / actions",
    "stress_credit": "Spreads de crédit", "euro_dollar": "Euro / dollar",
    "energie_contre_obligations": "Énergie / obligations",
    "taux_reel_court": "Taux réel court", "impot_energetique": "Impôt énergétique",
    "actions_contre_obligations": "Actions / obligations", "largeur_marche": "Largeur du marché",
}
# Couleur de la zone entre la courbe et sa référence (courbe au-dessus, courbe en dessous).
# Code couleur des cadrans : vert = croissance, rouge = récession, ambre = inflation, bleu = déflation.
REMPLISSAGE = {
    "r_contre_g": (ROUGE, VERT), "taux_reel_10a": (BLEU, AMBRE), "or_contre_actions": (AMBRE, VERT),
    "stress_credit": (VERT, ROUGE), "euro_dollar": (BLEU, AMBRE), "energie_contre_obligations": (AMBRE, BLEU),
    "taux_reel_court": (BLEU, AMBRE), "impot_energetique": (VERT, ROUGE),
    "actions_contre_obligations": (VERT, ROUGE), "largeur_marche": (VERT, ROUGE),
}


def construire(cle=None, quadrant_id=None):
    """Renvoie (liste d'indicateurs, baromètre). Ne lève jamais d'exception."""
    try:
        actualiser(cle)
    except Exception as e:  # ceinture et bretelles
        log(f"actualisation impossible ({e})")
    ctx = {}
    out = []
    for d in DEFINITIONS:
        base = {k: v for k, v in d.items() if k not in ("calc", "sources")}
        base["titre_court"] = TITRES_COURTS[d["id"]]
        base["sources"] = [SRC[s] for s in d["sources"]]
        try:
            base.update(d["calc"](ctx))
            base["detail"] = fr_txt(base.get("detail", ""))
            dessus, dessous = REMPLISSAGE[d["id"]]
            base["series"]["remplissage_dessus"] = dessus
            base["series"]["remplissage_dessous"] = dessous
            base["disponible"] = True
            log(f"{d['id']}: {base['valeur_actuelle']} ({base['mois']}) — {base['signal']}")
        except Exception as e:
            log(f"{d['id']}: indisponible ({type(e).__name__}: {e})")
            traceback.print_exc(limit=2)
            base.update({"disponible": False, "signal": "Données indisponibles pour le moment",
                         "couleur": GRIS, "valeur_actuelle": None, "series": None,
                         "penche": {"axe": None, "sens": 0}})
        out.append(base)
    try:
        baro = barometre(out, quadrant_id)
    except Exception as e:
        log(f"baromètre indisponible ({e})")
        baro = None
    return out, baro


if __name__ == "__main__":
    inds, baro = construire(os.environ.get("ALPHAVANTAGE_KEY", "").strip() or None, "boom_inflationniste")
    print(json.dumps(baro, ensure_ascii=False, indent=2))
