"""Movie and TV metadata providers; artwork is cached unchanged by the app."""
# Provider adapters normalize external records while the app manages persistence and image caching.
import html
import re
from urllib.parse import urlsplit, quote

import requests
from matching import resolve_candidates, matches_movie_year
from tagging import wikipedia_film_subjects
from balloon import lookup_balloon, get_json as balloon_json, detail_path, description as balloon_description, PROVIDER
from omdb import lookup_omdb, movie_detail as omdb_detail, text_field as omdb_text


def poster_url(value):
    # Accept only known HTTPS artwork hosts without credentials or alternate ports.
    if not isinstance(value, str):
        raise ValueError('Invalid poster URL')
    parsed = urlsplit(value)
    host = parsed.hostname or ''
    if (parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port not in (None, 443)
            or not (host in ('static.tvmaze.com', 'upload.wikimedia.org', 'm.media-amazon.com') or host.endswith('.mzstatic.com'))):
        raise ValueError('Invalid poster host')
    return value


def plain_text(value):
    # Convert provider summaries into plain text before storing them for escaped template output.
    return html.unescape(re.sub(r'<[^>]*>', '', value or '')).strip()


def lookup_media(item, normalize, now):
    # Adapt movie and TV results to the same match structure used by book metadata.
    choice = item.get('match_choice', '')
    if item['kind'] == 'movie' and (not choice or choice.startswith('omdb:')):
        primary = lookup_omdb(item, normalize, now, poster_url)
        if primary:
            return primary
    primary = lookup_balloon(item, normalize, now) if not choice or choice.startswith('balloon-') else None
    if primary:
        return primary
    if item['kind'] == 'movie':
        response = requests.get('https://itunes.apple.com/search',
                                params={'term': item['title'], 'entity': 'movie', 'media': 'movie', 'limit': 50, 'country': 'CA'},
                                timeout=(4, 8))
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get('results'), list):
            raise ValueError('Invalid movie search response')
        matches = [row for row in payload['results'] if isinstance(row, dict)
                   and row.get('kind') == 'feature-movie'
                   and normalize(row.get('trackName', '')) == normalize(item['title'])
                   and matches_movie_year(item, row.get('releaseDate'))
                   and (not item['author'] or normalize(item['author']) == normalize(row.get('artistName', '')))]
        # Keep exact-match rules when falling back to another movie source.
        if not matches:
            return lookup_movie_wikipedia(item, normalize, now)
        candidates = []
        for row in matches:
            match = {'key': f"itunes:{int(row['trackId'])}", 'title': row['trackName'],
                     'author_name': [row['artistName']] if row.get('artistName') else [],
                     'first_publish_year': (row.get('releaseDate') or '')[:4],
                     'subject': [row['primaryGenreName']] if row.get('primaryGenreName') else [],
                     'description': plain_text(row.get('longDescription') or row.get('shortDescription')),
                     'poster_url': row.get('artworkUrl100', '')}
            options = [{'cover_id': int(row['trackId']), 'title': row['trackName'],
                        'publish_date': match['first_publish_year'], 'publishers': match['author_name'],
                        'poster_url': poster_url(row['artworkUrl100'])}] if row.get('artworkUrl100') else []
            candidates.append({'provider': 'Apple iTunes', 'fetched_at': now(), 'match': match,
                               'response': payload, 'poster_options': options})
        return resolve_candidates(candidates, item)
    response = requests.get('https://api.tvmaze.com/search/shows', params={'q': item['title']}, timeout=(4, 8))
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise ValueError('Invalid TV search response')
    candidates = []
    for entry in payload:
        show = entry.get('show', {}) if isinstance(entry, dict) else {}
        if not isinstance(show, dict) or normalize(show.get('name', '')) != normalize(item['title']):
            continue
        match = {'key': f"tvmaze:{int(show['id'])}", 'title': show['name'], 'author_name': [],
                 'first_publish_year': (show.get('premiered') or '')[:4], 'subject': show.get('genres') or [],
                 'description': plain_text(show.get('summary')),
                 'poster_url': (show.get('image') or {}).get('original', '')}
        candidates.append({'provider': 'TVmaze', 'fetched_at': now(), 'match': match, 'response': payload})
    return resolve_candidates(candidates, item)


