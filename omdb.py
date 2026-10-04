"""Optional OMDb movie fallback, with bounded searches and exact matching."""
import os
import re

import requests

from matching import resolve_candidates, matches_movie_year
from credentials import read_credentials

BASE_URL = 'https://www.omdbapi.com/'
PROVIDER = 'OMDb'


def api_key():
    override = os.environ.get('OMDB_API_KEY', '').strip()
    if override:
        return override
    try:
        return read_credentials().get('omdb_api_key', '').strip()
    except (OSError, ValueError, AttributeError):
        return ''


def test_api_key(key):
    """Return a safe status without exposing upstream errors or request URLs."""
    try:
        response = requests.get(BASE_URL, params={'apikey': key, 'i': 'tt0133093', 'r': 'json'}, timeout=(4, 8))
        if response.status_code == 401:
            return 'Invalid key'
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            return 'Unavailable'
        if payload.get('Response') == 'True' and payload.get('imdbID') == 'tt0133093':
            return 'Connected'
        error = str(payload.get('Error', '')).casefold()
        if 'invalid api key' in error or 'no api key' in error:
            return 'Invalid key'
        if 'limit' in error:
            return 'Request limit reached'
        return 'Unavailable'
    except (requests.RequestException, ValueError):
        return 'Unavailable'


def get_json(params):
    key = api_key()
    if not key:
        raise ValueError('OMDb API key is not configured.')
    response = requests.get(BASE_URL, params={**params, 'apikey': key, 'r': 'json'}, timeout=(4, 8))
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError('Invalid OMDb response.')
    return payload


def movie_detail(identifier):
    if not re.fullmatch(r'tt\d+', identifier):
        raise ValueError('Invalid OMDb movie identifier.')
    row = get_json({'i': identifier, 'type': 'movie', 'plot': 'full'})
    if row.get('Response') != 'True' or row.get('Type') != 'movie' or row.get('imdbID') != identifier:
        raise ValueError('OMDb movie is unavailable.')
    return row


def text_field(row, field):
    value = row.get(field)
    return value.strip() if isinstance(value, str) and value.strip() != 'N/A' else ''


def lookup_omdb(item, normalize, now, validate_poster):
    if not api_key():
        return None
    try:
        choice = item.get('match_choice', '')
        rows, search = [], []
        if choice:
            if not re.fullmatch(r'omdb:tt\d+', choice):
                return None
            rows = [{'imdbID': choice.split(':', 1)[1], 'Title': item['title'], 'Type': 'movie'}]
        else:
            # OMDb returns ten records per page; inspect at most fifty, like iTunes.
            for page in range(1, 6):
                params = {'s': item['title'], 'type': 'movie', 'page': page}
                if item.get('year'):
                    params['y'] = item['year']
                payload = get_json(params)
                search.append(payload)
                if payload.get('Response') != 'True':
                    break
                results = payload.get('Search')
                if not isinstance(results, list):
                    raise ValueError('Invalid OMDb search response.')
                rows.extend(results)
                if page * 10 >= int(payload.get('totalResults', len(results))):
                    break
        candidates, seen = [], set()
        for row in rows:
            if not isinstance(row, dict) or row.get('Type') != 'movie':
                continue
            identifier = row.get('imdbID', '')
            if (not isinstance(identifier, str) or not re.fullmatch(r'tt\d+', identifier)
                    or identifier in seen or normalize(text_field(row, 'Title')) != normalize(item['title'])):
                continue
            seen.add(identifier)
            detail = movie_detail(identifier)
            if normalize(text_field(detail, 'Title')) != normalize(item['title']):
                continue
            if not matches_movie_year(item, text_field(detail, 'Year')):
                continue
            directors = [name.strip() for name in text_field(detail, 'Director').split(',') if name.strip()]
            if item.get('author') and not any(normalize(name) == normalize(item['author']) for name in directors):
                continue
            poster = text_field(detail, 'Poster')
            if poster:
                try:
                    poster = validate_poster(poster)
                except ValueError:
                    poster = ''
            runtime = re.fullmatch(r'(\d+) min', text_field(detail, 'Runtime'))
            match = {'key': 'omdb:' + identifier, 'title': detail['Title'], 'author_name': directors,
                     'first_publish_year': text_field(detail, 'Year')[:4],
                     'subject': [genre.strip() for genre in text_field(detail, 'Genre').split(',') if genre.strip()],
                     'description': text_field(detail, 'Plot'), 'poster_url': poster,
                     'source_url': f'https://www.imdb.com/title/{identifier}/',
                     'runtime': int(runtime.group(1)) if runtime else None}
            options = [{'cover_id': 1, 'title': match['title'], 'publish_date': match['first_publish_year'],
                        'publishers': directors, 'poster_url': poster}] if poster else []
            candidates.append({'provider': PROVIDER, 'fetched_at': now(), 'match': match,
                               'response': {'search': search, 'detail': detail}, 'poster_options': options})
        return resolve_candidates(candidates, item)
    except (requests.RequestException, ValueError, TypeError, KeyError):
        return None
