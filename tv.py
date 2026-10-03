"""TVmaze hierarchy and child-title metadata."""
# Shows, seasons, and episodes use distinct provider keys within one persisted title hierarchy.
import json
import re

import requests

from media import plain_text, poster_url
from matching import resolve_candidates

# These mappings define the valid parent-child pairs and provider identifiers at each level.
CHILD_KINDS = {'tv': 'tv_season', 'tv_season': 'tv_episode'}
KEY_PREFIXES = {'tv': 'tvmaze', 'tv_season': 'tvmaze-season', 'tv_episode': 'tvmaze-episode'}


def tv_id(kind, key):
    # Check that the stored provider-key prefix matches the requested hierarchy level.
    prefix = KEY_PREFIXES[kind]
    if not isinstance(key, str) or not re.fullmatch(re.escape(prefix) + r':\d+', key):
        raise ValueError('Choose a TVmaze match before loading seasons or episodes.')
    return key.split(':')[1]


def fetch_tv_children(kind, key):
    # Fetch only the immediate children: seasons of a show or episodes of a season.
    identifier = tv_id(kind, key)
    path = f'shows/{identifier}/seasons' if kind == 'tv' else f'seasons/{identifier}/episodes'
    response = requests.get('https://api.tvmaze.com/' + path, timeout=(4, 8))
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list) or len(payload) > 5000 or any(not isinstance(row, dict) for row in payload):
        raise ValueError('Invalid TV collection response')
    return payload


def child_metadata(kind, row, parent, now):
    # Prefer child artwork and fall back to the parent’s chosen cover; inherit the parent’s genres.
    number = row.get('number')
    title = row.get('name') or (f'Season {number}' if kind == 'tv_season' else f'Episode {number}' if number is not None else 'Special')
    parent_metadata = json.loads(parent['metadata_json'])
    parent_match = parent_metadata.get('match', {})
    image = (row.get('image') or {}).get('original', '')
    inherited = parent['cover_choice'] or parent['cover_key']
    fallback = inherited.removeprefix('poster:') if inherited.startswith('poster:') else parent_match.get('poster_url', '')
    url = image or fallback
    date = row.get('premiereDate') if kind == 'tv_season' else row.get('airdate')
    match = {'key': KEY_PREFIXES[kind] + ':' + str(int(row['id'])), 'title': title,
             'number': number, 'airdate': date or '', 'first_publish_year': (date or '')[:4],
             'author_name': [], 'subject': parent_match.get('subject', []),
             'description': plain_text(row.get('summary')), 'poster_url': url,
             'runtime': row.get('runtime'), 'episode_count': row.get('episodeOrder')}
    options = []
    for candidate in dict.fromkeys([url, fallback]):
        if candidate:
            poster_url(candidate)
            options.append({'cover_id': len(options) + 1, 'title': title, 'publish_date': match['first_publish_year'],
                            'publishers': [], 'poster_url': candidate})
    return {'provider': 'TVmaze', 'fetched_at': now(), 'match': match, 'response': row, 'poster_options': options}


def lookup_tv_child(item, parent, normalize, now):
    # A known provider ID resolves directly; new local children match by number or normalized title.
    if item['source_key']:
        identifier = tv_id(item['kind'], item['source_key'])
        resource = 'seasons' if item['kind'] == 'tv_season' else 'episodes'
        response = requests.get(f'https://api.tvmaze.com/{resource}/{identifier}', timeout=(4, 8))
        response.raise_for_status()
        row = response.json()
        if not isinstance(row, dict):
            raise ValueError('Invalid TV title response')
        return child_metadata(item['kind'], row, parent, now)
    if parent['status'] != 'matched':
        raise ValueError('Choose the parent TV title before looking up this title.')
    key = json.loads(parent['metadata_json']).get('match', {}).get('key', '')
    rows = fetch_tv_children(parent['kind'], key)
    candidates = [child_metadata(item['kind'], row, parent, now) for row in rows
                  if ((item['position'] is not None and row.get('number') == item['position'])
                      or (item['position'] is None and normalize(row.get('name') or (f"Season {row.get('number')}" if item['kind'] == 'tv_season' else '')) == normalize(item['title'])))]
    return resolve_candidates(candidates, item)
