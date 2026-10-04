"""Cinebook: a local library with persisted, asynchronous book enrichment."""
# Flask routes, SQLite persistence, and background enrichment share the title model across all categories.
import csv
import hashlib
import io
import json
import os
import re
import secrets
import sqlite3
import subprocess
import threading
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import requests
from flask import Flask, abort, flash, redirect, render_template, request, session, url_for
from media import lookup_media, media_cover_options, fetch_media_description, poster_url
from matching import candidate_key, resolve_candidates
from balloon import KEY_PATTERNS, confirm_movie_title
from credentials import read_credentials, update_credentials, using_credentials
from omdb import api_key as omdb_api_key, test_api_key as test_omdb_api_key
from cover_uploads import MAX_COVER_BYTES, validate_cover_image, cover_mimetype
from tv import CHILD_KINDS, child_metadata, fetch_tv_children, lookup_tv_child
from exporting import save_json_export
from tagging import (tag_name, ensure_tag, tag_catalog, attach_tags, initialize_tags,
                     sync_metadata_tags, replace_title_tags, suggest_from_library)


def now():
    return datetime.now(timezone.utc).isoformat()


def normalize(value):
    return re.sub(r'[^\w]+', ' ', value.casefold()).strip()


def title_initial(title):
    # Ignore leading punctuation and fold accents; numbers and other scripts use #.
    first = next((char for char in unicodedata.normalize('NFKD', title) if char.isalnum()), '')
    initial = first.upper()[:1]
    return initial if initial in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ' and initial else '#'


def connect(app):
    # Enable foreign keys on every connection so deleting a TV parent also removes its descendants.
    db = sqlite3.connect(app.config['DATABASE'], timeout=10)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    return db


# Persist one pair of progress values; templates translate them to reading or watching labels.
READING_STATUSES = ('To Be Read', 'Read')
COLLECTION_CATEGORIES = {'book': 'Books', 'movie': 'Movies', 'tv': 'TV Shows'}
SINGULAR_CATEGORIES = {'book': 'Book', 'movie': 'Movie', 'tv': 'TV Show'}
TITLE_LABELS = {'book': 'Book', 'movie': 'Movie', 'tv': 'TV show'}
ALL_CATEGORIES = {**COLLECTION_CATEGORIES, 'tv_season': 'Seasons', 'tv_episode': 'Episodes'}
SINGULAR_CATEGORIES.update(tv_season='Season', tv_episode='Episode')
TITLE_LABELS.update(tv_season='Season', tv_episode='Episode')
COLOUR_SCHEMES = {'forest': 'Paper & Forest', 'slate': 'Slate & Blue', 'plum': 'Parchment & Plum'}


def validate_kind(value):
    if value not in ALL_CATEGORIES:
        raise ValueError('Choose Books, Movies, or TV Shows.')
    return value


def validate_reading_status(value):
    if value not in READING_STATUSES:
        raise ValueError('Status must be Read or To Be Read.')
    return value


def validate_eidr(value):
    # Accept a bare ID or registry URL, then verify its ISO 7064 MOD 37,36 check character.
    value = str(value or '').strip().upper()
    value = re.sub(r'^HTTPS://(?:DOI\.ORG/|UI\.EIDR\.ORG/CONTENT/)', '', value)
    if not value:
        return ''
    if not re.fullmatch(r'10\.5240/(?:[0-9A-F]{4}-){5}[0-9A-Z]', value):
        raise ValueError('EIDR must use the format 10.5240/XXXX-XXXX-XXXX-XXXX-XXXX-C.')
    alphabet = '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ'
    suffix = value.split('/')[1].replace('-', '')
    check = 36
    for character in suffix[:-1]:
        check = ((check + alphabet.index(character)) % 36 or 36) * 2 % 37
    if suffix[-1] != alphabet[(37 - check) % 36]:
        raise ValueError('EIDR check character is invalid. Check the identifier and try again.')
    return value


def validate(data):
    # Normalize form and import fields through one path, including media status labels and TV parent IDs.
    result = {key: str(data.get(key) or '').strip() for key in ('title', 'author', 'isbn', 'notes')}
    result['kind'] = validate_kind(data.get('kind') or 'book')
    status = data.get('reading_status') or 'To Be Read'
    if result['kind'] != 'book':
        status = {'Watched': 'Read', 'To Watch': 'To Be Read'}.get(status, status)
    result['reading_status'] = validate_reading_status(status)
    if result['kind'] == 'movie':
        result['year'] = str(data.get('year') or '').strip()
        if result['year'] and not re.fullmatch(r'[1-9]\d{3}', result['year']):
            raise ValueError('Year must be a four-digit year between 1000 and 9999.')
        result['alternate_title'] = str(data.get('alternate_title') or '').strip()
        if len(result['alternate_title']) > 500:
            raise ValueError('Alternate Title must be at most 500 characters.')
    if result['kind'] in ('movie', 'tv'):
        result['eidr'] = validate_eidr(data.get('eidr'))
    if result['kind'] in ('tv_season', 'tv_episode'):
        try:
            result['parent_id'] = int(data.get('parent_id'))
            result['position'] = int(data['position']) if data.get('position') not in (None, '') else None
        except (ValueError, TypeError):
            raise ValueError('Choose a parent title and enter a valid season or episode number.')
        if result['parent_id'] < 1 or (result['position'] is not None and not 0 <= result['position'] <= 9999):
            raise ValueError('Season and episode numbers must be between 0 and 9999.')
        result['source_key'] = str(data.get('source_key') or '')
        prefix = 'season' if result['kind'] == 'tv_season' else 'episode'
        if result['source_key'] and not re.fullmatch(
                'tvmaze-' + prefix + r':\d+|' + KEY_PATTERNS[result['kind']], result['source_key']):
            raise ValueError('Invalid TV title identifier.')
    if not result['title'] or len(result['title']) > 500:
        raise ValueError('Each title must contain 1–500 characters.')
    if len(result['author']) > 500 or len(result['notes']) > 10000:
        raise ValueError('Author must be under 500 characters; notes under 10,000.')
    result['isbn'] = re.sub(r'[\s-]', '', result['isbn']).upper()
    if result['kind'] != 'book':
        result['isbn'] = ''
    if result['isbn'] and not re.fullmatch(r'(\d{13}|\d{9}[\dX])', result['isbn']):
        raise ValueError('ISBN must have 10 or 13 characters, with optional spaces or hyphens.')
    if 'tags' in data:
        if not isinstance(data['tags'], list) or len(data['tags']) > 100:
            raise ValueError('Tags must be a list of at most 100 names.')
        result['tags'] = list(dict.fromkeys(tag_name(name) for name in data['tags']))
    return result


def identity(book):
    # Top-level duplicates use ISBN or title/author; TV children are identified within their own parent.
    if book.get('kind') in ('tv_season', 'tv_episode'):
        suffix = book.get('source_key') or ('number:' + str(book['position']) if book.get('position') is not None else 'title:' + normalize(book['title']))
        return f"{book['kind']}:{book['parent_id']}:{suffix}"
    value = 'isbn:' + book['isbn'] if book['isbn'] else normalize(book['title']) + '|' + normalize(book['author'])
    if book.get('kind') == 'movie' and book.get('year'):
        value += '|' + str(book['year'])
    return value if book.get('kind', 'book') == 'book' else book['kind'] + ':' + value


def matches_search(book, query):
    # Include only assigned tags, alongside the title fields used by collection search.
    values = [book['title'], book.get('display_author', book.get('author', '')), book.get('isbn', '')]
    if book.get('kind') == 'movie':
        values.append(book.get('alternate_title', ''))
    if book.get('position') is not None:
        values.append(str(book['position']))
    values.extend(tag['name'] for tag in book.get('tags', []))
    return not query or query.casefold() in ' '.join(values).casefold()


def metadata_author(metadata, fallback):
    names = metadata.get('match', {}).get('author_name', [])
    if not isinstance(names, list):
        return fallback
    names = list(dict.fromkeys(name.strip() for name in names if isinstance(name, str) and name.strip()))
    return ', '.join(names) if names else fallback


def book_authors(book):
    # Use separate provider author names only when they still agree with the user-entered author.
    names = book.get('match', {}).get('author_name', [])
    if isinstance(names, list):
        names = list(dict.fromkeys(name.strip() for name in names if isinstance(name, str) and name.strip()))
        if normalize(', '.join(names)) == normalize(book['author']) or not book['author']:
            return names
    return [book['author']] if book['author'] else []


def metadata_field_paths(value, path=()):
    # Represent array entries with None so one visibility setting applies across the entire array.
    if isinstance(value, dict) and value:
        return {leaf for key, child in value.items() for leaf in metadata_field_paths(child, path + (key,))}
    if isinstance(value, list) and any(isinstance(child, (dict, list)) for child in value):
        return {leaf for child in value for leaf in metadata_field_paths(child, path + (None,))}
    return {path} if path else set()


def field_token(path):
    # JSON path tokens preserve literal field names without confusing dots or brackets with separators.
    return json.dumps(path, ensure_ascii=False, separators=(',', ':'))


def field_label(path):
    return ''.join('[]' if part is None else ('.' if index else '') + part for index, part in enumerate(path))


def filtered_metadata(value, hidden):
    # Build a display-only copy and prune emptied branches; the saved metadata is never modified.
    omitted = object()

    def filter_value(item, path):
        if field_token(path) in hidden:
            return omitted
        if isinstance(item, dict):
            result = {}
            for key, child in item.items():
                filtered = filter_value(child, path + (key,))
                if filtered is not omitted:
                    result[key] = filtered
            return result if result or not item else omitted
        if isinstance(item, list) and any(isinstance(child, (dict, list)) for child in item):
            result = [filter_value(child, path + (None,)) for child in item]
            result = [child for child in result if child is not omitted]
            return result if result or not item else omitted
        return item

    result = filter_value(value, ())
    return {} if result is omitted else result


