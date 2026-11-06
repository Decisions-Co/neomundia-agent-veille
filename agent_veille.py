"""
Agent de veille NEOMUNDIA — dispositif déterministe (workflow, pas agentique).

Chaque semaine : interroge Claude avec l'outil web_search pour chacun des 6
domaines de veille Qualiopi (critère 6, indicateurs 23 à 26), extrait les
actualités réellement nouvelles et substantielles, et les transmet au
Google Sheet "Veille NEOMUNDIA" via un webhook Make.com.

Chaque ligne créée porte le statut "À valider" (fixé dans le mapping Make,
pas ici) — aucune ligne ne compte comme preuve officielle avant relecture
humaine. Ce dispositif ne décide jamais seul : il propose.
"""

import os
import re
import json
import hashlib
import unicodedata
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo
from datetime import date, datetime, timedelta

import requests

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"].strip()
MAKE_WEBHOOK_URL = os.environ["MAKE_WEBHOOK_URL"].strip()
MAKE_API_KEY = os.environ["MAKE_API_KEY"].strip()

MODEL = "claude-sonnet-4-6"
HISTORIQUE_PATH = "historique_veille.json"

# Correspondance Domaine -> Indicateur Qualiopi (critère 6), calculée ici plutôt que
# par une formule dans le Sheet : Make écrase les formules pré-remplies à chaque
# nouvelle ligne insérée, donc la valeur doit être une donnée écrite comme les autres.
INDICATEURS_QUALIOPI = {
    "Réglementaire": "Indicateur 23",
    "Métier": "Indicateur 24",
    "Pédagogique": "Indicateur 25",
    "Technologique": "Indicateur 25",
    "Handicap": "Indicateur 26",
    # Rattachement de la veille juridique ; les obligations opérationnelles sont
    # précisées dans l’impact et l’action, sans créer un nouvel indicateur.
    "Violences sexistes et sexuelles": "Indicateur 23",
}

# Un domaine = une recherche ciblée, pas une recherche générique unique.
# Correspondance exacte avec les indicateurs Qualiopi (critère 6) :
# Réglementaire -> 23, Métier -> 24, Pédagogique/Technologique -> 25, Handicap -> 26.
DOMAINES = [
    {
        "nom": "Réglementaire",
        "requete": "actualité réglementation formation professionnelle France décret Légifrance",
    },
    {
        "nom": "Métier",
        "requete": "Impact concret de l’IA générative sur les tâches, métiers et compétences en entreprise : industrie, commerce, services, logistique, fonctions support. Alterner secteurs et fonctions. Privilégier les observatoires de branche, France Stratégie, Dares, Apec et études documentées.",
    },
    {
        "nom": "Pédagogique",
        "requete": "Centre Inffo actualité ingénierie pédagogique formation professionnelle FEST FOAD",
    },
    {
        "nom": "Technologique",
        "requete": "intelligence artificielle outils numériques formation professionnelle innovation",
    },
    {
        "nom": "Handicap",
        "requete": "Agefiph FIPHFP accessibilité handicap formation professionnelle réglementation",
    },
    {
        "nom": "Violences sexistes et sexuelles",
        "requete": (
            "violences sexistes et sexuelles formation professionnelle ; "
            "référent violences sexistes et sexuelles organisme de formation ; "
            "harcèlement sexuel stagiaire ; Qualiopi violences discriminations ; "
            "prévention et traitement des signalements dans le cadre de la formation. "
            "Priorité aux publications du ministère du Travail (travail-emploi.gouv.fr), "
            "de Légifrance (legifrance.gouv.fr), de Centre Inffo (centre-inffo.fr) "
            "et de France compétences (francecompetences.fr). "
            "Distinguer les obligations réglementaires des recommandations ; "
            "retenir les mesures applicables aux formations B2B intra-entreprise "
            "et les dates d'entrée en vigueur vérifiées dans les textes sources."
        ),
    },
]

CONTEXTE_NEOMUNDIA = (
    "Neomundia, pôle formation de Décisions & Co, dispense des formations IA sur mesure, "
    "intra-entreprise, en présentiel chez le client ou à distance, non certifiantes, "
    "pour des entreprises de tous secteurs. Les formateurs sont internes, sans sous-traitance. "
    "Exclure les sujets centrés sur CPF, EDOF, Mon Compte Formation, apprentissage, NPEC, CFA, "
    "RNCP, Répertoire spécifique, titres professionnels, VAE, France Travail, demandeurs d’emploi, "
    "Transitions Pro et PTP. Un texte transversal ne passe que si une disposition s’applique "
    "directement à Neomundia. Pour le domaine Métier, préciser la tâche ou compétence qui évolue "
    "et une implication concrète pour les formations."
)

