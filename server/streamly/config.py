"""Chargement de la configuration et generation du jeton d'acces."""
import json
import os
import secrets
import shutil

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
EXAMPLE_PATH = os.path.join(BASE_DIR, "config.example.json")
DATA_DIR = os.path.join(BASE_DIR, "data")
HLS_DIR = os.path.join(BASE_DIR, "hls")
LOG_DIR = os.path.join(BASE_DIR, "logs")
WEB_DIR = os.path.join(os.path.dirname(BASE_DIR), "web")


def load():
    """Charge config.json, en le creant depuis l'exemple au premier lancement."""
    if not os.path.exists(CONFIG_PATH):
        shutil.copy(EXAMPLE_PATH, CONFIG_PATH)
        print("config.json cree depuis l'exemple — renseignez vos providers.")

    with open(CONFIG_PATH, encoding="utf-8") as fh:
        cfg = json.load(fh)

    # Les cles de documentation du fichier exemple ne servent pas a l'execution.
    cfg = {k: v for k, v in cfg.items() if not k.startswith("_comment")}

    if not cfg.get("token"):
        cfg["token"] = secrets.token_urlsafe(24)
        save(cfg)
        print("Jeton d'acces genere : %s" % cfg["token"])

    for d in (DATA_DIR, HLS_DIR, LOG_DIR):
        os.makedirs(d, exist_ok=True)

    return cfg


def save(cfg):
    """Reecrit config.json en conservant les commentaires du fichier existant."""
    existing = {}
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            existing = json.load(fh)
    existing.update(cfg)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(existing, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, CONFIG_PATH)
    os.chmod(CONFIG_PATH, 0o600)  # contient des identifiants
