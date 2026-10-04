"""Curated metadata tags and persistent user-managed title assignments."""
import json
import re
import unicodedata

# Match selected record subjects, never raw search responses or title guesses.
TAG_RULES = {
    'Fiction': r'\bfiction\b|^novel$',
    'Science Fiction': r'\bscience fiction\b|^sci fi$',
    'Fantasy': r'\bfantasy\b',
    'Horror': r'\bhorror\b',
    'Thriller': r'\bthrillers?\b|^suspense$',
    'Romance': r'\bromance\b|^love stories$|^courtship$',
    'Historical Fiction': r'\bhistorical fiction\b|^fiction .*historical\b',
    'Classics': r'\bclassics\b|^classical literature$',
    'Literary Fiction': r'\bliterary fiction\b',
    'Coming of Age': r'\bcoming of age\b',
    'Post-Apocalyptic': r'\bpost apocalyptic\b|^end of the world$',
    'Family': r'^famil(?:y|ies)(?: life| relations)?$|^fiction family life\b|^domestic fiction$',
    'Drama': r'^drama$',
    'Action': r'^action$|^action adventure$',
    'Mystery': r'\bmyster(?:y|ies)\b|^detective fiction$',
    'Adventure': r'^adventure(?: fiction)?$|^action adventure$',
    'Comedy': r'^comedy$|^humorous fiction$',
    'Nonfiction': r'^non fiction$|^nonfiction$',
    'Biography': r'^biograph(?:y|ies)$|^autobiograph(?:y|ies)$|^memoirs?$',
    'Documentary': r'^documentary$',
    'Quests': r'^quests(?: expeditions)?$',
    'Animation': r'^animation$|^animated$',
    'Crime': r'^crime$',
    'War': r'^war$',
    'Western': r'^western$',
    'Musical': r'^musical$',
}


def wikipedia_film_subjects(description):
    """Read explicit genres only from the selected article's opening film definition."""
    if not isinstance(description, str):
        return []
    definition = re.search(r'\b(?:is|was)\s+(?:an?|the)\s+([^\n.]{1,180}?)\bfilm\b',
                           description.split('\n', 1)[0], re.IGNORECASE)
    if not definition:
        return []
    genres = re.sub(r'[^\w]+', ' ', definition.group(1).casefold()).strip()
    patterns = {
        'Science Fiction': r'\b(?:science fiction|sci fi)\b',
        'Fantasy': r'\bfantasy\b', 'Horror': r'\bhorror\b',
        'Thriller': r'\bthriller\b', 'Romance': r'\b(?:romantic|romance)\b',
        'Coming of Age': r'\bcoming of age\b', 'Post-Apocalyptic': r'\bpost apocalyptic\b',
        'Drama': r'\bdrama\b', 'Action': r'\baction\b', 'Mystery': r'\bmystery\b',
        'Adventure': r'\badventure\b', 'Comedy': r'\bcomedy\b',
        'Documentary': r'\bdocumentary\b', 'Biography': r'\bbiographical\b',
        'Animation': r'\b(?:animated|animation)\b', 'Crime': r'\bcrime\b',
        'War': r'\bwar\b', 'Western': r'\bwestern\b', 'Musical': r'\bmusical\b',
    }
    return [name for name, pattern in patterns.items() if re.search(pattern, genres)]


def tag_name(value):
    if not isinstance(value, str) or any(unicodedata.category(c).startswith('C') for c in value):
        raise ValueError('Use a tag name without control characters.')
    name = ' '.join(unicodedata.normalize('NFKC', value).split())
    if not 1 <= len(name) <= 60:
        raise ValueError('Tag names must contain 1–60 characters.')
    return name


def metadata_tags(metadata):
    match = metadata.get('match', {})
    subjects = match.get('subject', [])
    if isinstance(subjects, str):
        subjects = [subjects]
    if not isinstance(subjects, list):
        return set()
    # Older Wikipedia movie matches saved no subjects. Derive the same explicit
    # genres here so rerunning tagging repairs them without another provider lookup.
    if not subjects and str(match.get('key', '')).startswith('wikipedia:'):
        subjects = wikipedia_film_subjects(match.get('description', ''))
    values = [re.sub(r'[^\w]+', ' ', value.casefold()).strip()
              for value in subjects if isinstance(value, str) and not value.casefold().startswith('nyt:')]
    return {name for name, pattern in TAG_RULES.items()
            if any(re.search(pattern, value) and not (name == 'Fiction' and value.startswith('non fiction'))
                   for value in values)}