SYSTEM_PROMPT = f"""Tu es l'agent de veille de NEOMUNDIA. Ta mission : identifier UNIQUEMENT
les actualités réellement nouvelles et substantielles du domaine assigné, parues dans les
7 derniers jours — jamais un résumé générique du secteur, jamais une actualité déjà ancienne
ou spéculative.

{CONTEXTE_NEOMUNDIA}

Pour chaque actualité pertinente trouvée (0 à 2 maximum — la rareté est normale, ne force
jamais une entrée artificielle si rien de substantiel n'est paru), fournis un résumé factuel,
sans opinion ni exagération. Utilise une publication précise, consultable, jamais une page
d’accueil. Privilégie la source primaire. Impact et action doivent être renseignés.
Préfère un résumé sans chiffres. Pour tout chiffre, donne une citation exacte de la source
qui établit le chiffre et son contexte. Indique la date exacte de publication de la source.
Pour un événement ou appel à projets, renseigne l’échéance et cite le passage qui l’établit.
Écarte les échéances passées et les rappels sans nouveauté substantielle.
Pour un même sujet, utilise une identité stable, indépendante du titre et du lien.
Pour une évolution réellement nouvelle, précise la mesure et ce qui change.

Réponds UNIQUEMENT avec un objet JSON valide, sans aucun texte avant ou après, au format exact :
{{"items": [
  {{
    "source": "nom du site ou de la publication",
    "organisme": "organisme émetteur (ex: Agefiph, Légifrance, France Compétences, Centre Inffo...)",
    "lien": "URL directe de la source",
    "identite": "identité stable : numéro de texte ou nom de la mesure, de l’événement ou de l’appel et édition si pertinente",
    "publication": "AAAA-MM-JJ",
    "type": "actualite, evenement ou appel",
    "echeance": "AAAA-MM-JJ pour un événement ou appel, sinon vide",
    "preuve": "extrait exact de la page justifiant les chiffres et l’échéance, sinon vide",
    "contenu": "résumé factuel en une phrase",
    "impact": "impact concret pour NEOMUNDIA, une à deux phrases",
    "diffusion": "Oui ou Non — Oui si les formateurs ou l'équipe pédagogique doivent en avoir connaissance",
    "action": "action concrète recommandée, ou Aucune action requise dans l'immédiat"
  }}
]}}

Si rien de nouveau et substantiel n'est trouvé pour ce domaine, réponds {{"items": []}}.

RÈGLE ABSOLUE : ta réponse finale doit être EXCLUSIVEMENT l'objet JSON ci-dessus, sans
aucune phrase avant, après, ou à la place — même si tu n'as rien trouvé, même si tu as des
observations intéressantes à partager sur pourquoi tu n'as rien trouvé (ex. un organisme en
liquidation, une actualité trop ancienne). Ces observations n'ont pas leur place dans la
réponse finale. Si tu as besoin de raisonner, fais-le, mais la dernière chose que tu écris
doit être uniquement {{"items": [...]}} ou {{"items": []}}, rien d'autre.
"""


def charger_historique():
    if os.path.exists(HISTORIQUE_PATH):
        with open(HISTORIQUE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"liens_vus": []}


def sauver_historique(historique):
    with open(HISTORIQUE_PATH, "w", encoding="utf-8") as f:
        json.dump(historique, f, ensure_ascii=False, indent=2)


def aujourd_hui():
    return datetime.now(ZoneInfo("Europe/Paris")).date()


def normaliser(texte):
    texte = unicodedata.normalize("NFKD", texte.casefold())
    texte = "".join(c for c in texte if not unicodedata.combining(c))
    return " ".join(re.findall(r"\w+", texte))


def empreinte(texte):
    return hashlib.sha256(texte.encode("utf-8")).hexdigest()


def url_precise(lien):
    url = urlsplit(lien)
    if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
        return ""
    if not url.path.strip("/"):
        return ""
    return lien