def save_metadata_author(db, book, metadata):
    author = metadata_author(metadata, book['author'])
    if author == book['author']:
        return
    canonical_identity = identity({**dict(book), 'author': author})
    # Two separately entered records may converge to the same author. Keep both records.
    collision = db.execute('SELECT 1 FROM titles WHERE identity=? AND id!=?', (canonical_identity, book['id'])).fetchone()
    db.execute('UPDATE titles SET author=?,identity=?,revision=revision+1 WHERE id=? AND revision=?',
               (author, book['identity'] if collision else canonical_identity, book['id'], book['revision']))


def lookup(book):
    # Provider searches are fuzzy, so retain only exact normalized title/author or ISBN matches.
    if book.get('kind', 'book') != 'book':
        return lookup_media(book, normalize, now)
    params = {'limit': 100, 'fields': 'key,title,author_name,first_publish_year,isbn,subject,edition_count,cover_i'}
    if book['isbn']:
        params['isbn'] = book['isbn']
    else:
        params['title'] = book['title']
        if book['author']:
            params['author'] = book['author']
    response = requests.get('https://openlibrary.org/search.json', params=params, timeout=(4, 8),
                            headers={'User-Agent': os.environ.get('CINEBOOK_USER_AGENT', os.environ.get('CINABOOK_USER_AGENT', 'Cinebook/0.1 (local personal library)'))})
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or not isinstance(payload.get('docs'), list):
        raise ValueError('Invalid search response')
    docs = payload.get('docs', [])
    candidates = []
    for doc in docs:
        if not isinstance(doc, dict) or not isinstance(doc.get('title'), str):
            continue
        authors = doc.get('author_name', [])
        if not isinstance(authors, list) or any(not isinstance(author, str) for author in authors):
            continue
        title_match = normalize(doc.get('title', '')) == normalize(book['title'])
        author_match = not book['author'] or any(normalize(book['author']) == normalize(a) for a in authors)
        if (book['isbn'] and book['isbn'] in doc.get('isbn', [])) or (not book['isbn'] and title_match and author_match):
            candidates.append({'provider': 'Open Library', 'fetched_at': now(), 'match': doc, 'response': payload})
    return resolve_candidates(candidates, book)


def enrich_one(app):
    # Process one queued title; TV children wait until their parent has a selected metadata match.
    with connect(app) as db:
        book = db.execute("SELECT * FROM titles WHERE status='pending' AND (parent_id IS NULL OR parent_id IN (SELECT id FROM titles WHERE status='matched')) ORDER BY id LIMIT 1").fetchone()
    if book is None:
        return False
    try:
        if book['kind'] in ('tv_season', 'tv_episode'):
            with connect(app) as db:
                parent = db.execute('SELECT * FROM titles WHERE id=?', (book['parent_id'],)).fetchone()
            if parent is None:
                return False
            metadata = app.config['LOOKUP_TV_CHILD'](dict(book), dict(parent), normalize, now)
        else:
            with using_credentials(app.config['CREDENTIALS_FILE']):
                metadata = app.config['LOOKUP'](dict(book))
        status = 'ambiguous' if metadata and metadata.get('candidates') else 'matched' if metadata else 'unmatched'
        error = ''
    except (requests.RequestException, ValueError, TypeError, KeyError) as exc:
        metadata, status = None, 'failed'
        error = 'Metadata lookup failed. Check your connection and retry.'
        app.logger.info('Lookup failed for book %s: %s', book['id'], type(exc).__name__)
    # Do not overwrite an edit made while the network request was running.
    with connect(app) as db:
        saved_json = json.dumps(metadata, ensure_ascii=False) if metadata else ('{}' if status != 'failed' else book['metadata_json'])
        updated = db.execute("UPDATE titles SET metadata_json=?, status=?, error=?, cover_status=CASE WHEN ?='failed' OR cover_choice LIKE 'upload:%' THEN cover_status ELSE 'pending' END, description_status=CASE WHEN ?='failed' THEN description_status ELSE 'pending' END WHERE id=? AND revision=?",
                             (saved_json, status, error, status, status, book['id'], book['revision']))
        if updated.rowcount and status != 'failed':
            sync_metadata_tags(db, book['id'], metadata or {})
        if status == 'ambiguous' and updated.rowcount:
            db.execute("UPDATE titles SET cover_status=CASE WHEN cover_choice LIKE 'upload:%' THEN cover_status ELSE 'unavailable' END,description_status='unavailable' WHERE id=?", (book['id'],))
        elif metadata and updated.rowcount:
            save_metadata_author(db, book, metadata)
            if book['kind'] in ('tv_season', 'tv_episode'):
                db.execute('UPDATE titles SET source_key=? WHERE id=?', (metadata['match'].get('key', ''), book['id']))
    return True


def fetch_description(work_key):
    # Open Library descriptions may be plain strings or objects containing a value field.
    if not isinstance(work_key, str) or not re.fullmatch(r'/works/OL\d+W', work_key):
        raise ValueError('Invalid work identifier')
    response = requests.get(f'https://openlibrary.org{work_key}.json', timeout=(4, 8),
                            headers={'User-Agent': os.environ.get('CINEBOOK_USER_AGENT', os.environ.get('CINABOOK_USER_AGENT', 'Cinebook/0.1 (local personal library)'))})
    if response.status_code == 404:
        return None
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError('Invalid work response')
    description = payload.get('description', '')
    if isinstance(description, dict):
        description = description.get('value', '')
    if not isinstance(description, str):
        raise ValueError('Invalid description')
    return {'description': description.strip(), 'response': payload, 'fetched_at': now(),
            'provider': 'Open Library', 'work_key': work_key}


def fetch_one_description(app):
    # Description state is independent of artwork state; cached book descriptions are shared by work ID.
    with connect(app) as db:
        book = db.execute("SELECT * FROM titles WHERE description_status='pending' AND status NOT IN ('pending','ambiguous') ORDER BY id LIMIT 1").fetchone()
    if book is None:
        return False
    metadata = json.loads(book['metadata_json'])
    work_key = metadata.get('match', {}).get('key', '')
    description, status, error = '', 'unavailable', ''
    try:
        if book['kind'] != 'book':
            # Initial descriptions arrive with the search; independent retries refresh the source.
            description = metadata.get('match', {}).get('description', '')
            if metadata.pop('refresh_description', False) or not description:
                with using_credentials(app.config['CREDENTIALS_FILE']):
                    result = app.config['FETCH_MEDIA_DESCRIPTION']({**dict(book), 'match': metadata.get('match', {})})
                if result:
                    description = result['description']
                    metadata['work'] = result
            status = 'available' if description else 'unavailable'
        elif isinstance(work_key, str) and re.fullmatch(r'/works/OL\d+W', work_key):
            with connect(app) as db:
                cached = db.execute('SELECT payload FROM descriptions WHERE work_key=?', (work_key,)).fetchone()
            result = json.loads(cached['payload']) if cached else app.config['FETCH_DESCRIPTION'](work_key)
            if result:
                description = result['description']
                metadata['work'] = result
                if description:
                    status = 'available'
                    with connect(app) as db:
                        db.execute('INSERT OR IGNORE INTO descriptions (work_key,payload) VALUES (?,?)',
                                   (work_key, json.dumps(result, ensure_ascii=False)))
    except (requests.RequestException, ValueError, TypeError, KeyError) as exc:
        description, status = book['description'], 'failed'
        error = 'Description lookup failed. Check your connection and retry.'
        app.logger.info('Description failed for book %s: %s', book['id'], type(exc).__name__)
    with connect(app) as db:
        db.execute('UPDATE titles SET description=?,description_status=?,description_error=?,metadata_json=? WHERE id=? AND revision=?',
                   (description, status, error, json.dumps(metadata, ensure_ascii=False), book['id'], book['revision']))
    return True


def cover_identifier(book):
    # An explicit cover choice takes precedence over provider artwork and the ISBN fallback.
    if book['cover_choice']:
        return book['cover_choice']
    match = json.loads(book['metadata_json']).get('match', {})
    if book['kind'] != 'book':
        return 'poster:' + poster_url(match['poster_url']) if match.get('poster_url') else ''
    cover_id = match.get('cover_i')
    if isinstance(cover_id, int) and cover_id > 0:
        return f'id:{cover_id}'
    if book['isbn']:
        return 'isbn:' + book['isbn']
    return ''


def fetch_cover_options(work_key, offset):
    # Edition pages supply candidate artwork; repeated cover IDs appear only once per page.
    if not re.fullmatch(r'/works/OL\d+W', work_key):
        raise ValueError('Invalid work identifier')
    response = requests.get(f'https://openlibrary.org{work_key}/editions.json',
                            params={'limit': 50, 'offset': offset}, timeout=(4, 8),
                            headers={'User-Agent': os.environ.get('CINEBOOK_USER_AGENT', os.environ.get('CINABOOK_USER_AGENT', 'Cinebook/0.1 (local personal library)'))})
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or not isinstance(payload.get('entries'), list):
        raise ValueError('Invalid editions response')
    options, seen = [], set()
    for edition in payload['entries']:
        if not isinstance(edition, dict):
            continue
        languages = edition.get('languages') or []
        if not isinstance(languages, list) or not languages:
            continue
        # Require explicitly English editions; unknown and mixed languages are excluded.
        if any(not isinstance(language, dict) or language.get('key') != '/languages/eng' for language in languages):
            continue
        for cover_id in edition.get('covers', []):
            if isinstance(cover_id, int) and cover_id > 0 and cover_id not in seen:
                seen.add(cover_id)
                options.append({'cover_id': cover_id, 'edition_key': edition.get('key', ''),
                                'title': edition.get('title', ''), 'publishers': edition.get('publishers', []),
                                'publish_date': edition.get('publish_date', ''), 'isbn': (edition.get('isbn_13') or edition.get('isbn_10') or [''])[0]})
    return {'language': 'eng', 'options': options[:12], 'next_offset': offset + 50 if payload.get('size', 0) > offset + 50 else None}


