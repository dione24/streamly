"""Fiches enrichies des films et des series.

Deux sources, gardees en base une fois lues :
  - le panel lui-meme : `get_vod_info` et `get_series_info` renvoient souvent
    la fiche TMDB (image de fond, genre, annee, realisateur, distribution) ;
  - OMDb, si une cle est configuree : les notes IMDb, Rotten Tomatoes et
    Metacritic, et une affiche quand le panel n'en fournit pas.
"""
import json
import re
import time
import urllib.parse
import urllib.request

TMDB_IMAGES = 'https://image.tmdb.org/t/p/'
# Relire une fiche OMDb introuvable au bout d'un mois : le catalogue OMDb
# grandit, et le titre du panel a pu etre corrige.
OMDB_RETRY_SECONDS = 30 * 86400


def tmdb_image(value, size):
    """Adresse d'une image TMDB a la taille voulue.

    Les panels donnent tantot un chemin (« /abc.jpg »), tantot l'adresse de
    l'original (plusieurs Mo), tantot une liste : on ramene tout a une taille
    raisonnable pour un ecran.
    """
    if isinstance(value, list):
        value = next((v for v in value if v), '')
    value = str(value or '').strip()
    if not value or value.lower() in ('none', 'null'):
        return None
    if value.startswith('/'):
        return TMDB_IMAGES + size + value
    match = re.match(r'(https?://image\.tmdb\.org/t/p/)[^/]+(/.+)$', value)
    if match:
        return match.group(1) + size + match.group(2)
    return value if value.startswith(('http://', 'https://')) else None


def clean_title(title):
    """« EN| The Dunes (2019) » ou « The Dunes - 2019 » -> « The Dunes »."""
    title = re.sub(r'^\s*[A-Z]{2,3}\s*[|:]\s*', '', str(title or ''))
    title = re.sub(r'\s*[\(\[]\s*(19|20)\d{2}\s*[\)\]]\s*$', '', title)
    title = re.sub(r'\s+-\s+(19|20)\d{2}\s*$', '', title)
    return title.strip()


def _year(info):
    for key in ('releasedate', 'releaseDate', 'release_date', 'year'):
        match = re.match(r'\s*((?:19|20)\d{2})', str(info.get(key) or ''))
        if match:
            return match.group(1)
    return None


def panel_extra(info):
    """Champs utiles d'une fiche de panel (`info` de get_vod_info ou get_series_info)."""
    if not isinstance(info, dict):
        return {}
    backdrop = info.get('backdrop') or info.get('backdrop_path')
    try:
        rating = float(info.get('rating') or 0)
    except (TypeError, ValueError):
        rating = 0
    trailer = str(info.get('youtube_trailer') or '').strip()
    extra = {
        'backdrop': tmdb_image(backdrop, 'w1280'),
        'backdrop_small': tmdb_image(backdrop, 'w780'),
        'year': _year(info),
        'genre': str(info.get('genre') or '').strip()[:80],
        'director': str(info.get('director') or '').strip()[:120],
        'cast': str(info.get('cast') or info.get('actors') or '').strip()[:200],
        'rating': round(rating, 1) if 0 < rating <= 10 else None,
        'trailer': trailer if re.fullmatch(r'[\w-]{6,20}', trailer) else None,
        'original_title': clean_title(info.get('o_name') or info.get('name') or ''),
    }
    return {k: v for k, v in extra.items() if v}


def images_of(extra):
    """Images d'une fiche que le relais d'images a le droit d'aller chercher."""
    urls = [extra.get('backdrop'), extra.get('backdrop_small'), (extra.get('omdb') or {}).get('poster')]
    return [u for u in urls if u and u.startswith(('http://', 'https://'))]


def omdb_due(extra):
    """Vrai s'il faut (re)demander a OMDb."""
    at = (extra or {}).get('omdb_at') or 0
    if not at:
        return True
    return not (extra.get('omdb') or {}) and time.time() - at > OMDB_RETRY_SECONDS


class Omdb:
    """Notes et affiche d'un film ou d'une serie, par titre et annee."""
    URL = 'https://www.omdbapi.com/'

    def __init__(self, api_key, opener=None, timeout=5):
        self.api_key = (api_key or '').strip()
        self.opener = opener or urllib.request.urlopen
        self.timeout = timeout

    def __bool__(self):
        return bool(self.api_key)

    def lookup(self, title, year=None, kind='movie'):
        """Fiche OMDb reduite, ou {} si le titre est introuvable."""
        title = clean_title(title)
        if not self.api_key or not title:
            return {}
        query = {'apikey': self.api_key, 't': title, 'type': 'series' if kind == 'series' else 'movie'}
        if year:
            query['y'] = year
        with self.opener(self.URL + '?' + urllib.parse.urlencode(query), timeout=self.timeout) as response:
            data = json.loads(response.read(200000) or b'{}')
        if data.get('Response') != 'True':
            return {}
        value = lambda v: None if v in (None, '', 'N/A') else v
        ratings = {r.get('Source'): r.get('Value') for r in data.get('Ratings') or [] if isinstance(r, dict)}
        found = {
            'imdb_id': value(data.get('imdbID')),
            'imdb_rating': value(data.get('imdbRating')),
            'rotten_tomatoes': value(ratings.get('Rotten Tomatoes')),
            'metacritic': value(ratings.get('Metacritic')),
            'rated': value(data.get('Rated')),
            'awards': value(data.get('Awards')),
            'poster': value(data.get('Poster')),
            'runtime': value(data.get('Runtime')),
        }
        return {k: v for k, v in found.items() if v}