def url_normalisee(lien):
    url = urlsplit(lien)
    query = urlencode(sorted(
        (k, v) for k, v in parse_qsl(url.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid"}
    ))
    return urlunsplit(("", url.netloc.lower().removeprefix("www."), url.path.rstrip("/"), query, ""))


def cles_sujet(item):
    lien = item["lien"]
    contenu = normaliser(item["contenu"])
    identite = normaliser(item.get("identite", ""))
    cles = {"url:" + url_normalisee(lien), "texte:" + contenu}
    if identite:
        cles.add("sujet:" + identite)
    texte = identite + " " + contenu
    decret = re.search(r"\bdecret\s+(?:n\s+)?(20\d{2})\s+(\d+)\b", texte)
    if decret:
        cles.add("decret:" + "-".join(decret.groups()))
    if "impulsion ia" in texte:
        cles.add("plan:impulsion ia")
    if "ai act" in texte or "reglement europeen sur l ia" in texte:
        article = re.search(r"\barticle (\d+)\b", texte)
        cles.add("ai act:" + (article[1] if article else "general"))
    return {empreinte(cle) for cle in cles}


class TexteHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.masque = False
        self.parties = []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.masque = True

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.masque = False

    def handle_data(self, data):
        if not self.masque:
            self.parties.append(data)


def chiffres(texte):
    texte = re.sub(r"(?<=\d)[ \u00a0\u202f](?=\d{3}\b)", "", texte)
    return set(re.findall(r"\d+(?:[.,]\d+)?", texte.replace(",", ".")))


def valider_source(item, jour):
    for champ in ("source", "organisme", "lien", "contenu", "impact", "action", "identite"):
        if not isinstance(item.get(champ), str) or not item[champ].strip():
            return "champ manquant : " + champ

    publication_brute = item.get("publication")
    try:
        publication = date.fromisoformat(publication_brute)
    except (ValueError, TypeError):
        return f"date de publication invalide : {publication_brute!r}"[:200]
    debut = jour - timedelta(days=7)
    if not debut <= publication <= jour:
        return f"publication hors période : {publication} (du {debut} au {jour})"
    if item.get("type") not in {"actualite", "evenement", "appel"}:
        return "type invalide"

    echeance = None
    if item["type"] in {"evenement", "appel"}:
        echeance_brute = item.get("echeance")
        try:
            echeance = date.fromisoformat(echeance_brute)
        except (ValueError, TypeError):
            return f"date d’échéance invalide : {echeance_brute!r}"[:200]
        if echeance < jour:
            return f"échéance passée : {echeance} (date du jour : {jour})"

    lien = item["lien"].strip()
    try:
        if not url_precise(lien):
            return "lien non précis"
        with requests.get(lien, timeout=12, stream=True) as reponse:
            reponse.raise_for_status()
            if not url_precise(reponse.url):
                return "redirection vers une page d’accueil"
            mime = reponse.headers.get("Content-Type", "").split(";")[0].strip().lower()
            if mime not in {"text/html", "text/plain", "application/xhtml+xml"}:
                return f"format de source non pris en charge : {mime or 'inconnu'}"
            limite = 2_000_000
            donnees = bytearray()
            for bloc in reponse.iter_content(16384):
                donnees.extend(bloc)
                if len(donnees) >= limite:
                    break
            texte = donnees[:limite].decode(reponse.encoding or "utf-8", errors="replace")
    except requests.HTTPError as erreur:
        return f"source inaccessible : HTTP {erreur.response.status_code}"
    except requests.RequestException as erreur:
        return f"source inaccessible : {type(erreur).__name__}"
    except ValueError:
        return "URL de source invalide"
    except LookupError:
        return "encodage de source inconnu"

    if mime != "text/plain":
        lecteur = TexteHTML()
        lecteur.feed(texte)
        texte = " ".join(lecteur.parties)
    source = normaliser(texte)
    if len(source) < 200:
        return "source insuffisante"
    preuve = item.get("preuve") or ""
    if not isinstance(preuve, str):
        return "preuve invalide"
    nombres = chiffres(" ".join(item[c] for c in ("contenu", "impact", "action")))
    if nombres or echeance:
        extrait = normaliser(preuve)
        if len(extrait) < 15 or extrait not in source or not nombres <= chiffres(preuve):
            return "chiffres ou échéance sans preuve vérifiable"
    if echeance:
        mois = ("janvier février mars avril mai juin juillet août septembre octobre novembre décembre").split()
        dates = (echeance.isoformat(), echeance.strftime("%d/%m/%Y"),
                 f"{echeance.day} {mois[echeance.month - 1]} {echeance.year}",
                 f"{echeance.day} {mois[echeance.month - 1]}")
        if not any(re.search(r"\b" + normaliser(d) + r"\b", extrait) for d in dates):
            return "échéance absente de la preuve"
    item["lien"] = lien
    return ""


def extraire_json(texte):
    """Le modèle répond parfois avec des balises ```json autour du JSON — on les retire.
    Si du texte parasite entoure malgré tout le JSON, on tente une extraction de secours
    en isolant le premier { jusqu'au dernier } de la réponse."""
    texte = texte.strip()
    texte_nettoye = re.sub(r"^```(?:json)?\s*", "", texte)
    texte_nettoye = re.sub(r"\s*```$", "", texte_nettoye)
    try:
        return json.loads(texte_nettoye)
    except json.JSONDecodeError:
        pass

    debut = texte.find("{")
    fin = texte.rfind("}")
    if debut != -1 and fin != -1 and fin > debut:
        return json.loads(texte[debut:fin + 1])
    raise json.JSONDecodeError("Aucun JSON exploitable trouvé", texte, 0)


def rechercher_domaine(domaine, jour, reprise=False, instruction=""):
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": MODEL,
            "max_tokens": 3000,
            "tools": [{"type": "web_search_20250305", "name": "web_search"}],
            "system": SYSTEM_PROMPT,
            "messages": [{
                "role": "user",
                "content": (
                    f"Domaine de veille : {domaine['nom']}.\n"
                    f"Requête de recherche : {domaine['requete']}\n"
                    f"Date du jour : {jour.strftime('%d/%m/%Y')}. "
                    "Cherche uniquement les actualités des 7 derniers jours.\n"
                    + instruction
                ),
            }],
        },
        timeout=90,
    )

    if resp.status_code >= 400:
        print(f"[{domaine['nom']}] Erreur API ({resp.status_code}) : {resp.text[:2000]}")
        return []

    data = resp.json()
    textes = [bloc["text"] for bloc in data.get("content", []) if bloc.get("type") == "text"]
    for texte in reversed(textes):
        try:
            parsed = extraire_json(texte)
        except json.JSONDecodeError:
            continue
        items = parsed.get("items") if isinstance(parsed, dict) else None
        if isinstance(items, list) and all(isinstance(item, dict) for item in items):
            return items

    print(f"[{domaine['nom']}] Aucun JSON valide (arrêt API : {data.get('stop_reason')}).")
    if not reprise:
        print(f"[{domaine['nom']}] Nouvelle tentative de recherche.")
        return rechercher_domaine(
            domaine, jour, reprise=True,
            instruction='Réponds directement avec {"items": [...]} ou {"items": []}, sans analyse.',
        )
    return []