def fetch_cover(identifier):
    # Keep the original image bytes after validating the source, response type, size, and file signature.
    key, value = identifier.split(':', 1)
    if key == 'poster':
        url = poster_url(value)
    elif key not in ('id', 'isbn') or not re.fullmatch(r'[0-9X]+', value):
        raise ValueError('Invalid cover identifier')
    else:
        url = f'https://covers.openlibrary.org/b/{key}/{value}-L.jpg'
    with requests.get(url, params={'default': 'false'}, timeout=(4, 8), stream=True,
                      **({'allow_redirects': False} if key == 'poster' else {}),
                      headers={'User-Agent': os.environ.get('CINEBOOK_USER_AGENT', os.environ.get('CINABOOK_USER_AGENT', 'Cinebook/0.1 (local personal library)'))}) as response:
        if response.status_code == 404:
            return None
        response.raise_for_status()
        content_type = response.headers.get('Content-Type', '').split(';')[0].strip().lower()
        if content_type not in (('image/jpeg', 'image/png') if key == 'poster' else ('image/jpeg',)):
            raise ValueError('Cover response is not JPEG')
        content = bytearray()
        for chunk in response.iter_content(65536):
            content.extend(chunk)
            if len(content) > 5 * 1024 * 1024:
                raise ValueError('Cover is too large')
        if not (content.startswith(b'\xff\xd8\xff') or (key == 'poster' and content.startswith(b'\x89PNG\r\n\x1a\n'))):
            raise ValueError('Invalid JPEG cover')
        return bytes(content)


def fetch_one_cover(app):
    # Reuse images by provider identifier, then attach the result only if the title revision still matches.
    with connect(app) as db:
        book = db.execute("SELECT * FROM titles WHERE cover_status='pending' AND status NOT IN ('pending','ambiguous') ORDER BY id LIMIT 1").fetchone()
    if book is None:
        return False
    identifier = ''
    status, error = 'unavailable', ''
    saved_key = ''
    try:
        identifier = cover_identifier(book)
        if identifier:
            with connect(app) as db:
                cached = db.execute('SELECT image FROM covers WHERE identifier=?', (identifier,)).fetchone()
            image = cached['image'] if cached else app.config['FETCH_COVER'](identifier)
            if image:
                with connect(app) as db:
                    db.execute('INSERT OR IGNORE INTO covers (identifier,image,fetched_at) VALUES (?,?,?)',
                               (identifier, image, now()))
                saved_key, status = identifier, 'available'
    except (requests.RequestException, ValueError, OSError) as exc:
        status, error = 'failed', 'Cover lookup failed. You can retry below.'
        saved_key = book['cover_key']  # Keep a previously saved cover during network outages.
        app.logger.info('Cover failed for book %s: %s', book['id'], type(exc).__name__)
    with connect(app) as db:
        db.execute('UPDATE titles SET cover_key=?,cover_status=?,cover_error=? WHERE id=? AND revision=?',
                   (saved_key, status, error, book['id'], book['revision']))
    return True


def worker(app):
    # Short-circuiting gives metadata priority, followed by descriptions and covers, one job per iteration.
    while True:
        try:
            did_work = enrich_one(app) or fetch_one_description(app) or fetch_one_cover(app)
        except sqlite3.Error:
            app.logger.exception('Metadata worker database error')
            did_work = False
        # Also stays below the Covers API's ISBN limit of 100 requests per five minutes.
        time.sleep(3.1 if did_work else 2)


def parse_import(filename, content, default_kind='book', default_parent_id=None):
    # Validate the entire upload before inserting anything; exports retain IDs for later parent remapping.
    text = content.decode('utf-8-sig')
    suffix = Path(filename).suffix.lower()
    exported = False
    catalog_names = []
    if suffix == '.csv':
        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames or 'title' not in reader.fieldnames:
            raise ValueError('CSV needs a title column; author, isbn, and notes are optional.')
        rows = list(reader)
    elif suffix == '.txt':
        rows = [{'title': line.strip()} for line in text.splitlines() if line.strip()]
    elif suffix == '.json':
        value = json.loads(text)
        exported = isinstance(value, dict) and 'version' in value and 'titles' in value
        if exported:
            catalog_names = value.get('tags', [])
            if not isinstance(catalog_names, list) or len(catalog_names) > 1000:
                raise ValueError('The exported tag catalog must be a list of at most 1000 names.')
            catalog_names = [tag_name(name) for name in catalog_names]
        rows = value.get('titles') if isinstance(value, dict) else value
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError('JSON must contain a list of book objects, or an exported titles list.')
    else:
        raise ValueError('Choose a .csv, .txt, or .json file.')
    if (not rows and not exported) or len(rows) > (5000 if exported else 25):
        raise ValueError('Import 1–25 books at a time.')
    validated = []
    for row in rows:
        result = validate({'kind': default_kind, 'parent_id': default_parent_id, **row})
        if exported:
            try:
                result['_import_id'] = int(row['id'])
            except (KeyError, TypeError, ValueError):
                raise ValueError('An exported title is missing its original ID.')
        validated.append(result)
    if catalog_names and validated:
        validated[0]['_tag_catalog'] = catalog_names
    return validated


