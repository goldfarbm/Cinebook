"""IMDb-backed movie and TV adapter for api.balloonerismm.workers.dev."""
import html
import re
from urllib.parse import urlsplit

import requests

from matching import resolve_candidates, matches_movie_year

BASE_URL = 'https://api.balloonerismm.workers.dev'
PROVIDER = 'Balloonerismm (IMDb)'
KEY_PATTERNS = {
    'movie': r'balloon-movie:(tt\d+)',
    'tv': r'balloon-tv:(tt\d+)',
    'tv_season': r'balloon-season:(tt\d+):(\d+)',
    'tv_episode': r'balloon-episode:(tt\d+):(\d+):(\d+)',
}


def key_parts(kind, key):
    match = re.fullmatch(KEY_PATTERNS[kind], key or '')
    if not match:
        raise ValueError('Invalid Balloonerismm title identifier.')
    return match.groups()


def detail_path(kind, key):
    parts = key_parts(kind, key)
    if kind in ('movie', 'tv'):
        return f'/{kind}/{parts[0]}'
    path = f'/tv/{parts[0]}/season/{parts[1]}'
    return path + (f'/episode/{parts[2]}' if kind == 'tv_episode' else '')


def get_json(path, params=None):
    response = requests.get(BASE_URL + path, params=params, timeout=(4, 8))
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or payload.get('success') is False:
        raise ValueError('Balloonerismm API returned an invalid response.')
    return payload


def description(row):
    return html.unescape(re.sub(r'<[^>]*>', '', row.get('overview') or '')).strip()


def confirm_movie_title(link, title, normalize):
    """Use an IMDb link solely to verify the entered title, without importing metadata."""
    try:
        url = urlsplit(link.strip())
        identifier = re.fullmatch(r'/title/(tt\d+)/?', url.path)
        if (url.scheme not in ('http', 'https') or url.hostname not in ('imdb.com', 'www.imdb.com', 'm.imdb.com')
                or url.username or url.password or url.port not in (None, 80, 443) or not identifier):
            raise ValueError
    except ValueError:
        raise ValueError('Enter an IMDb title link, such as https://www.imdb.com/title/tt2543164/.') from None
    try:
        record = get_json('/movie/' + identifier.group(1))
        if record.get('id') != identifier.group(1) or not isinstance(record.get('title'), str) or not record['title'].strip():
            raise ValueError('Invalid IMDb movie title response.')
    except (requests.RequestException, ValueError, TypeError, KeyError):
        raise ValueError('Could not confirm the title from IMDb. Try again, or remove the optional IMDb link.') from None
    names = [record['title'], record.get('original_title')]
    if not any(isinstance(name, str) and normalize(name) == normalize(title) for name in names):
        raise ValueError(f'The IMDb link is for “{record["title"]}”. Enter its title or original title to add this movie.')


def lookup_balloon(item, normalize, now):
    kind = item['kind']
    if kind not in ('movie', 'tv'):
        return None
    try:
        payload = get_json(f'/search/{kind}', {'query': item['title'], 'language': 'en-US'})
        rows = payload.get('results')
        if not isinstance(rows, list):
            raise ValueError('Invalid Balloonerismm search results.')
        candidates = []
        title_field = 'title' if kind == 'movie' else 'name'
        for row in rows:
            if not isinstance(row, dict) or not re.fullmatch(r'tt\d+', str(row.get('id', ''))):
                continue
            names = [row.get(title_field), row.get('original_' + title_field)]
            if not any(isinstance(name, str) and normalize(name) == normalize(item['title']) for name in names):
                continue
            identifier = row['id']
            detail = get_json(f'/{kind}/{identifier}')
            if detail.get('id') != identifier or not isinstance(detail.get(title_field), str):
                raise ValueError('Invalid Balloonerismm detail record.')
            if not any(isinstance(name, str) and normalize(name) == normalize(item['title'])
                       for name in [detail.get(title_field), detail.get('original_' + title_field)]):
                continue
            authors, credits = [], None
            if kind == 'movie':
                if not matches_movie_year(item, detail.get('release_date')):
                    continue
                try:
                    credits = get_json(f'/movie/{identifier}/credits')
                    authors = list(dict.fromkeys(person['name'] for person in credits.get('crew', [])
                        if isinstance(person, dict) and person.get('job', '').casefold() == 'director'
                        and isinstance(person.get('name'), str)))
                except (requests.RequestException, ValueError, TypeError, KeyError):
                    if item.get('author'):
                        continue
                if item.get('author') and not any(normalize(author) == normalize(item['author']) for author in authors):
                    continue
            genres = detail.get('genres', [])
            if not isinstance(genres, list):
                raise ValueError('Invalid Balloonerismm genres.')
            match = {'key': f'balloon-{kind}:{identifier}', 'title': detail[title_field],
                     'author_name': authors,
                     'first_publish_year': (detail.get('release_date' if kind == 'movie' else 'first_air_date') or '')[:4],
                     'subject': [genre['name'] for genre in genres if isinstance(genre, dict)
                                 and isinstance(genre.get('name'), str)],
                     'description': description(detail), 'poster_url': detail.get('poster_path') or '',
                     'source_url': f'https://www.imdb.com/title/{identifier}/',
                     'runtime': detail.get('runtime')}
            options = [{'cover_id': 1, 'title': match['title'], 'publish_date': match['first_publish_year'],
                        'publishers': authors, 'poster_url': match['poster_url']}] if match['poster_url'] else []
            candidates.append({'provider': PROVIDER, 'fetched_at': now(), 'match': match,
                               'response': {'search': payload, 'detail': detail, 'credits': credits},
                               'poster_options': options})
        return resolve_candidates(candidates, item)
    except (requests.RequestException, ValueError, TypeError, KeyError):
        # An unavailable primary API must not prevent the existing providers from working.
        return None


def fetch_children(kind, key):
    parts = key_parts(kind, key)
    detail = get_json(detail_path(kind, key))
    field = 'seasons' if kind == 'tv' else 'episodes'
    rows = detail.get(field)
    if not isinstance(rows, list) or len(rows) > 5000:
        raise ValueError('Invalid Balloonerismm TV collection.')
    children = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('Invalid Balloonerismm TV child.')
        number = row.get('season_number' if kind == 'tv' else 'episode_number')
        if not isinstance(number, int) or not 0 <= number <= 9999:
            raise ValueError('Invalid Balloonerismm TV child number.')
        child_key = (f'balloon-season:{parts[0]}:{number}' if kind == 'tv'
                     else f'balloon-episode:{parts[0]}:{parts[1]}:{number}')
        children.append(child_row(row, child_key, number, kind == 'tv'))
    return children


def child_row(row, key, number, season):
    return {'_provider_key': key, '_provider': PROVIDER, 'id': row.get('id', key), 'number': number,
            'name': row.get('name') or row.get('label') or (f'Season {number}' if season else f'Episode {number}'),
            'summary': description(row), 'image': {'original': row.get('poster_path' if season else 'still_path') or ''},
            'premiereDate': row.get('air_date') or '', 'airdate': row.get('air_date') or '',
            'runtime': row.get('runtime'), 'episodeOrder': len(row.get('episodes', [])) if season else None,
            '_response': row}