def envoyer_make(item, jour):
    payload = {
        "date": jour.strftime("%d/%m/%Y"),
        "domaine": item["domaine"],
        "indicateur": INDICATEURS_QUALIOPI.get(item["domaine"], ""),
        "source": item.get("source", ""),
        "organisme": item.get("organisme", ""),
        "lien": item.get("lien", ""),
        "contenu": item.get("contenu", ""),
        "impact": item.get("impact", ""),
        "diffusion": item.get("diffusion", "Non"),
        "action": item.get("action", ""),
    }
    try:
        resp = requests.post(
            MAKE_WEBHOOK_URL,
            headers={"x-make-apikey": MAKE_API_KEY},
            json=payload,
            timeout=15,
        )
        resp.raise_for_status()
        print(f"Envoyé à Make [{item['domaine']}] : {item.get('contenu', '')[:70]}")
        return True
    except Exception as e:
        print(f"Erreur envoi Make [{item.get('domaine', '?')}] : {e}")
        return False


def main():
    jour = aujourd_hui()
    historique = charger_historique()
    debut = (jour - timedelta(days=56)).isoformat()
    sujets = {
        cle: date_vue for cle, date_vue in historique.get("sujets", {}).items()
        if debut <= date_vue <= jour.isoformat()
    }
    historique["sujets"] = sujets
    total_envoyes = 0
    for domaine in DOMAINES:
        print(f"\n--- Domaine : {domaine['nom']} ---")
        envoyes = 0
        items = rechercher_domaine(domaine, jour)
        remplacement_legifrance = False
        for item in items:
            if envoyes == 2:
                break
            erreur = valider_source(item, jour)
            if erreur:
                lien = item.get("lien")
                url = lien.split("?", 1)[0].split("#", 1)[0] if isinstance(lien, str) else ""
                print(f"Ignoré : {erreur} | URL : {url[:400]!r}")
                if (erreur == "source inaccessible : HTTP 403"
                        and urlsplit(url).hostname in {"www.legifrance.gouv.fr", "legifrance.gouv.fr"}
                        and not remplacement_legifrance):
                    remplacement_legifrance = True
                    print("Légifrance refuse l’accès. Recherche d’une publication officielle accessible.")
                    items.extend(rechercher_domaine(
                        domaine, jour, reprise=True,
                        instruction=(
                            f"Le lien Légifrance {url} est inaccessible. "
                            "Cherche une publication officielle accessible qui documente la même mesure, "
                            "par exemple sur le site du ministère concerné ou service-public.gouv.fr. "
                            "Écarte les liens Légifrance pour cette recherche. "
                            'Sans publication officielle précise et récente, réponds {"items": []}.'
                        ),
                    ))
                continue
            cles = cles_sujet(item)
            if cles.intersection(sujets):
                print("Ignoré : sujet déjà signalé dans les 56 derniers jours.")
                continue
            item["domaine"] = domaine["nom"]
            if envoyer_make(item, jour):
                envoyes += 1
                total_envoyes += 1
                sujets.update(dict.fromkeys(cles, jour.isoformat()))
                sauver_historique(historique)
    sauver_historique(historique)
    print(f"Terminé. {total_envoyes} actualité(s) transmises à Make (statut initial : À valider).")


if __name__ == "__main__":
    main()
