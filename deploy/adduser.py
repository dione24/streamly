#!/usr/bin/env python3
"""Cree ou met a jour un compte Streamly.

    python3 deploy/adduser.py <identifiant> [role]

Le mot de passe est demande sans echo. Seuls un sel et une empreinte PBKDF2
sont ecrits dans config.json : le mot de passe n'y figure jamais en clair.
Roles : admin (par defaut) ou viewer.
"""
import getpass
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'server'))
from streamly.auth import hash_password  # noqa: E402

CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'server', 'config.json')


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    username, role = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else 'admin')
    if role not in ('admin', 'viewer'):
        print("Role inconnu : utilisez 'admin' ou 'viewer'.")
        return 1

    password = os.environ.get('STREAMLY_PASSWORD') or getpass.getpass('Mot de passe : ')
    if len(password) < 8:
        print('Mot de passe trop court (8 caracteres minimum).')
        return 1

    with open(CONFIG, encoding='utf-8') as fh:
        cfg = json.load(fh)
    salt, digest = hash_password(password)
    users = [u for u in cfg.get('users', []) if u.get('username') != username]
    users.append({'username': username, 'salt': salt,
                  'password_hash': digest, 'role': role})
    cfg['users'] = users

    tmp = CONFIG + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(cfg, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, CONFIG)
    os.chmod(CONFIG, 0o600)
    print("Compte '%s' enregistre (role %s). Redemarrez le service." % (username, role))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