def lookup_movie_wikipedia(item, normalize, now):
    # Use film articles as a fallback when the movie catalogue has no exact match.
    response = requests.get('https://en.wikipedia.org/w/api.php', params={
        'action': 'query', 'format': 'json', 'generator': 'search', 'gsrsearch': item['title'] + ' film',
        'gsrlimit': 20, 'prop': 'extracts|pageimages', 'exintro': 1, 'explaintext': 1,
        'exlimit': 'max', 'piprop': 'original', 'pilicense': 'any'},
        headers={'User-Agent': 'Cinebook/0.1 (local personal collection)'}, timeout=(4, 8))
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or 'error' in payload:
        raise ValueError('Invalid movie response')
    pages = payload.get('query', {}).get('pages', {})
    candidates = []
    for page in sorted(pages.values(), key=lambda page: page.get('index', 0)):
        title = page.get('title', '')
        description = page.get('extract', '').strip()
        # Require a film article for the exact title, rather than soundtracks or related films.
        bare_title = re.sub(r' \((?:\d{4} )?film\)$', '', title)
        intro = description.split('\n', 1)[0]
        identity = re.split(r'\bdirected by\b', intro, maxsplit=1, flags=re.IGNORECASE)[0]
        medium = re.split(r'\b(?:film|movie)\b', identity, maxsplit=1, flags=re.IGNORECASE)[0]
        other_media = re.search(r'\b(?:television|TV|series|sitcom|song|soundtrack|album|novel|book|video game)\b', medium, re.IGNORECASE)
        film_intro = re.search(r'\b(?:film|movie)\b', identity, re.IGNORECASE) or (
            re.search(r'\bdirected by\b', intro, re.IGNORECASE)
            and re.search(r'\b(?:comedy|drama|thriller|horror|documentary|animation|animated|romance|action|adventure)\b', identity, re.IGNORECASE))
        if normalize(bare_title) != normalize(item['title']) or other_media or not film_intro:
            continue
        director = re.search(r'directed by ([^.\n]+?)(?: \(| in (?:his|her|their) (?:feature )?directorial debut| and | from a screenplay|,|\.|\n|$)', intro, re.IGNORECASE)
        author = director.group(1).strip() if director else ''
        if item['author'] and normalize(item['author']) != normalize(author):
            continue
        year = re.search(r'\bis (?:a|an) ([1-9]\d{3})\b', intro, re.IGNORECASE)
        year_value = year.group(1) if year else ''
        if not matches_movie_year(item, year_value):
            continue
        url = page.get('original', {}).get('source', '')
        match = {'key': f"wikipedia:{int(page['pageid'])}", 'title': bare_title,
                 'author_name': [author] if author else [], 'first_publish_year': year_value,
                 'subject': wikipedia_film_subjects(description), 'description': description, 'poster_url': url,
                 'source_url': 'https://en.wikipedia.org/wiki/' + quote(title.replace(' ', '_'))}
        options = [{'cover_id': int(page['pageid']), 'title': bare_title, 'publish_date': match['first_publish_year'],
                    'publishers': [author] if author else [], 'poster_url': poster_url(url)}] if url else []
        candidates.append({'provider': 'Wikipedia', 'fetched_at': now(), 'match': match, 'response': payload, 'poster_options': options})
    return resolve_candidates(candidates, item)