def ensure_tag(db, name):
    name = tag_name(name)
    key = name.casefold()
    db.execute('INSERT OR IGNORE INTO tags (name,name_key) VALUES (?,?)', (name, key))
    return db.execute('SELECT id FROM tags WHERE name_key=?', (key,)).fetchone()['id']


def tag_catalog(db):
    return [dict(row) for row in db.execute("""SELECT tags.id,tags.name,COUNT(title_tags.title_id) AS title_count
        FROM tags LEFT JOIN title_tags ON tags.id=title_tags.tag_id AND title_tags.enabled=1
        GROUP BY tags.id ORDER BY tags.name COLLATE NOCASE,tags.id""")]


def attach_tags(db, books):
    by_id = {book['id']: book for book in books}
    for book in books:
        book['tags'] = []
    if not by_id:
        return
    ids = list(by_id)
    for start in range(0, len(ids), 500):
        batch = ids[start:start + 500]
        placeholders = ','.join('?' for _ in batch)
        for row in db.execute(f"""SELECT title_tags.title_id,tags.id,tags.name FROM title_tags
            JOIN tags ON tags.id=title_tags.tag_id WHERE title_tags.enabled=1
            AND title_tags.title_id IN ({placeholders})
            ORDER BY tags.name COLLATE NOCASE,tags.id""", batch):
            by_id[row['title_id']]['tags'].append({'id': row['id'], 'name': row['name']})


def sync_metadata_tags(db, title_id, metadata):
    # User selections and explicit exclusions survive new matches and metadata refreshes.
    db.execute("DELETE FROM title_tags WHERE title_id=? AND origin='metadata' AND enabled=1", (title_id,))
    suggestions = {name.casefold() for name in metadata_tags(metadata)}
    for row in db.execute('SELECT id,name_key FROM tags').fetchall():
        if row['name_key'] in suggestions:
            db.execute("INSERT OR IGNORE INTO title_tags (title_id,tag_id,origin,enabled) VALUES (?,?,'metadata',1)",
                       (title_id, row['id']))


def replace_title_tags(db, title_id, ids):
    selected = set(ids)
    if len(selected) > 100:
        raise ValueError('Choose at most 100 tags per title.')
    available = {row['id'] for row in db.execute('SELECT id FROM tags')}
    if not selected.issubset(available):
        raise ValueError('A selected tag no longer exists. Reload the page and try again.')
    # Record exclusions for the current catalog so automatic refresh cannot undo an unchecked tag.
    for tag_id in available:
        db.execute("""INSERT INTO title_tags (title_id,tag_id,origin,enabled) VALUES (?,?,'manual',?)
            ON CONFLICT(title_id,tag_id) DO UPDATE SET origin='manual',enabled=excluded.enabled""",
                   (title_id, tag_id, int(tag_id in selected)))


def suggest_from_library(db):
    rows = db.execute("SELECT id,metadata_json FROM titles WHERE status='matched'").fetchall()
    suggested = set()
    for row in rows:
        suggested.update(metadata_tags(json.loads(row['metadata_json'])))
    before = db.execute('SELECT COUNT(*) FROM tags').fetchone()[0]
    for name in sorted(suggested):
        ensure_tag(db, name)
    for row in rows:
        sync_metadata_tags(db, row['id'], json.loads(row['metadata_json']))
    return db.execute('SELECT COUNT(*) FROM tags').fetchone()[0] - before


def initialize_tags(db):
    db.execute("""CREATE TABLE IF NOT EXISTS tags (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
        name_key TEXT NOT NULL UNIQUE)""")
    db.execute("""CREATE TABLE IF NOT EXISTS title_tags (
        title_id INTEGER NOT NULL REFERENCES titles(id) ON DELETE CASCADE,
        tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
        origin TEXT NOT NULL CHECK(origin IN ('metadata','manual')),
        enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
        PRIMARY KEY(title_id,tag_id))""")
    db.execute('CREATE INDEX IF NOT EXISTS title_tags_by_tag ON title_tags(tag_id,enabled,title_id)')
    # Seed once; deleting a tag must not recreate it on startup or the next provider lookup.
    if not db.execute("SELECT 1 FROM settings WHERE key='tags_initialized'").fetchone():
        suggest_from_library(db)
        db.execute("INSERT INTO settings (key,value_json) VALUES ('tags_initialized','true')")