def create_app(config=None):
    # Provider and save-dialog functions are injectable so tests can run without network calls or native dialogs.
    app = Flask(__name__)
    app.config.update(DATABASE=str(Path(app.instance_path) / 'cinabook.sqlite3'), SECRET_KEY=secrets.token_hex(32),
                      MAX_CONTENT_LENGTH=1024 * 1024, START_WORKER=True, LOOKUP=lookup, FETCH_COVER=fetch_cover,
                      FETCH_DESCRIPTION=fetch_description,
                      FETCH_COVER_OPTIONS=fetch_cover_options,
                      FETCH_MEDIA_COVER_OPTIONS=media_cover_options,
                      FETCH_MEDIA_DESCRIPTION=fetch_media_description,
                      FETCH_TV_CHILDREN=fetch_tv_children, LOOKUP_TV_CHILD=lookup_tv_child, SAVE_EXPORT=save_json_export,
                      SESSION_COOKIE_SAMESITE='Strict', SESSION_COOKIE_HTTPONLY=True)
    app.config.update(config or {})
    app.config.setdefault('CREDENTIALS_FILE', str(Path(app.config['DATABASE']).parent / 'credentials.json'))
    Path(app.config['DATABASE']).parent.mkdir(parents=True, exist_ok=True)
    with connect(app) as db:
        db.execute('''CREATE TABLE IF NOT EXISTS titles (
            id INTEGER PRIMARY KEY, kind TEXT NOT NULL DEFAULT 'book',
            title TEXT NOT NULL, author TEXT NOT NULL, isbn TEXT NOT NULL,
            notes TEXT NOT NULL, identity TEXT NOT NULL UNIQUE,
            metadata_json TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'pending',
            error TEXT NOT NULL DEFAULT '', added_at TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0)''')
        # Add missing columns in place so earlier local libraries remain usable after upgrades.
        columns = {row['name'] for row in db.execute('PRAGMA table_info(titles)')}
        for column, definition in [('cover_key', "TEXT NOT NULL DEFAULT ''"),
                                   ('cover_status', "TEXT NOT NULL DEFAULT 'pending'"),
                                   ('cover_error', "TEXT NOT NULL DEFAULT ''"),
                                   ('description', "TEXT NOT NULL DEFAULT ''"),
                                   ('description_status', "TEXT NOT NULL DEFAULT 'pending'"),
                                   ('description_error', "TEXT NOT NULL DEFAULT ''"),
                                   ('reading_status', "TEXT NOT NULL DEFAULT 'To Be Read' CHECK (reading_status IN ('Read', 'To Be Read'))"),
                                   ('cover_choice', "TEXT NOT NULL DEFAULT ''"),
                                   ('match_choice', "TEXT NOT NULL DEFAULT ''"),
                                   ('parent_id', 'INTEGER REFERENCES titles(id) ON DELETE CASCADE'),
                                   ('position', 'INTEGER'),
                                   ('source_key', "TEXT NOT NULL DEFAULT ''"),
                                   ('parent_source_key', "TEXT NOT NULL DEFAULT ''"),
                                   ('eidr', "TEXT NOT NULL DEFAULT ''"),
                                   ('alternate_title', "TEXT NOT NULL DEFAULT ''"),
                                   ('year', "TEXT NOT NULL DEFAULT ''")]:
            if column not in columns:
                db.execute(f'ALTER TABLE titles ADD COLUMN {column} {definition}')
                if column == 'cover_status':
                    # Older cached search responses did not request a cover ID.
                    db.execute("UPDATE titles SET status='pending' WHERE status='matched'")
                if column == 'match_choice':
                    # Audit earlier automatic selections once, retaining their saved artwork meanwhile.
                    db.execute("UPDATE titles SET status='pending' WHERE status='matched'")
        db.execute('''CREATE TABLE IF NOT EXISTS covers (
            identifier TEXT PRIMARY KEY, image BLOB NOT NULL, fetched_at TEXT NOT NULL)''')
        db.execute('''CREATE TABLE IF NOT EXISTS descriptions (
            work_key TEXT PRIMARY KEY, payload TEXT NOT NULL)''')
        db.execute('''CREATE TABLE IF NOT EXISTS cover_options (
            work_key TEXT NOT NULL, offset INTEGER NOT NULL, payload TEXT NOT NULL,
            PRIMARY KEY (work_key,offset))''')
        db.execute('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value_json TEXT NOT NULL)')
        db.execute('''CREATE TABLE IF NOT EXISTS tv_collections (
            parent_id INTEGER NOT NULL REFERENCES titles(id) ON DELETE CASCADE,
            source_key TEXT NOT NULL, fetched_at TEXT NOT NULL, payload_json TEXT NOT NULL,
            PRIMARY KEY (parent_id,source_key))''')
        initialize_tags(db)
        # Upgrade previously matched entries using their already saved metadata.
        for book in db.execute('SELECT * FROM titles').fetchall():
            save_metadata_author(db, book, json.loads(book['metadata_json']))

    # Serialize concurrent thumbnail downloads and prevent overlapping native save dialogs.
    cover_download_lock = threading.Lock()
    export_save_lock = threading.Lock()

    def default_category():
        with connect(app) as db:
            preference = db.execute("SELECT value_json FROM settings WHERE key='default_category'").fetchone()
        return json.loads(preference['value_json']) if preference else 'book'

    @app.context_processor
    def collection_labels():
        # Expose saved display preferences and navigation helpers to every template.
        with connect(app) as db:
            preference = db.execute("SELECT value_json FROM settings WHERE key='default_view'").fetchone()
            theme_preference = db.execute("SELECT value_json FROM settings WHERE key='theme'").fetchone()
            scheme_preference = db.execute("SELECT value_json FROM settings WHERE key='colour_scheme'").fetchone()
            available_tags = tag_catalog(db)
        default_view = json.loads(preference['value_json']) if preference else 'grid'
        theme = json.loads(theme_preference['value_json']) if theme_preference else 'light'
        colour_scheme = json.loads(scheme_preference['value_json']) if scheme_preference else 'forest'
        return {'categories': ALL_CATEGORIES, 'singular_categories': SINGULAR_CATEGORIES, 'title_labels': TITLE_LABELS,
                'collection_url': collection_url, 'title_url': title_url, 'default_view': default_view, 'theme': theme,
                'colour_scheme': colour_scheme, 'colour_schemes': COLOUR_SCHEMES,
                'default_category': default_category(), 'collection_categories': COLLECTION_CATEGORIES,
                'available_tags': available_tags}

    def cached_cover_image(identifier):
        # Browser thumbnails may arrive together; serialize downloads and reuse cached images.
        with cover_download_lock:
            with connect(app) as db:
                row = db.execute('SELECT image FROM covers WHERE identifier=?', (identifier,)).fetchone()
            if row:
                return row['image']
            image = app.config['FETCH_COVER'](identifier)
            if image:
                with connect(app) as db:
                    db.execute('INSERT OR IGNORE INTO covers (identifier,image,fetched_at) VALUES (?,?,?)', (identifier,image,now()))
            return image

    @app.before_request
    def local_guard():
        # Restrict accepted hosts and require the session token for every state-changing form submission.
        if request.endpoint == 'upload_cover':
            # Allow a 5 MB image plus multipart overhead; imports retain their 1 MB limit.
            request.max_content_length = MAX_COVER_BYTES + 1024 * 1024
        if request.host.split(':')[0].lower() not in ('localhost', '127.0.0.1'):
            abort(403)
        if 'csrf' not in session:
            session['csrf'] = secrets.token_hex(32)
        if request.method == 'POST' and not secrets.compare_digest(session['csrf'], request.form.get('csrf', '')):
            abort(400, description='Invalid form token. Reload the page and try again.')

    @app.after_request
    def headers(response):
        response.headers['Content-Security-Policy'] = "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        return response

    def get_book(book_id):
        # Expand the persisted JSON into the fields used by shared collection and detail templates.
        with connect(app) as db:
            row = db.execute('SELECT * FROM titles WHERE id=?', (book_id,)).fetchone()
        if row is None:
            abort(404)
        book = dict(row)
        book['metadata'] = json.loads(book['metadata_json'])
        book['match'] = book['metadata'].get('match', {})
        book['display_author'] = book['author'] or ', '.join(book['match'].get('author_name', [])) or 'Author unknown'
        book['authors'] = book_authors(book)
        with connect(app) as db:
            attach_tags(db, [book])
        return book

    def collection_url(book, query=''):
        # Return to the containing collection: a category, a show’s seasons, or a season’s episodes.
        if book['kind'] == 'tv_season':
            return url_for('tv_seasons', show_id=book['parent_id'], q=query)
        if book['kind'] == 'tv_episode':
            return url_for('tv_episodes', season_id=book['parent_id'], q=query)
        return url_for('index', category=book['kind'], **({'q': query} if query else {}))

    def title_url(book):
        # TV parents open their child collections; individual episodes and other titles open details.
        if book['kind'] == 'tv':
            return url_for('tv_seasons', show_id=book['id'])
        if book['kind'] == 'tv_season':
            return url_for('tv_episodes', season_id=book['id'])
        return url_for('detail', book_id=book['id'])

    def tv_parent(parent_id, child_kind):
        parent = get_book(parent_id)
        if CHILD_KINDS.get(parent['kind']) != child_kind:
            raise ValueError('Choose the correct parent show or season for this title.')
        return parent

    def sync_tv_children(parent, force=False):
        # A cached parent/provider pair is sufficient offline unless the user explicitly requests a refresh.
        if parent['status'] != 'matched' or not re.fullmatch(
                r'tvmaze(?:-season)?:\d+|' + KEY_PATTERNS[parent['kind']], parent['match'].get('key', '')):
            return 'Choose a match or finish the parent title lookup to load this collection.'
        key = parent['match']['key']
        with connect(app) as db:
            cached = db.execute('SELECT 1 FROM tv_collections WHERE parent_id=? AND source_key=?', (parent['id'], key)).fetchone()
        if cached and not force:
            return ''
        try:
            rows = app.config['FETCH_TV_CHILDREN'](parent['kind'], key)
            children = [child_metadata(CHILD_KINDS[parent['kind']], row, parent, now) for row in rows]
            with connect(app) as db:
                # Recheck the parent after the network request before committing any refreshed child records.
                db.execute('BEGIN IMMEDIATE')
                current = db.execute('SELECT * FROM titles WHERE id=?', (parent['id'],)).fetchone()
                if (current is None or current['revision'] != parent['revision'] or
                        json.loads(current['metadata_json']).get('match', {}).get('key') != key):
                    return 'The parent title changed during the lookup. Refresh this page.'
                for metadata in children:
                    match = metadata['match']
                    kind = CHILD_KINDS[parent['kind']]
                    existing = db.execute('SELECT * FROM titles WHERE parent_id=? AND source_key=?', (parent['id'], match['key'])).fetchone()
                    if not existing and match['number'] is not None:
                        existing = db.execute("SELECT * FROM titles WHERE parent_id=? AND source_key='' AND position=? AND (parent_source_key=? OR parent_source_key='')", (parent['id'], match['number'], key)).fetchone()
                    if existing:
                        # Keep the local title, notes, watched status, and selected cover on refresh.
                        db.execute("UPDATE titles SET metadata_json=?,source_key=?,parent_source_key=?,status='matched',error='',description=?,description_status=?,description_error='',cover_status=CASE WHEN cover_choice='' THEN 'pending' ELSE cover_status END,revision=revision+1 WHERE id=?",
                                   (json.dumps(metadata, ensure_ascii=False), match['key'], key, match['description'], 'available' if match['description'] else 'unavailable', existing['id']))
                    else:
                        child = {'kind': kind, 'parent_id': parent['id'], 'source_key': match['key'], 'title': match['title']}
                        db.execute('''INSERT INTO titles (kind,parent_id,position,source_key,parent_source_key,title,author,isbn,notes,identity,added_at,metadata_json,status,description,description_status,cover_status)
                            VALUES (?,?,?,?,?,?,'','','',?, ?,?,'matched',?,?, 'pending')''',
                            (kind, parent['id'], match['number'], match['key'], key, match['title'], identity(child), now(),
                             json.dumps(metadata, ensure_ascii=False), match['description'], 'available' if match['description'] else 'unavailable'))
                    saved_id = existing['id'] if existing else db.execute('SELECT last_insert_rowid()').fetchone()[0]
                    sync_metadata_tags(db, saved_id, metadata)
                db.execute('INSERT OR REPLACE INTO tv_collections (parent_id,source_key,fetched_at,payload_json) VALUES (?,?,?,?)',
                           (parent['id'], key, now(), json.dumps(rows, ensure_ascii=False)))
            return ''
        except (requests.RequestException, ValueError, TypeError, KeyError, sqlite3.IntegrityError):
            return 'Could not load this collection. Saved titles are still available. Check your connection and try again.'

    def render_tv_collection(parent):
        # Only display children associated with the current parent match, plus locally added children.
        error = sync_tv_children(parent)
        query = request.args.get('q', '').strip()
        selected_tag = requested_tag()
        key = parent['match'].get('key', '')
        with connect(app) as db:
            rows = db.execute("SELECT * FROM titles WHERE parent_id=? AND (parent_source_key=? OR parent_source_key='') ORDER BY position IS NULL,position,id", (parent['id'], key)).fetchall()
        books = []
        for row in rows:
            child = get_book(row['id'])
            if selected_tag and not any(tag['id'] == selected_tag['id'] for tag in child['tags']):
                continue
            if matches_search(child, query):
                books.append(child)
        kind = CHILD_KINDS[parent['kind']]
        displayed = filtered_metadata(parent['metadata'], hidden_metadata_fields())
        ancestors = []
        ancestor = parent
        while ancestor['parent_id']:
            ancestor = get_book(ancestor['parent_id'])
            ancestors.insert(0, ancestor)
        return render_template('detail.html', book=parent, displayed_metadata=displayed,
                               metadata_filtered=displayed != parent['metadata'], children_parent=parent,
                               books=books, total=len(rows), pending=sum(row['status'] == 'pending' for row in rows),
                               category=kind, category_label=ALL_CATEGORIES[kind].lower(), query=query,
                               hierarchy_error=error, ancestors=ancestors, selected_tag=selected_tag)

    @app.get('/tv/<int:show_id>/seasons')
    def tv_seasons(show_id):
        parent = get_book(show_id)
        if parent['kind'] != 'tv':
            abort(404)
        return render_tv_collection(parent)

    @app.get('/tv/seasons/<int:season_id>/episodes')
    def tv_episodes(season_id):
        parent = get_book(season_id)
        if parent['kind'] != 'tv_season':
            abort(404)
        return render_tv_collection(parent)

    @app.post('/tv/<int:book_id>/refresh-children')
    def refresh_tv_children(book_id):
        parent = get_book(book_id)
        if parent['kind'] not in CHILD_KINDS:
            abort(404)
        error = sync_tv_children(parent, force=True)
        flash(error or 'Collection refreshed. Your notes and watched statuses were kept.')
        return redirect(title_url(parent))

    def requested_tag():
        value = request.args.get('tag', '')
        if not value:
            return None
        try:
            tag_id = int(value)
        except ValueError:
            abort(400)
        with connect(app) as db:
            row = db.execute('SELECT id,name FROM tags WHERE id=?', (tag_id,)).fetchone()
        if row is None:
            abort(404)
        return dict(row)

    @app.get('/tags')
    def manage_tags():
        return render_template('tags.html')

    @app.post('/tags')
    def create_tag():
        try:
            name = tag_name(request.form.get('name', ''))
            with connect(app) as db:
                exists = db.execute('SELECT 1 FROM tags WHERE name_key=?', (name.casefold(),)).fetchone()
                ensure_tag(db, name)
            flash('That tag already exists.' if exists else f'Tag created: {name}.')
        except ValueError as exc:
            flash(str(exc))
        return redirect(url_for('manage_tags'))

    @app.post('/tags/<int:tag_id>/delete')
    def delete_tag(tag_id):
        with connect(app) as db:
            row = db.execute('SELECT name FROM tags WHERE id=?', (tag_id,)).fetchone()
            if row is None:
                abort(404)
            db.execute('DELETE FROM tags WHERE id=?', (tag_id,))
        flash(f"Tag deleted: {row['name']}. Titles remain in your library.")
        return redirect(url_for('manage_tags'))

    @app.post('/tags/suggest')
    def suggest_tags():
        with connect(app) as db:
            added = suggest_from_library(db)
        flash(f'Automatic tagging complete. Applied tags from saved metadata and added {added} new '
              f'{"tag" if added == 1 else "tags"}. Your tag selections were kept.')
        return redirect(url_for('manage_tags'))

    @app.post('/books/<int:book_id>/tags')
    def save_title_tags(book_id):
        get_book(book_id)
        try:
            ids = [int(value) for value in request.form.getlist('tag_id')]
            name = request.form.get('new_tag', '').strip()
            with connect(app) as db:
                # Validate submitted IDs before creating a tag so stale forms do not partially save.
                available = {row['id'] for row in db.execute('SELECT id FROM tags')}
                if not set(ids).issubset(available) or len(set(ids)) > 100:
                    raise ValueError('A selected tag is invalid. Reload the page and try again.')
                if name:
                    ids.append(ensure_tag(db, name))
                replace_title_tags(db, book_id, ids)
            flash('Tags saved.')
        except ValueError as exc:
            flash(str(exc))
        return redirect(url_for('detail', book_id=book_id))

    def hidden_metadata_fields():
        with connect(app) as db:
            row = db.execute("SELECT value_json FROM settings WHERE key='hidden_metadata_fields'").fetchone()
        return set(json.loads(row['value_json'])) if row else set()

    missing_movie_cover = """kind='movie' AND NOT EXISTS (
        SELECT 1 FROM covers WHERE identifier=titles.cover_key AND length(image)>0)"""

    @app.post('/settings/movies/lookup-missing-covers')
    def lookup_movies_without_covers():
        # Queue fresh metadata; the existing worker then fetches descriptions and artwork.
        with connect(app) as db:
            missing = db.execute('SELECT COUNT(*) FROM titles WHERE ' + missing_movie_cover).fetchone()[0]
            queued = db.execute("""UPDATE titles SET status='pending',match_choice='',error='',
                cover_status='pending',cover_error='',description_status='pending',description_error='',
                revision=revision+1 WHERE """ + missing_movie_cover + " AND status!='pending'").rowcount
        if queued:
            flash(f'Metadata lookup queued for {queued} ' + ('movie' if queued == 1 else 'movies') + ' without cover images.')
        elif missing:
            flash('All movies without cover images are already queued.')
        else:
            flash('Every movie already has a cover image.')
        return redirect(url_for('settings', _anchor='movie-lookups'))

    @app.get('/settings')
    def settings():
        # Include previously hidden paths even when the current metadata no longer contains those fields.
        hidden = hidden_metadata_fields()
        paths = {tuple(json.loads(token)) for token in hidden}
        with connect(app) as db:
            rows = db.execute('SELECT metadata_json FROM titles').fetchall()
            missing_covers = db.execute('SELECT COUNT(*) FROM titles WHERE ' + missing_movie_cover).fetchone()[0]
        for row in rows:
            paths.update(metadata_field_paths(json.loads(row['metadata_json'])))
        groups = {}
        for path in sorted(paths, key=field_label):
            token = field_token(path)
            group = path[0]
            groups.setdefault(group, []).append({'token': token, 'label': field_label(path), 'visible': token not in hidden})
        return render_template('settings.html', groups=groups, field_count=len(paths), omdb=omdb_settings(),
                               movies_without_covers=missing_covers)

    def omdb_settings():
        try:
            saved = read_credentials(app.config['CREDENTIALS_FILE'])
        except (OSError, ValueError):
            return {'status': 'Unavailable', 'saved': False, 'override': False}
        override = os.environ.get('OMDB_API_KEY', '').strip()
        configured = bool(saved.get('omdb_api_key'))
        status = saved.get('omdb_status', 'Not tested') if configured else 'Not configured'
        if override:
            fingerprint = hashlib.sha256(override.encode()).hexdigest()
            tested = saved.get('omdb_environment_status', {})
            status = tested.get('status', 'Not tested') if tested.get('key_hash') == fingerprint else 'Not tested'
        return {'status': status, 'saved': configured, 'override': bool(override)}

    @app.post('/settings/omdb')
    def save_omdb_settings():
        action = request.form.get('action')
        path = app.config['CREDENTIALS_FILE']
        override = os.environ.get('OMDB_API_KEY', '').strip()
        try:
            if action == 'remove':
                update_credentials(path, {}, remove=('omdb_api_key', 'omdb_status'))
                flash('Saved OMDb key removed.' + (' The environment override remains active.' if override else ''))
            elif action in ('save', 'test'):
                if action == 'save':
                    if override:
                        flash('OMDB_API_KEY overrides the saved key. Remove that environment variable to replace the key here.')
                        return redirect(url_for('settings'))
                    key = request.form.get('api_key', '').strip()
                    if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', key):
                        flash('Enter a valid OMDb API key.')
                        return redirect(url_for('settings'))
                else:
                    with using_credentials(path):
                        key = omdb_api_key()
                    if not key:
                        flash('OMDb is not configured. Enter an API key first.')
                        return redirect(url_for('settings'))
                status = test_omdb_api_key(key)
                if override:
                    update_credentials(path, {'omdb_environment_status': {
                        'key_hash': hashlib.sha256(key.encode()).hexdigest(), 'status': status}})
                else:
                    update_credentials(path, {'omdb_api_key': key, 'omdb_status': status})
                flash('OMDb: ' + status + '. Changes take effect immediately.')
            else:
                abort(400)
        except (OSError, ValueError):
            flash('Could not update OMDb settings. Check access to the local credentials file.')
        return redirect(url_for('settings'))

    @app.post('/settings')
    def save_settings():
        # Appearance buttons save one preference; the field checklist stores only explicitly hidden tokens.
        if request.form.get('action') == 'default_category':
            category = request.form.get('default_category')
            if category not in COLLECTION_CATEGORIES:
                abort(400)
            with connect(app) as db:
                db.execute("INSERT OR REPLACE INTO settings (key,value_json) VALUES ('default_category',?)", (json.dumps(category),))
            flash(f'Default category saved: {COLLECTION_CATEGORIES[category]}.')
            return redirect(url_for('settings'))
        if request.form.get('action') == 'colour_scheme':
            scheme = request.form.get('colour_scheme')
            if scheme not in COLOUR_SCHEMES:
                abort(400)
            with connect(app) as db:
                db.execute("INSERT OR REPLACE INTO settings (key,value_json) VALUES ('colour_scheme',?)", (json.dumps(scheme),))
            flash(f'Colour scheme saved: {COLOUR_SCHEMES[scheme]}.')
            return redirect(url_for('settings'))
        if request.form.get('action') == 'theme':
            theme = request.form.get('theme')
            if theme not in ('light', 'dark'):
                abort(400)
            with connect(app) as db:
                db.execute("INSERT OR REPLACE INTO settings (key,value_json) VALUES ('theme',?)", (json.dumps(theme),))
            flash(f'Appearance saved: {theme.capitalize()}.')
            return redirect(url_for('settings'))
        if request.form.get('action') == 'default_view':
            view = request.form.get('default_view')
            if view not in ('grid', 'list'):
                abort(400)
            with connect(app) as db:
                db.execute("INSERT OR REPLACE INTO settings (key,value_json) VALUES ('default_view',?)", (json.dumps(view),))
            flash(f'Default view saved: {view.capitalize()}.')
            return redirect(url_for('settings'))
        if request.form.get('action') == 'reset':
            hidden = []
        else:
            # Unchecked known fields become hidden; fields discovered later remain visible by default.
            known = set(request.form.getlist('known_field'))
            visible = set(request.form.getlist('field'))
            if len(known) > 2048 or not visible.issubset(known):
                abort(400)
            for token in known:
                try:
                    path = json.loads(token)
                except (ValueError, TypeError):
                    abort(400)
                if (len(token) > 8192 or not isinstance(path, list) or not path or len(path) > 128 or
                        not isinstance(path[0], str) or any(part is not None and not isinstance(part, str) for part in path)):
                    abort(400)
                if field_token(path) != token:
                    abort(400)
            hidden = sorted(known - visible)
        with connect(app) as db:
            db.execute("INSERT OR REPLACE INTO settings (key,value_json) VALUES ('hidden_metadata_fields',?)", (json.dumps(hidden,ensure_ascii=False),))
        flash('All metadata JSON fields are now shown.' if request.form.get('action') == 'reset' else 'Settings saved.')
        return redirect(url_for('settings'))

    def insert_books(rows):
        # Insert parents before children and translate exported IDs to local IDs inside one transaction.
        added = 0
        import_ids = {book['_import_id'] for book in rows if '_import_id' in book}
        mapped_ids = {}
        remaining = list(rows)
        with connect(app) as db:
            for book in rows:
                for name in book.get('_tag_catalog', []) + book.get('tags', []):
                    ensure_tag(db, name)
            while remaining:
                progress = False
                for original in list(remaining):
                    book = dict(original)
                    child = book['kind'] in ('tv_season', 'tv_episode')
                    if child and '_import_id' in book:
                        if book['parent_id'] not in import_ids:
                            raise ValueError('The export is missing a parent show or season.')
                        # Defer a child until its exported parent has been inserted or mapped to an existing duplicate.
                        if book['parent_id'] not in mapped_ids:
                            continue
                        book['parent_id'] = mapped_ids[book['parent_id']]
                    parent_key = ''
                    duplicate = None
                    if child:
                        parent = db.execute('SELECT * FROM titles WHERE id=?', (book['parent_id'],)).fetchone()
                        if parent is None or CHILD_KINDS.get(parent['kind']) != book['kind']:
                            raise ValueError('Choose the correct parent show or season for this title.')
                        parent_key = json.loads(parent['metadata_json']).get('match', {}).get('key', '')
                        duplicate = db.execute("SELECT id FROM titles WHERE parent_id=? AND kind=? AND (parent_source_key=? OR parent_source_key='') AND ((? IS NULL AND title=? COLLATE NOCASE) OR (? IS NOT NULL AND position=?))",
                                               (book['parent_id'], book['kind'], parent_key, book.get('position'), book['title'], book.get('position'), book.get('position'))).fetchone()
                    if duplicate:
                        saved_id = duplicate['id']
                    else:
                        cursor = db.execute('INSERT OR IGNORE INTO titles (title,author,isbn,notes,identity,added_at,reading_status,kind,parent_id,position,source_key,parent_source_key,eidr,alternate_title,year) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                                            (book['title'], book['author'], book['isbn'], book['notes'], identity(book), now(), book['reading_status'], book['kind'],
                                             book.get('parent_id'), book.get('position'), book.get('source_key', ''), parent_key, book.get('eidr', ''), book.get('alternate_title', ''), book.get('year', '')))
                        added += cursor.rowcount
                        saved_id = cursor.lastrowid if cursor.rowcount else db.execute('SELECT id FROM titles WHERE identity=?', (identity(book),)).fetchone()['id']
                    if 'tags' in book:
                        # Duplicate imports can restore/merge explicit tags without overwriting other title data.
                        current = [row['tag_id'] for row in db.execute(
                            'SELECT tag_id FROM title_tags WHERE title_id=? AND enabled=1', (saved_id,))]
                        replace_title_tags(db, saved_id, current + [ensure_tag(db, name) for name in book['tags']])
                    if '_import_id' in book:
                        mapped_ids[book['_import_id']] = saved_id
                    remaining.remove(original)
                    progress = True
                if not progress:
                    raise ValueError('The export has an invalid or circular TV hierarchy.')
        return added

    @app.get('/search-suggestions')
    def search_suggestions():
        query = request.args.get('q', '').strip().casefold()
        category = request.args.get('category', default_category())
        if category not in ALL_CATEGORIES:
            abort(400)
        with connect(app) as db:
            if category in ('tv_season', 'tv_episode'):
                try:
                    parent_id = int(request.args.get('parent_id', ''))
                    parent = tv_parent(parent_id, category)
                except ValueError:
                    abort(400)
                key = parent['match'].get('key', '')
                rows = db.execute("""SELECT id,title,alternate_title FROM titles WHERE kind=? AND parent_id=?
                    AND (parent_source_key=? OR parent_source_key='')""", (category, parent_id, key)).fetchall()
            else:
                rows = db.execute('SELECT id,title,alternate_title FROM titles WHERE kind=?', (category,)).fetchall()
            books = [dict(row) for row in rows]
            attach_tags(db, books)
        if not query:
            return {'suggestions': []}
        terms = {}
        for book in books:
            for name, label in [(book['title'], 'Title'), (book['alternate_title'], 'Title')] + [
                    (tag['name'], 'Tag') for tag in book['tags']]:
                if name and query in name.casefold():
                    terms.setdefault((name.casefold(), label), {'value': name, 'label': label})
        suggestions = sorted(terms.values(), key=lambda term: (
            not term['value'].casefold().startswith(query), term['value'].casefold(), term['label']))
        return {'suggestions': suggestions[:10]}

    @app.get('/')
    def index():
        # Search only the selected category; counts describe the full collection before search filtering.
        query = request.args.get('q', '').strip()
        selected_letter = request.args.get('letter', '').upper()
        if selected_letter not in ('', '#', *'ABCDEFGHIJKLMNOPQRSTUVWXYZ'):
            abort(400)
        selected_tag = requested_tag()
        category = request.args.get('category', default_category())
        if category not in COLLECTION_CATEGORIES:
            abort(400)
        with connect(app) as db:
            rows = db.execute('SELECT * FROM titles WHERE kind=? ORDER BY title COLLATE NOCASE', (category,)).fetchall()
            counts = {row['kind']: row['total'] for row in db.execute('SELECT kind,COUNT(*) AS total FROM titles GROUP BY kind')}
        books = []
        pending = sum(row['status'] == 'pending' for row in rows)
        for row in rows:
            book = dict(row)
            book['match'] = json.loads(book['metadata_json']).get('match', {})
            book['display_author'] = book['author'] or ', '.join(book['match'].get('author_name', [])) or 'Author unknown'
            book['authors'] = book_authors(book)
            books.append(book)
        with connect(app) as db:
            attach_tags(db, books)
        books = [book for book in books if matches_search(book, query)]
        if selected_letter:
            books = [book for book in books if title_initial(book['title']) == selected_letter]
        if selected_tag:
            books = [book for book in books if any(tag['id'] == selected_tag['id'] for tag in book['tags'])]
        return render_template('index.html', books=books, selected_tag=selected_tag, selected_letter=selected_letter,
                               total=len(rows), pending=pending, query=query,
                               category=category, categories=COLLECTION_CATEGORIES, category_counts=counts,
                               category_label='TV shows' if category == 'tv' else COLLECTION_CATEGORIES[category].lower())

    @app.get('/author')
    def author_titles():
        # Search the local Books collection using individual author names without making provider requests.
        name = request.args.get('name', '').strip()
        if not name or len(name) > 500:
            abort(400)
        local_books = []
        with connect(app) as db:
            rows = db.execute("SELECT * FROM titles WHERE kind='book' ORDER BY title COLLATE NOCASE").fetchall()
        for row in rows:
            book = dict(row)
            book['match'] = json.loads(book['metadata_json']).get('match', {})
            if any(normalize(author) == normalize(name) for author in book_authors(book)):
                book['display_author'] = book['author'] or ', '.join(book_authors(book)) or 'Author unknown'
                local_books.append(book)
        with connect(app) as db:
            attach_tags(db, local_books)
        return render_template('author.html', name=name, local_books=local_books)

    @app.post('/books')
    def add():
        category = request.form.get('kind', 'book')
        destination = url_for('index', category=category if category in COLLECTION_CATEGORIES else 'tv')
        try:
            book = validate(request.form)
            imdb_link = request.form.get('imdb_link', '').strip()
            if category == 'movie' and imdb_link:
                confirm_movie_title(imdb_link, book['title'], normalize)
            if category in ('tv_season', 'tv_episode'):
                tv_parent(book['parent_id'], category)
                destination = collection_url(book)
            added = insert_books([book])
            label = SINGULAR_CATEGORIES[category]
            message = f'{TITLE_LABELS[category]} added. Metadata lookup queued.' if added else f'That {label} is already in your library.'
            flash('IMDb title confirmed. ' + message if category == 'movie' and imdb_link else message)
        except ValueError as exc:
            flash(str(exc))
        return redirect(destination)

    @app.post('/import')
    def import_books():
        upload = request.files.get('file')
        category = request.form.get('kind', 'book')
        destination = url_for('index', category=category if category in COLLECTION_CATEGORIES else 'tv')
        try:
            if not upload or not upload.filename:
                raise ValueError('Choose a file to import.')
            validate_kind(category)
            parent_id = request.form.get('parent_id', type=int)
            if category in ('tv_season', 'tv_episode'):
                parent = tv_parent(parent_id, category)
                destination = title_url(parent)
            content = upload.read()
            rows = parse_import(upload.filename, content, category, parent_id)
            added = insert_books(rows)
            if not rows:
                # An empty-library export can still contain unused custom tags.
                with connect(app) as db:
                    for name in json.loads(content.decode('utf-8-sig')).get('tags', []):
                        ensure_tag(db, name)
            flash(f'Imported {added} titles; skipped {len(rows) - added} duplicates. Metadata lookup queued.')
        except (ValueError, UnicodeError, csv.Error) as exc:
            flash(str(exc))
        return redirect(destination)

    @app.get('/books/<int:book_id>')
    def detail(book_id):
        # Prefix episode headings for display while preserving the original stored title.
        book = get_book(book_id)
        page_heading = book['title']
        if book['kind'] == 'tv_episode':
            season = get_book(book['parent_id'])
            season_label = f"Season {season['position']}" if season['position'] is not None else season['title']
            episode_label = f"Episode {book['position']}" if book['position'] is not None else 'Special'
            page_heading = f"{season_label} : {episode_label} : {book['title']}"
        displayed = filtered_metadata(book['metadata'], hidden_metadata_fields())
        return render_template('detail.html', book=book, page_heading=page_heading, displayed_metadata=displayed, metadata_filtered=displayed != book['metadata'])

    def match_candidates(book):
        if book['status'] != 'ambiguous':
            abort(409, description='This title no longer needs a match selection. Refresh the page.')
        return book['metadata'].get('candidates', [])

    def candidate_book(book, candidate):
        # Preview candidate artwork without applying the title’s existing explicit cover selection.
        return {**book, 'metadata_json': json.dumps(candidate), 'cover_choice': ''}

    @app.get('/books/<int:book_id>/match-options')
    def match_options(book_id):
        # Return compact modal data and bind candidate preview URLs to the current title revision.
        book = get_book(book_id)
        candidates = match_candidates(book)
        return {'title': book['title'], 'kind': book['kind'], 'revision': book['revision'], 'candidates': [
            {'choice': index, 'title': candidate['match'].get('title', book['title']),
             'authors': candidate['match'].get('author_name', []),
             'year': candidate['match'].get('first_publish_year', ''),
             'description': candidate['match'].get('description', '')[:300], 'provider': candidate.get('provider', ''),
             'image_url': url_for('match_option_image', book_id=book_id, choice=index, revision=book['revision'])
             if cover_identifier(candidate_book(book, candidate)) else None}
            for index, candidate in enumerate(candidates)]}

    @app.get('/books/<int:book_id>/match-options/<int:choice>/image')
    def match_option_image(book_id, choice):
        book = get_book(book_id)
        candidates = match_candidates(book)
        if request.args.get('revision', type=int) != book['revision']:
            abort(409)
        if choice >= len(candidates):
            abort(404)
        try:
            image = cached_cover_image(cover_identifier(candidate_book(book, candidates[choice])))
        except (requests.RequestException, ValueError, OSError):
            abort(502)
        if not image:
            abort(404)
        return app.response_class(image, mimetype='image/png' if image.startswith(b'\x89PNG') else 'image/jpeg')

    @app.post('/books/<int:book_id>/match/select')
    def select_match(book_id):
        # Lock the transaction and reject stale modal choices before applying a selected provider record.
        try:
            choice = int(request.form.get('choice', ''))
            revision = int(request.form.get('revision', ''))
        except ValueError:
            return {'error': 'Choose one of the displayed titles.'}, 400
        with connect(app) as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM titles WHERE id=?', (book_id,)).fetchone()
            if row is None:
                abort(404)
            book = dict(row)
            if book['status'] != 'ambiguous' or book['revision'] != revision:
                return {'error': 'This title changed. Refresh the page before choosing a match.'}, 409
            stored = json.loads(book['metadata_json'])
            candidates = stored.get('candidates', [])
            if choice < 0 or choice >= len(candidates):
                return {'error': 'Choose one of the displayed titles.'}, 400
            metadata = candidates[choice]
            if 'response' not in metadata and stored.get('response') is not None:
                metadata['response'] = stored['response']
            uploaded = book['cover_choice'].startswith('upload:')
            db.execute("UPDATE titles SET metadata_json=?,match_choice=?,status='matched',error='',cover_key=?,cover_choice=?,cover_status=?,cover_error='',description='',description_status='pending',description_error='',revision=revision+1 WHERE id=?",
                       (json.dumps(metadata, ensure_ascii=False), candidate_key(metadata),
                        book['cover_key'] if uploaded else '', book['cover_choice'] if uploaded else '',
                        book['cover_status'] if uploaded else 'pending', book_id))
            save_metadata_author(db, {**book, 'revision': revision + 1}, metadata)
            sync_metadata_tags(db, book_id, metadata)
            if book['kind'] in ('tv_season', 'tv_episode'):
                db.execute('UPDATE titles SET source_key=? WHERE id=?', (metadata['match'].get('key', ''), book_id))
        return {'message': 'Title selected. Description and cover lookup queued.'}

    @app.post('/books/<int:book_id>/reading-status')
    def change_reading_status(book_id):
        # Reading/watching progress is independent of metadata lookup and does not invalidate cached details.
        book = get_book(book_id)
        try:
            reading_status = validate_reading_status(request.form.get('reading_status'))
            with connect(app) as db:
                db.execute('UPDATE titles SET reading_status=? WHERE id=?', (reading_status, book_id))
            label = reading_status if book['kind'] == 'book' else {'Read': 'Watched', 'To Be Read': 'To Watch'}[reading_status]
            flash(f'Status saved: {label}.')
        except ValueError as exc:
            flash(str(exc))
        if request.form.get('location') == 'library':
            args = {'q': request.form.get('q', '')}
            letter = request.form.get('letter', '').upper()
            if book['kind'] in COLLECTION_CATEGORIES and letter in ('#', *'ABCDEFGHIJKLMNOPQRSTUVWXYZ'):
                args['letter'] = letter
            tag_id = request.form.get('tag', type=int)
            if tag_id:
                with connect(app) as db:
                    if db.execute('SELECT 1 FROM tags WHERE id=?', (tag_id,)).fetchone():
                        args['tag'] = tag_id
            if book['kind'] == 'tv_season':
                return redirect(url_for('tv_seasons', show_id=book['parent_id'], **args))
            if book['kind'] == 'tv_episode':
                return redirect(url_for('tv_episodes', season_id=book['parent_id'], **args))
            if book['kind'] != 'book' or 'letter' in args:
                args['category'] = book['kind']
            return redirect(url_for('index', **args))
        return redirect(url_for('detail', book_id=book_id))

    @app.get('/books/<int:book_id>/cover')
    def cover(book_id):
        # Serve cached original bytes with an ETag so browsers can revalidate without downloading unchanged artwork.
        book = get_book(book_id)
        with connect(app) as db:
            cached = db.execute('SELECT image FROM covers WHERE identifier=?', (book['cover_key'],)).fetchone()
        if cached is None:
            abort(404)
        response = app.response_class(cached['image'], mimetype=cover_mimetype(cached['image']))
        response.set_etag(hashlib.sha256(cached['image']).hexdigest())
        response.headers['Cache-Control'] = 'private, max-age=0, must-revalidate'
        return response.make_conditional(request)

    def options_page(book, offset, fetch=False):
        # The cache marker distinguishes media artwork from explicitly English book editions.
        work_key = book['match'].get('key', '')
        media = book['kind'] != 'book'
        if not isinstance(work_key, str) or not re.fullmatch(
                r'(itunes|tvmaze|tvmaze-season|tvmaze-episode|wikipedia):\d+|omdb:tt\d+|' + KEY_PATTERNS[book['kind']]
                if media else r'/works/OL\d+W', work_key):
            return {'options': [], 'next_offset': None}
        with connect(app) as db:
            row = db.execute('SELECT payload FROM cover_options WHERE work_key=? AND offset=?', (work_key, offset)).fetchone()
        if row:
            cached_page = json.loads(row['payload'])
            if cached_page.get('language') == ('media' if media else 'eng'):
                return cached_page
        if not fetch:
            abort(404)
        page = {**(app.config['FETCH_MEDIA_COVER_OPTIONS'](book, offset) if media else app.config['FETCH_COVER_OPTIONS'](work_key, offset)),
                'language': 'media' if media else 'eng'}
        with connect(app) as db:
            db.execute('INSERT OR REPLACE INTO cover_options (work_key,offset,payload) VALUES (?,?,?)', (work_key,offset,json.dumps(page)))
        return page

    def option_offset():
        # Accept only bounded provider page offsets; both picker providers use pages of 50.
        try:
            offset = int(request.values.get('offset', 0))
        except ValueError:
            abort(400)
        if offset < 0 or offset > 10000 or offset % 50:
            abort(400)
        return offset

    def selected_option(book, cover_id, offset):
        # Selections must belong to a previously cached options page, rather than an arbitrary submitted image ID.
        option = next((item for item in options_page(book, offset)['options'] if item['cover_id'] == cover_id), None)
        if option is None:
            abort(404)
        return option

    def option_identifier(option):
        return 'poster:' + poster_url(option['poster_url']) if option.get('poster_url') else f"id:{option['cover_id']}"

    @app.get('/books/<int:book_id>/cover-options')
    def cover_choices(book_id):
        book = get_book(book_id)
        offset = option_offset()
        try:
            page = options_page(book, offset, fetch=True)
        except (requests.RequestException, ValueError, TypeError, KeyError):
            return {'error': 'Could not load editions. Check your connection and try again.'}, 502
        return {**page, 'current': book['cover_key'], 'options': [
            {**item, 'selected': book['cover_key'] == option_identifier(item),
             'image_url': url_for('cover_option_image', book_id=book_id, cover_id=item['cover_id'], offset=offset)}
            for item in page['options']]}

    @app.get('/books/<int:book_id>/cover-options/<int:cover_id>/image')
    def cover_option_image(book_id, cover_id):
        option = selected_option(get_book(book_id), cover_id, option_offset())
        try:
            image = cached_cover_image(option_identifier(option))
        except (requests.RequestException, ValueError, OSError):
            abort(502)
        if not image:
            abort(404)
        return app.response_class(image, mimetype='image/png' if image.startswith(b'\x89PNG') else 'image/jpeg', headers={'Cache-Control': 'private, max-age=86400'})

    @app.post('/books/<int:book_id>/cover/select')
    def select_cover(book_id):
        # Download and cache first, then check the revision again before attaching the user’s choice.
        book = get_book(book_id)
        try:
            cover_id = int(request.form.get('cover_id', ''))
        except ValueError:
            abort(400)
        option = selected_option(book, cover_id, option_offset())
        try:
            identifier = option_identifier(option)
            image = cached_cover_image(identifier)
        except (requests.RequestException, ValueError, OSError):
            return {'error': 'Could not save this cover. Please try again.'}, 502
        if not image:
            return {'error': 'This edition’s cover is unavailable. Please choose another.'}, 404
        with connect(app) as db:
            # Protect an edit made while a cover download was in progress.
            db.execute('BEGIN IMMEDIATE')
            current = db.execute('SELECT * FROM titles WHERE id=?', (book_id,)).fetchone()
            if (not current or current['revision'] != book['revision'] or
                    json.loads(current['metadata_json']).get('match', {}).get('key') != book['match'].get('key')):
                return {'error': 'The book changed. Close the picker and reopen it.'}, 409
            metadata = json.loads(current['metadata_json'])
            metadata['cover_selection'] = option
            db.execute("UPDATE titles SET cover_choice=?,cover_key=?,cover_status='available',cover_error='',metadata_json=?,revision=revision+1 WHERE id=?",
                       (identifier,identifier,json.dumps(metadata,ensure_ascii=False),book_id))
        return {'image_url': url_for('cover', book_id=book_id, v=cover_id)}

    @app.post('/books/<int:book_id>/cover/upload')
    def upload_cover(book_id):
        book = get_book(book_id)
        upload = request.files.get('cover_file')
        try:
            revision = int(request.form.get('revision', ''))
            if revision != book['revision']:
                flash('This title changed. Reload the page before adding a cover.')
                return redirect(url_for('detail', book_id=book_id))
            if upload is None or not upload.filename:
                raise ValueError('Choose a JPEG, PNG, or WebP image.')
            image = validate_cover_image(upload.read(MAX_COVER_BYTES + 1))
        except ValueError as exc:
            flash(str(exc) if upload is not None else 'Choose a JPEG, PNG, or WebP image.')
            return redirect(url_for('detail', book_id=book_id))
        identifier = 'upload:' + hashlib.sha256(image).hexdigest()
        with connect(app) as db:
            db.execute('BEGIN IMMEDIATE')
            current = db.execute('SELECT * FROM titles WHERE id=?', (book_id,)).fetchone()
            if current is None:
                abort(404)
            if current['revision'] != revision:
                flash('This title changed. Reload the page before adding a cover.')
                return redirect(url_for('detail', book_id=book_id))
            metadata = json.loads(current['metadata_json'])
            metadata.pop('cover_selection', None)
            db.execute('INSERT OR IGNORE INTO covers (identifier,image,fetched_at) VALUES (?,?,?)', (identifier, image, now()))
            db.execute("UPDATE titles SET cover_choice=?,cover_key=?,cover_status='available',cover_error='',metadata_json=?,revision=revision+1 WHERE id=?",
                       (identifier, identifier, json.dumps(metadata, ensure_ascii=False), book_id))
        flash('Cover added and saved locally.')
        return redirect(url_for('detail', book_id=book_id))

    @app.post('/books/<int:book_id>/cover/retry')
    def retry_cover(book_id):
        get_book(book_id)
        with connect(app) as db:
            db.execute("UPDATE titles SET cover_status='pending',cover_error='',revision=revision+1 WHERE id=?", (book_id,))
        flash('Cover lookup queued.')
        return redirect(url_for('detail', book_id=book_id))

    @app.post('/books/<int:book_id>/description/retry')
    def retry_description(book_id):
        # Media retries request fresh provider text; book descriptions continue to use their shared work cache.
        book = get_book(book_id)
        with connect(app) as db:
            metadata = book['metadata']
            if book['kind'] != 'book':
                metadata['refresh_description'] = True
            db.execute("UPDATE titles SET description_status='pending',description_error='',metadata_json=?,revision=revision+1 WHERE id=?", (json.dumps(metadata, ensure_ascii=False), book_id))
        flash('Description lookup queued.')
        return redirect(url_for('detail', book_id=book_id))

    @app.post('/books/<int:book_id>/edit')
    def edit(book_id):
        # Only lookup identity fields trigger a fresh search; review-only edits retain the saved metadata.
        old = get_book(book_id)
        try:
            book = validate({**request.form, 'kind': old['kind'], 'parent_id': old['parent_id'],
                             'position': old['position'], 'source_key': old['source_key']})
            if 'reading_status' not in request.form:
                book['reading_status'] = old['reading_status']
            if 'eidr' not in request.form and old['kind'] in ('movie', 'tv'):
                book['eidr'] = old['eidr']
            if old['kind'] == 'movie' and 'alternate_title' not in request.form:
                book['alternate_title'] = old['alternate_title']
            if old['kind'] == 'movie' and 'year' not in request.form:
                book['year'] = old['year']
            changed = any(book[key] != old[key] for key in ('title', 'author', 'isbn'))
            year_changed = old['kind'] == 'movie' and book['year'] != old['year']
            lookup_changed = changed or year_changed
            child = old['kind'] in ('tv_season', 'tv_episode')
            with connect(app) as db:
                db.execute('UPDATE titles SET title=?,author=?,isbn=?,notes=?,identity=?,reading_status=?,revision=revision+1, status=?,metadata_json=?,error=? WHERE id=?',
                           (book['title'], book['author'], book['isbn'], book['notes'], identity(book), book['reading_status'],
                            'pending' if lookup_changed else old['status'], '{}' if lookup_changed and not child else old['metadata_json'],
                            '' if lookup_changed else old['error'], book_id))
                if old['kind'] in ('movie', 'tv'):
                    db.execute('UPDATE titles SET eidr=? WHERE id=?', (book['eidr'], book_id))
                if old['kind'] == 'movie':
                    db.execute('UPDATE titles SET alternate_title=?,year=? WHERE id=?', (book['alternate_title'], book['year'], book_id))
                if lookup_changed:
                    sync_metadata_tags(db, book_id, {})
                    db.execute("UPDATE titles SET match_choice='' WHERE id=?", (book_id,))
                    db.execute("UPDATE titles SET description='',description_status='pending',description_error='' WHERE id=?", (book_id,))
                if changed:
                    db.execute("UPDATE titles SET cover_key='',cover_choice='',cover_status='pending',cover_error='' WHERE id=?", (book_id,))
            flash(f"{TITLE_LABELS[old['kind']]} saved." + (' Metadata lookup queued.' if lookup_changed else ''))
        except (ValueError, sqlite3.IntegrityError) as exc:
            flash(str(exc) if isinstance(exc, ValueError) else 'Another book already has those details.')
        return redirect(url_for('detail', book_id=book_id))

    @app.post('/books/<int:book_id>/retry')
    def retry(book_id):
        get_book(book_id)
        # A deliberate new lookup must offer ambiguous matches again instead of reusing the saved choice.
        with connect(app) as db:
            db.execute("UPDATE titles SET status='pending',match_choice='',error='',revision=revision+1 WHERE id=?", (book_id,))
        flash('Metadata lookup queued.')
        return redirect(url_for('detail', book_id=book_id))

    @app.post('/books/<int:book_id>/delete')
    def delete(book_id):
        # SQLite foreign-key cascades remove seasons and episodes when their parent title is deleted.
        book = get_book(book_id)
        with connect(app) as db:
            db.execute('DELETE FROM titles WHERE id=?', (book_id,))
        flash(f"{TITLE_LABELS[book['kind']]} removed.")
        return redirect(collection_url(book))

    def export_json():
        # Export complete stored metadata regardless of the fields hidden in the details-page display.
        with connect(app) as db:
            titles = [dict(row) for row in db.execute('SELECT * FROM titles ORDER BY id')]
            attach_tags(db, titles)
            names = [tag['name'] for tag in tag_catalog(db)]
        for title in titles:
            title['metadata'] = json.loads(title.pop('metadata_json'))
            title['tags'] = [tag['name'] for tag in title['tags']]
        return json.dumps({'version': 1, 'exported_at': now(), 'tags': names, 'titles': titles}, indent=2, ensure_ascii=False)

    @app.get('/export')
    def export():
        return app.response_class(export_json(),
                                  mimetype='application/json', headers={'Content-Disposition': 'attachment; filename=cinebook.json'})

    @app.post('/export/save')
    def save_export():
        # Allow one native save dialog at a time and release its lock on success, cancellation, or failure.
        if not export_save_lock.acquire(blocking=False):
            return {'error': 'A save dialog is already open.'}, 409
        try:
            filename = app.config['SAVE_EXPORT'](export_json(), app.config['DATABASE'])
            return {'cancelled': filename is None, 'filename': filename}
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
            app.logger.info('JSON export save failed')
            return {'error': 'Could not save the export. Check the destination and try again.'}, 500
        finally:
            export_save_lock.release()

    @app.get('/status')
    def status():
        # The browser polls these states and revisions to notice completed background lookups.
        with connect(app) as db:
            rows = db.execute('SELECT id,status,cover_status,description_status,revision FROM titles ORDER BY id').fetchall()
        return {'books': [dict(row) for row in rows]}

    # The daemon resumes persisted pending jobs on startup without blocking page requests.
    if app.config['START_WORKER']:
        threading.Thread(target=worker, args=(app,), daemon=True, name='book-metadata').start()
    return app


if __name__ == '__main__':
    create_app().run(host='127.0.0.1', port=5000, debug=False, use_reloader=False)
