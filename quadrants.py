#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Les 4 Quadrants de Charles Gave — version cloud (GitHub Actions)
================================================================
Chaque soir :
1. Télécharge ACWI, GLD, TLT (mensuel ajusté) et WTI (mensuel) chez Alpha Vantage.
2. Calcule les deux ratios et leur moyenne mobile 7 ans (84 mois) → quadrant.
3. Backteste 4 portefeuilles depuis 2016 (dont la stratégie de Gave).
4. Régénère le site (docs/index.html) + history.csv + state.json,
   avec la section « Les indicateurs de Charles Gave » (module indicateurs.py).
5. Envoie un EMAIL si changement de quadrant (si secrets configurés).

Secrets attendus (variables d'environnement) :
  ALPHAVANTAGE_KEY    (obligatoire)
  MAIL_USER           (optionnel — adresse Gmail expéditrice)
  MAIL_APP_PASSWORD   (optionnel — mot de passe d'application Gmail)
  (MAIL_TO n'est plus utilisé : les destinataires sont reconstruits depuis la
   boîte Gmail à chaque exécution — voir « abonnés & email » ci-dessous.)

Règle du portefeuille « Gave » (P2), déduite du schéma des 4 cadrans :
chaque cadran a son actif roi (CI→or, CD→actions, RD→obligations, RI→cash) ;
l'actif à RETIRER est l'actif roi du cadran diagonalement opposé.
À chaque croisement ratio/MM7a qui change le cadran, l'actif exclu est vendu
et son produit réparti à parts égales entre les trois autres.
"""
import json, os, sys, csv, time, urllib.request, urllib.parse
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
    for k, (fname, url) in enumerate(jobs):
        path = os.path.join(DATA, fname)
        if k:
            time.sleep(12)  # limite de débit Alpha Vantage (indicateurs.py enchaîne ensuite, 12 s aussi)
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
        try:
            v = float(row.get("5. adjusted close") or row.get("4. close"))
        except (ValueError, TypeError):
            continue  # valeur "." ou vide : mois ignoré
        if v > 0:
            out[d[:7]] = v
    return out


def parse_wti(path):
    with open(path, encoding="utf-8") as f:
        j = json.load(f)
    out = {}
    for row in j.get("data", []):
        try:
            v = float(row["value"])
        except (ValueError, KeyError, TypeError):
            continue
        if v > 0:  # évite une division par zéro dans le ratio ACWI / WTI
            out[row["date"][:7]] = v
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
# La liste des abonnés n'est PLUS stockée dans le dépôt (public) : elle est
# reconstruite à chaque exécution en relisant, via IMAP, tous les messages de la
# boîte Gmail dont l'objet contient l'un des marqueurs ci-dessous (lus ou non,
# archivés ou non), appliqués dans l'ordre chronologique (le dernier gagne).
#
# Règles :
#  - ABONNEMENT-QUADRANTS : abonne l'EXPÉDITEUR du message. Si le corps contient
#    une autre adresse, celle-ci n'est PAS abonnée : elle reçoit (une seule fois)
#    une invitation à confirmer elle-même son inscription (double opt-in).
#  - STOP-QUADRANTS : désabonne l'EXPÉDITEUR uniquement.
#  - Message « de confiance » = envoyé par MAIL_USER à lui-même : les adresses
#    présentes dans le corps sont appliquées telles quelles (administration
#    manuelle de la liste, messages de migration).
#  - CONFIRMER-QUADRANTS (message de MAIL_USER à lui-même) : trace des invitations
#    déjà envoyées (adresses dans le corps) — évite tout second envoi.
#  - Le propriétaire (MAIL_USER) est toujours abonné.
SUBS_PATH = os.path.join(BASE, "subscribers.json")  # ancien fichier : lu uniquement pour la migration
SUBJECT_SUB = "ABONNEMENT-QUADRANTS"
SUBJECT_UNSUB = "STOP-QUADRANTS"
SUBJECT_INVIT = "CONFIRMER-QUADRANTS"
MAX_INVITATIONS_PAR_PASSAGE = 10

_EMAIL_RE = r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}"
_CARNET = None          # cache mémoire : la boîte IMAP n'est lue qu'une fois par exécution
_INBOX_TRAITEE = False  # process_inbox() n'agit qu'une fois par exécution


def mail_creds():
    user = os.environ.get("MAIL_USER", "").strip()
    pwd = os.environ.get("MAIL_APP_PASSWORD", "").strip()
    return (user, pwd) if user and pwd else (None, None)


def adresse_valide(a):
    import re
    return bool(a) and len(a) <= 254 and re.fullmatch(_EMAIL_RE, a) is not None


def masquer(a):
    """Adresse partiellement masquée pour les journaux (les logs GitHub Actions
    d'un dépôt public sont lisibles par tous)."""
    try:
        local, dom = a.split("@", 1)
        return f"{local[:1]}***@{dom}"
    except Exception:
        return "***"


def _adresses_du_corps(texte, exclure):
    import re
    vues = []
    for a in re.findall(_EMAIL_RE, texte or ""):
        a = a.lower().rstrip(".")
        if a not in exclure and adresse_valide(a) and a not in vues:
            vues.append(a)
    return vues


def _texte_du_message(msg):
    try:
        part = msg.get_body(preferencelist=("plain", "html"))
        if part is not None:
            return part.get_content()
    except Exception:
        pass
    for part in msg.walk():  # repli pour les messages mal formés
        if part.get_content_maintype() == "text":
            payload = part.get_payload(decode=True) or b""
            return payload.decode("utf-8", "ignore")
    return ""


def _authentification_douteuse(msg):
    """True si Gmail a signalé un échec d'authentification (expéditeur usurpé)."""
    ar = " ".join(str(h) for h in (msg.get_all("Authentication-Results") or [])).lower()
    if not ar:
        return False
    if "dmarc=fail" in ar:
        return True
    return "spf=pass" not in ar and "dkim=pass" not in ar


def _dossier_tous_messages(box):
    """Nom IMAP (déjà entre guillemets) du dossier « Tous les messages » de Gmail
    (attribut \\All, nom localisé) ; None si introuvable."""
    import re
    try:
        ok, lignes = box.list()
    except Exception as e:
        print(f"IMAP: LIST impossible ({e})")
        return None
    if ok != "OK":
        return None
    for ligne in lignes or []:
        if isinstance(ligne, tuple):
            ligne = b"".join(x for x in ligne if isinstance(x, bytes))
        if isinstance(ligne, bytes):
            ligne = ligne.decode("utf-8", "ignore")
        m = re.match(r'\((?P<flags>[^)]*)\)\s+(?:"[^"]*"|NIL)\s+(?P<nom>.+)$', ligne or "")
        if m and "\\all" in m.group("flags").lower().split():
            nom = m.group("nom").strip()
            return nom if nom.startswith('"') else f'"{nom}"'
    return None


def _lire_evenements(user, pwd):
    """Lit la boîte via IMAP et renvoie la liste des messages pertinents, triés par date."""
    import imaplib, email as email_mod
    from email import policy
    from email.utils import parseaddr, getaddresses, parsedate_to_datetime
    from datetime import datetime, timezone

    box = imaplib.IMAP4_SSL("imap.gmail.com", timeout=30)
    try:
        box.login(user, pwd)
        dossier = _dossier_tous_messages(box)
        if dossier:
            ok, _ = box.select(dossier, readonly=True)
            if ok != "OK":
                print("IMAP: dossier « Tous les messages » inaccessible — repli sur INBOX.")
                dossier = None
        if not dossier:
            print("IMAP: dossier « Tous les messages » introuvable — repli sur INBOX "
                  "(les messages archivés ou envoyés ne seront pas vus).")
            ok, _ = box.select("INBOX", readonly=True)
            if ok != "OK":
                raise RuntimeError("sélection de INBOX impossible")

        nums = set()
        for token in (SUBJECT_SUB, SUBJECT_UNSUB, SUBJECT_INVIT):
            ok, ids = box.search(None, "SUBJECT", f'"{token}"')
            if ok != "OK":
                raise RuntimeError(f"recherche IMAP « {token} » refusée")
            for n in (ids[0] or b"").split():
                nums.add(n)

        evts = []
        moi = user.lower()
        epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
        for num in sorted(nums, key=lambda n: int(n)):
            try:
                ok, data = box.fetch(num, "(BODY.PEEK[])")  # PEEK : ne marque pas comme lu
                if ok != "OK":
                    continue
                raw = next((x[1] for x in data if isinstance(x, tuple) and len(x) > 1), None)
                if not raw:
                    continue
                msg = email_mod.message_from_bytes(raw, policy=policy.default)
                sujet = str(msg.get("Subject", "") or "").upper()
                if SUBJECT_UNSUB in sujet:
                    action = "remove"
                elif SUBJECT_SUB in sujet:
                    action = "add"
                elif SUBJECT_INVIT in sujet:
                    action = "invit"
                else:
                    continue
                expediteur = parseaddr(str(msg.get("From", "")))[1].lower()
                dests = [a.lower() for _, a in getaddresses(
                    [str(h) for h in (msg.get_all("To") or []) + (msg.get_all("Cc") or [])]) if a]
                confiance = expediteur == moi and bool(dests) and all(d == moi for d in dests)
                try:
                    quand = parsedate_to_datetime(str(msg.get("Date")))
                    if quand.tzinfo is None:
                        quand = quand.replace(tzinfo=timezone.utc)
                except Exception:
                    quand = epoch
                evts.append({
                    "num": int(num), "date": quand, "action": action,
                    "expediteur": expediteur, "confiance": confiance,
                    "douteux": _authentification_douteuse(msg),
                    "corps": _adresses_du_corps(_texte_du_message(msg), {moi}),
                })
            except Exception as e:
                print(f"IMAP: message {num!r} ignoré ({type(e).__name__}: {e})")
        evts.sort(key=lambda e: (e["date"], e["num"]))
        return evts
    finally:
        try:
            box.logout()
        except Exception:
            pass


def _reconstruire(user, pwd):
    """Rejoue les messages dans l'ordre chronologique → carnet d'abonnés."""
    moi = user.lower()
    evts = _lire_evenements(user, pwd)
    abonnes, connues, invitees, candidates = set(), set(), set(), []
    for e in evts:
        if e["douteux"]:
            print("IMAP: message ignoré (authentification de l'expéditeur en échec).")
            continue
        if e["confiance"]:
            # message du propriétaire à lui-même : adresses du corps appliquées telles quelles
            for a in e["corps"]:
                if e["action"] == "invit":
                    invitees.add(a)
                else:
                    connues.add(a)
                    (abonnes.add if e["action"] == "add" else abonnes.discard)(a)
            continue
        if e["expediteur"] == moi or e["action"] == "invit":
            continue  # message du propriétaire à un tiers, ou marqueur d'un tiers : ignoré
        exp = e["expediteur"]
        if not adresse_valide(exp):
            continue
        connues.add(exp)
        if e["action"] == "add":
            abonnes.add(exp)
            for a in e["corps"]:
                if a != exp and a not in candidates:
                    candidates.append(a)  # adresse tierce : invitation, jamais d'abonnement direct
        else:
            abonnes.discard(exp)  # STOP : seul l'expéditeur est désabonné
    abonnes.discard(moi)
    return {"ok": True, "abonnes": abonnes, "connues": connues,
            "invitees": invitees, "candidates": candidates}


def _carnet():
    global _CARNET
    if _CARNET is not None:
        return _CARNET
    user, pwd = mail_creds()
    if not user:
        _CARNET = {"ok": False, "abonnes": set(), "connues": set(), "invitees": set(), "candidates": []}
        return _CARNET
    try:
        _CARNET = _reconstruire(user, pwd)
        print(f"IMAP: liste des abonnés reconstruite depuis la boîte Gmail "
              f"({len(_CARNET['abonnes']) + 1} destinataire(s), propriétaire compris).")
    except Exception as e:
        print(f"IMAP: lecture de la boîte impossible ({type(e).__name__}: {e}) — "
              f"liste des abonnés indisponible : repli sur le seul propriétaire.")
        _CARNET = {"ok": False, "abonnes": set(), "connues": set(), "invitees": set(), "candidates": []}
    return _CARNET


def load_subscribers():
    """Liste des destinataires (propriétaire toujours inclus). Sans identifiants : [].
    IMAP indisponible : [propriétaire] seulement (jamais une liste vide par erreur)."""
    user, _ = mail_creds()
    if not user:
        return []
    c = _carnet()
    return [user.lower()] + sorted(c["abonnes"] - {user.lower()})


def _smtp_envoyer(messages):
    """messages : liste de (destinataires, MIME). Renvoie une liste de booléens."""
    user, pwd = mail_creds()
    if not user or not messages:
        return [False] * len(messages)
    import smtplib
    res = []
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
            s.login(user, pwd)
            for dests, msg in messages:
                try:
                    s.sendmail(user, dests, msg.as_string())
                    res.append(True)
                except Exception as e:
                    print(f"SMTP: échec d'un envoi ({type(e).__name__}: {e})")
                    res.append(False)
    except Exception as e:
        print(f"SMTP: connexion impossible ({type(e).__name__}: {e})")
    return res + [False] * (len(messages) - len(res))


def _note_admin(user, sujet, texte):
    from email.mime.text import MIMEText
    from email.utils import formatdate, make_msgid
    m = MIMEText(texte, "plain", "utf-8")
    m["Subject"] = sujet
    m["From"] = user
    m["To"] = user
    m["Date"] = formatdate(localtime=False)
    m["Message-ID"] = make_msgid(domain="quadrants.local")
    return m


def _invitation(user, adresse):
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart
    from email.utils import formatdate, make_msgid
    import urllib.parse as up
    lien = (f"mailto:{user}?subject={SUBJECT_SUB}&body="
            + up.quote("Je confirme mon inscription aux alertes des 4 Quadrants."))
    texte = (
        "Bonjour,\n\n"
        f"Une inscription aux alertes « Les 4 Quadrants » a été demandée pour l'adresse {adresse}.\n"
        "Pour la confirmer, envoie un email (même vide) depuis cette adresse à "
        f"{user} avec l'objet {SUBJECT_SUB}.\n\n"
        "Si tu n'es pas à l'origine de cette demande, ignore simplement ce message : "
        "tu ne recevras rien d'autre.\n")
    html = (
        '<div style="font-family:Arial,sans-serif;max-width:560px;margin:0 auto;color:#0f172a">'
        '<h2 style="margin:0 0 12px">Les 4 Quadrants : confirmer ton inscription</h2>'
        f'<p style="line-height:1.5">Une inscription aux alertes « Les 4 Quadrants » (alerte de changement '
        f'de cadran et news hebdomadaire) a été demandée pour l\'adresse <b>{adresse}</b>.</p>'
        '<p style="line-height:1.5">Pour la confirmer, clique sur le bouton ci-dessous puis appuie sur '
        '« Envoyer » dans ton application mail. L\'inscription sera prise en compte à la mise à jour suivante.</p>'
        f'<p><a href="{lien}" style="display:inline-block;padding:10px 18px;background:#f59e0b;color:#0f172a;'
        'text-decoration:none;border-radius:8px;font-weight:700">Confirmer mon inscription</a></p>'
        '<p style="font-size:13px;color:#64748b;line-height:1.5">Si tu n\'es pas à l\'origine de cette demande, '
        'ignore simplement ce message : tu ne recevras rien d\'autre et aucune adresse n\'a été enregistrée.</p>'
        '</div>')
    m = MIMEMultipart("alternative")
    m.attach(MIMEText(texte, "plain", "utf-8"))
    m.attach(MIMEText(html, "html", "utf-8"))
    m["Subject"] = "Les 4 Quadrants : confirme ton inscription"
    m["From"] = f"Les 4 Quadrants <{user}>"
    m["To"] = adresse
    m["Date"] = formatdate(localtime=False)
    m["Message-ID"] = make_msgid(domain="quadrants.local")
    return m


def _migrer_ancien_fichier(user, c):
    """Migration unique : les adresses de l'ancien subscribers.json inconnues de la
    boîte sont inscrites par un message administratif (MAIL_USER → MAIL_USER),
    puis le fichier est supprimé (si tous les envois ont réussi)."""
    if not os.path.exists(SUBS_PATH):
        return
    try:
        with open(SUBS_PATH, encoding="utf-8") as f:
            anciens = json.load(f).get("abonnes", [])
    except Exception as e:
        print(f"Migration : subscribers.json illisible ({e}) — fichier conservé, à vérifier à la main.")
        return
    moi = user.lower()
    a_migrer = []
    for a in anciens:
        a = str(a).strip().lower()
        if adresse_valide(a) and a != moi and a not in c["connues"] and a not in a_migrer:
            a_migrer.append(a)
    if a_migrer:
        msgs = [([user], _note_admin(
            user, f"{SUBJECT_SUB} (migration)",
            f"Message automatique de migration de l'ancien fichier subscribers.json.\n"
            f"Adresse abonnée : {a}\n")) for a in a_migrer]
        res = _smtp_envoyer(msgs)
        if not all(res):
            print(f"Migration : {res.count(False)} envoi(s) sur {len(res)} en échec — "
                  f"subscribers.json conservé, nouvel essai au prochain passage.")
            for a, ok in zip(a_migrer, res):  # les envois réussis comptent déjà pour ce passage
                if ok:
                    c["abonnes"].add(a)
                    c["connues"].add(a)
            return
        for a in a_migrer:
            c["abonnes"].add(a)
            c["connues"].add(a)
    try:
        os.remove(SUBS_PATH)
        print(f"Migration : {len(a_migrer)} adresse(s) transférée(s) dans la boîte Gmail ; "
              f"subscribers.json supprimé du dépôt.")
    except Exception as e:
        print(f"Migration : suppression de subscribers.json impossible ({e}).")


def _envoyer_invitations(user, c):
    moi = user.lower()
    a_inviter = [a for a in c["candidates"]
                 if a != moi and a not in c["abonnes"] and a not in c["connues"]
                 and a not in c["invitees"]][:MAX_INVITATIONS_PAR_PASSAGE]
    for a in a_inviter:
        ok_inv, ok_note = (_smtp_envoyer([
            ([a], _invitation(user, a)),
            ([user], _note_admin(user, f"{SUBJECT_INVIT} (invitation envoyée)",
                                 f"Trace automatique : invitation à confirmer envoyée à {a}\n")),
        ]) + [False, False])[:2]
        if ok_inv:
            c["invitees"].add(a)
            print(f"Invitation de confirmation envoyée à {masquer(a)}.")
            if not ok_note:
                print("ATTENTION : trace de l'invitation non enregistrée (elle pourrait être renvoyée).")
        else:
            print(f"Invitation pour {masquer(a)} non envoyée — nouvel essai au prochain passage.")


def process_inbox():
    """Reconstruit la liste des abonnés depuis la boîte Gmail, effectue la migration
    de l'ancien subscribers.json et envoie les invitations de confirmation en attente.
    N'agit qu'une fois par exécution ; ne plante jamais."""
    global _INBOX_TRAITEE
    if _INBOX_TRAITEE:
        return
    _INBOX_TRAITEE = True
    user, _ = mail_creds()
    if not user:
        print("IMAP: identifiants absents — traitement des abonnements sauté.")
        return
    c = _carnet()
    if not c["ok"]:
        print("IMAP: boîte illisible — migration et invitations reportées au prochain passage.")
        return
    try:
        _migrer_ancien_fichier(user, c)
    except Exception as e:
        print(f"Migration : erreur ({type(e).__name__}: {e}) — fichier conservé.")
    try:
        _envoyer_invitations(user, c)
    except Exception as e:
        print(f"Invitations : erreur ({type(e).__name__}: {e}).")


def unsubscribe_footer(user):
    return (
        f'<hr style="border:none;border-top:1px solid #e2e8f0;margin:24px 0 12px">'
        f'<p style="font-size:12px;color:#64748b;line-height:1.5">'
        f'Cadre théorique de Charles Gave — ne constitue pas un conseil en investissement personnalisé.<br>'
        f'<a href="mailto:{user}?subject={SUBJECT_UNSUB}&body=Merci%20de%20me%20d%C3%A9sabonner." '
        f'style="display:inline-block;margin-top:8px;padding:8px 16px;background:#f1f5f9;'
        f'color:#334155;text-decoration:none;border-radius:8px;font-weight:600">Se désinscrire</a></p>'
    )


def send_email(subject, html, recipients=None, images=None):
    """Envoie en copie cachée à tous les abonnés (ou à la liste fournie).
    images : dict optionnel {cid: chemin_png} — images intégrées au message
    (multipart/related), à référencer dans le HTML par src="cid:<cid>"."""
    user, pwd = mail_creds()
    if not user:
        print("Email non configuré (MAIL_USER / MAIL_APP_PASSWORD absents) — pas d'envoi.")
        return False
    if recipients is None:
        recipients = load_subscribers()
        if not _carnet()["ok"]:
            print("Repli : envoi au seul propriétaire (liste des abonnés indisponible).")
    recipients = [r for r in dict.fromkeys(str(x).strip().lower() for x in recipients) if r]
    if not recipients:
        print("Aucun abonné — pas d'envoi.")
        return False
    import smtplib
    from email.mime.text import MIMEText
    body = MIMEText(html + unsubscribe_footer(user), "html", "utf-8")
    if images:
        from email.mime.multipart import MIMEMultipart
        from email.mime.image import MIMEImage
        msg = MIMEMultipart("related")
        msg.attach(body)
        for cid, path in images.items():
            try:
                with open(path, "rb") as f:
                    img = MIMEImage(f.read(), _subtype="png")
                img.add_header("Content-ID", f"<{cid}>")
                img.add_header("Content-Disposition", "inline", filename=os.path.basename(path))
                msg.attach(img)
            except Exception as e:
                print(f"Image « {cid} » non jointe ({e})")
    else:
        msg = body
    msg["Subject"] = subject
    msg["From"] = f"Les 4 Quadrants <{user}>"
    msg["To"] = user  # les abonnés sont en copie cachée (enveloppe SMTP uniquement)
    dests = list(dict.fromkeys([user.lower()] + recipients))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
            s.login(user, pwd)
            s.sendmail(user, dests, msg.as_string())
        print(f"Email « {subject} » envoyé à {len(dests)} destinataire(s) en copie cachée.")
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

    try:
        acwi = parse_monthly_adjusted(os.path.join(DATA, "acwi_monthly.json"))
        gld = parse_monthly_adjusted(os.path.join(DATA, "gld_monthly.json"))
        tlt = parse_monthly_adjusted(os.path.join(DATA, "tlt_monthly.json"))
        wti = parse_wti(os.path.join(DATA, "wti_monthly.json"))
        months = sorted(set(acwi) & set(gld) & set(tlt) & set(wti))
        if len(months) < MA_MONTHS + 1:
            raise ValueError(f"seulement {len(months)} mois communs (il en faut au moins {MA_MONTHS + 1})")
    except Exception as e:
        print(f"ERREUR BLOQUANTE : données ACWI / GLD / TLT / WTI manquantes ou illisibles dans data/ "
              f"({type(e).__name__}: {e}). Site, état et alertes inchangés ; le fichier sera "
              f"re-téléchargé au prochain passage (ou à restaurer depuis l'historique Git).")
        return 1

    process_inbox()  # liste des abonnés (IMAP), migration, invitations de confirmation
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
        try:
            with open(state_path, encoding="utf-8") as f:
                prev_state = json.load(f)
        except Exception as e:
            print(f"state.json illisible ({e}) — considéré comme absent (pas d'alerte ce soir).")
        if not isinstance(prev_state, dict):
            prev_state = None
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
    # Indicateurs de Charles Gave (section pédagogique) — ne bloque jamais la mise à jour
    indicateurs, barometre = [], None
    try:
        import indicateurs as IND
        indicateurs, barometre = IND.construire(key or None, last["quadrant_id"])
    except Exception as e:
        print(f"Indicateurs de Gave indisponibles ({type(e).__name__}: {e}) — site généré sans cette section.")

    payload = {"state": state, "rows": rows, "transitions": transitions,
               "quadrants": {q["id"]: q for q in QUADRANTS.values()},
               "backtest": bt, "indicateurs": indicateurs, "barometre": barometre}
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
            <p><b>{prev_state.get('quadrant') or prev_state['quadrant_id']}</b> &rarr; <b>{last['quadrant']}</b> (mois : {last['mois']})</p>
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
    sys.exit(main() or 0)
