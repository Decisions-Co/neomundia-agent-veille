"""
Agent de veille NEOMUNDIA — dispositif déterministe (workflow, pas agentique).

Chaque semaine : interroge Claude avec l'outil web_search pour chacun des 5
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
from datetime import datetime

import requests

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
MAKE_WEBHOOK_URL = os.environ["MAKE_WEBHOOK_URL"]
MAKE_API_KEY = os.environ["MAKE_API_KEY"]

MODEL = "claude-sonnet-4-6"
HISTORIQUE_PATH = "historique_veille.json"

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
        "requete": "France Compétences actualité métiers compétences emploi formation professionnelle",
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
]

CONTEXTE_NEOMUNDIA = (
    "NEOMUNDIA est l'activité de formation professionnelle de Décisions & Co, "
    "organisme certifié Qualiopi. Formations en présentiel, distanciel et FEST/FOAD, "
    "à destination de salariés et demandeurs d'emploi. L'évaluation d'impact doit "
    "être concrète et actionnable pour cet organisme précis, jamais générique."
)

SYSTEM_PROMPT = f"""Tu es l'agent de veille de NEOMUNDIA. Ta mission : identifier UNIQUEMENT
les actualités réellement nouvelles et substantielles du domaine assigné, parues dans les
7 derniers jours — jamais un résumé générique du secteur, jamais une actualité déjà ancienne
ou spéculative.

{CONTEXTE_NEOMUNDIA}

Pour chaque actualité pertinente trouvée (0 à 2 maximum — la rareté est normale, ne force
jamais une entrée artificielle si rien de substantiel n'est paru), fournis un résumé factuel,
sans opinion ni exagération.

Réponds UNIQUEMENT avec un objet JSON valide, sans aucun texte avant ou après, au format exact :
{{"items": [
  {{
    "source": "nom du site ou de la publication",
    "organisme": "organisme émetteur (ex: Agefiph, Légifrance, France Compétences, Centre Inffo...)",
    "lien": "URL directe de la source",
    "contenu": "résumé factuel en une phrase",
    "impact": "impact concret pour NEOMUNDIA, une à deux phrases",
    "diffusion": "Oui ou Non — Oui si les formateurs ou l'équipe pédagogique doivent en avoir connaissance",
    "action": "action concrète recommandée, ou Aucune action requise dans l'immédiat"
  }}
]}}

Si rien de nouveau et substantiel n'est trouvé pour ce domaine, réponds {{"items": []}}.
"""


def charger_historique():
    if os.path.exists(HISTORIQUE_PATH):
        with open(HISTORIQUE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"liens_vus": []}


def sauver_historique(historique):
    with open(HISTORIQUE_PATH, "w", encoding="utf-8") as f:
        json.dump(historique, f, ensure_ascii=False, indent=2)


def hash_lien(lien):
    return hashlib.sha256(lien.strip().lower().encode("utf-8")).hexdigest()


def extraire_json(texte):
    """Le modèle répond parfois avec des balises ```json autour du JSON — on les retire."""
    texte = texte.strip()
    texte = re.sub(r"^```(?:json)?\s*", "", texte)
    texte = re.sub(r"\s*```$", "", texte)
    return json.loads(texte)


def rechercher_domaine(domaine, liens_vus):
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": MODEL,
            "max_tokens": 1500,
            "tools": [{"type": "web_search_20250305", "name": "web_search"}],
            "system": SYSTEM_PROMPT,
            "messages": [{
                "role": "user",
                "content": (
                    f"Domaine de veille : {domaine['nom']}.\n"
                    f"Requête de recherche : {domaine['requete']}\n"
                    f"Date du jour : {datetime.now().strftime('%d/%m/%Y')}. "
                    f"Cherche uniquement les actualités des 7 derniers jours."
                ),
            }],
        },
        timeout=90,
    )

    if resp.status_code >= 400:
        print(f"[{domaine['nom']}] Erreur API ({resp.status_code}) : {resp.text[:2000]}")
        return []

    data = resp.json()
    texte_final = ""
    for bloc in data.get("content", []):
        if bloc.get("type") == "text":
            texte_final = bloc["text"]

    if not texte_final:
        print(f"[{domaine['nom']}] Aucune réponse texte exploitable.")
        return []

    try:
        parsed = extraire_json(texte_final)
    except json.JSONDecodeError:
        print(f"[{domaine['nom']}] Réponse non-JSON, ignorée : {texte_final[:300]}")
        return []

    nouveaux = []
    for item in parsed.get("items", []):
        lien = (item.get("lien") or "").strip()
        if not lien:
            continue
        h = hash_lien(lien)
        if h in liens_vus:
            print(f"[{domaine['nom']}] Déjà signalé, ignoré : {lien}")
            continue
        liens_vus.add(h)
        item["domaine"] = domaine["nom"]
        nouveaux.append(item)

    return nouveaux


def envoyer_make(item):
    payload = {
        "date": datetime.now().strftime("%d/%m/%Y"),
        "domaine": item["domaine"],
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
    except Exception as e:
        print(f"Erreur envoi Make [{item.get('domaine', '?')}] : {e}")


def main():
    print(f"Veille NEOMUNDIA — {datetime.now().strftime('%A %d %B %Y')}")
    historique = charger_historique()
    liens_vus = set(historique.get("liens_vus", []))
    total_envoyes = 0

    for domaine in DOMAINES:
        print(f"\n--- Domaine : {domaine['nom']} ---")
        items = rechercher_domaine(domaine, liens_vus)
        for item in items:
            envoyer_make(item)
            total_envoyes += 1

    historique["liens_vus"] = list(liens_vus)
    sauver_historique(historique)
    print(f"\nTerminé. {total_envoyes} entrée(s) envoyée(s) au Sheet, statut initial : À valider.")


if __name__ == "__main__":
    main()