def media_cover_options(book, offset):
    # Movies and TV children reuse stored options; shows retrieve original poster candidates from TVmaze.
    key = book['match'].get('key', '')
    if key.startswith('balloon-'):
        options = list(book['metadata'].get('poster_options', []))
        if book['kind'] in ('movie', 'tv'):
            payload = balloon_json(detail_path(book['kind'], key) + '/images')
            posters = payload.get('posters')
            if not isinstance(posters, list):
                raise ValueError('Invalid Balloonerismm images response.')
            urls = list(dict.fromkeys([option['poster_url'] for option in options] +
                                     [row['file_path'] for row in posters if isinstance(row, dict) and row.get('file_path')]))
            options = [{'cover_id': index, 'title': book['title'], 'publish_date': book['match'].get('first_publish_year', ''),
                        'publishers': [], 'poster_url': poster_url(url)} for index, url in enumerate(urls, start=1)]
    elif book['kind'] in ('movie', 'tv_season', 'tv_episode'):
        options = book['metadata'].get('poster_options', [])
    else:
        key = book['match'].get('key', '')
        if not re.fullmatch(r'tvmaze:\d+', key):
            return {'options': [], 'next_offset': None}
        response = requests.get(f"https://api.tvmaze.com/shows/{key.split(':')[1]}/images", timeout=(4, 8))
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise ValueError('Invalid TV images response')
        options = [{'cover_id': int(image['id']), 'title': book['title'],
                    'publish_date': book['match'].get('first_publish_year', ''), 'publishers': [],
                    'poster_url': poster_url(image['resolutions']['original']['url'])}
                   for image in payload if isinstance(image, dict) and image.get('type') == 'poster'
                   and (image.get('resolutions') or {}).get('original')]
    # Use the same paging convention as the book cover picker.
    return {'options': options[offset:offset + 50], 'next_offset': offset + 50 if len(options) > offset + 50 else None}


def fetch_media_description(item):
    # Refresh a selected provider record directly instead of repeating an ambiguous title search.
    key = item['match'].get('key', '')
    if key.startswith('balloon-'):
        payload = balloon_json(detail_path(item['kind'], key))
        return {'description': balloon_description(payload), 'response': payload, 'provider': PROVIDER}
    if re.fullmatch(r'omdb:tt\d+', key):
        payload = omdb_detail(key.split(':', 1)[1])
        return {'description': omdb_text(payload, 'Plot'), 'response': payload, 'provider': 'OMDb'}
    if not re.fullmatch(r'(itunes|tvmaze|tvmaze-season|tvmaze-episode|wikipedia):\d+', key):
        return None
    provider, identifier = key.split(':')
    if provider == 'itunes':
        response = requests.get('https://itunes.apple.com/lookup', params={'id': identifier, 'country': 'CA'}, timeout=(4, 8))
        response.raise_for_status()
        payload = response.json()
        results = payload.get('results', []) if isinstance(payload, dict) else []
        row = next((row for row in results if str(row.get('trackId')) == identifier), None)
        description = plain_text(row.get('longDescription') or row.get('shortDescription')) if row else ''
    elif provider in ('tvmaze', 'tvmaze-season', 'tvmaze-episode'):
        resource = {'tvmaze': 'shows', 'tvmaze-season': 'seasons', 'tvmaze-episode': 'episodes'}[provider]
        response = requests.get(f'https://api.tvmaze.com/{resource}/{identifier}', timeout=(4, 8))
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError('Invalid TV response')
        description = plain_text(payload.get('summary'))
    else:
        response = requests.get('https://en.wikipedia.org/w/api.php', params={
            'action': 'query', 'format': 'json', 'pageids': identifier, 'prop': 'extracts',
            'exintro': 1, 'explaintext': 1}, headers={'User-Agent': 'Cinebook/0.1 (local personal collection)'}, timeout=(4, 8))
        response.raise_for_status()
        payload = response.json()
        description = payload.get('query', {}).get('pages', {}).get(identifier, {}).get('extract', '')
    return {'description': description, 'response': payload,
            'provider': {'itunes': 'Apple iTunes', 'tvmaze': 'TVmaze', 'tvmaze-season': 'TVmaze', 'tvmaze-episode': 'TVmaze', 'wikipedia': 'Wikipedia'}[provider]}
