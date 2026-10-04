# Integration tests cover local persistence, settings, imports, and independently queued enrichment jobs.
import io
import json
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

import requests
from app import connect, create_app, enrich_one, fetch_one_cover, fetch_cover, fetch_one_description, fetch_description, fetch_cover_options, lookup, parse_import


class LibraryTests(unittest.TestCase):
    def setUp(self):
        # Each test uses a temporary database and disables the worker so queued jobs run only when invoked.
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app({'TESTING': True, 'START_WORKER': False,
                               'DATABASE': str(Path(self.temp.name) / 'books.db')})
        self.client = self.app.test_client()
        self.client.get('/')
        with self.client.session_transaction() as session:
            self.csrf = session['csrf']

    def tearDown(self):
        self.temp.cleanup()

    def post(self, url, **data):
        # Submit the session token and follow redirects so assertions can inspect the resulting page.
        return self.client.post(url, data={'csrf': self.csrf, **data}, follow_redirects=True)

    def book(self):
        with connect(self.app) as db:
            return dict(db.execute('SELECT * FROM titles').fetchone())

    def displayed_metadata(self, client=None):
        # Inspect the rendered JSON, which can differ from the complete metadata retained in storage.
        page = (client or self.client).get('/books/1').data.decode()
        return json.loads(re.search(r'<pre>(.*?)</pre>', page, re.S).group(1))

    def test_settings_filter_nested_fields_and_preserve_source(self):
        self.post('/books', title='Dune', author='Frank Herbert')
        metadata = {'match': {'title': 'Dune', 'isbn': ['123']},
                    'response': {'docs': [{'title': 'Dune', 'isbn': ['123']},
                                          {'title': 'Another edition', 'isbn': ['456']}]}}
        with connect(self.app) as db:
            db.execute('UPDATE titles SET metadata_json=? WHERE id=1', (json.dumps(metadata),))
        self.assertEqual(self.displayed_metadata(), metadata)
        page = self.client.get('/settings').data
        self.assertIn(b'response.docs[].isbn', page)
        known = ['["match","title"]', '["match","isbn"]',
                 '["response","docs",null,"title"]', '["response","docs",null,"isbn"]']
        self.post('/settings', known_field=known, field=known[:3])
        expected = {'match': metadata['match'], 'response': {'docs': [{'title': 'Dune'}, {'title': 'Another edition'}]}}
        self.assertEqual(self.displayed_metadata(), expected)
        self.assertEqual(json.loads(self.book()['metadata_json']), metadata)
        self.assertEqual(self.client.get('/export').json['titles'][0]['metadata'], metadata)
        reopened = create_app({'TESTING': True, 'START_WORKER': False, 'DATABASE': self.app.config['DATABASE']})
        self.assertEqual(self.displayed_metadata(reopened.test_client()), expected)
        metadata['new_field'] = 'Visible by default'
        with connect(self.app) as db:
            db.execute('UPDATE titles SET metadata_json=? WHERE id=1', (json.dumps(metadata),))
        self.assertEqual(self.displayed_metadata()['new_field'], 'Visible by default')
        self.post('/settings', action='reset')
        self.assertEqual(self.displayed_metadata(), metadata)

    def test_settings_hide_all_prunes_empty_branches_and_validates_input(self):
        self.assertIn(b'Metadata fields will appear here', self.client.get('/settings').data)
        self.post('/books', title='Dune')
        with connect(self.app) as db:
            db.execute('UPDATE titles SET metadata_json=? WHERE id=1', ('{"response":{"docs":[{"title":"Dune"}]}}',))
        self.post('/settings', known_field=['["response","docs",null,"title"]'])
        self.assertEqual(self.displayed_metadata(), {})
        self.assertEqual(self.client.post('/settings', data={'action': 'reset'}).status_code, 400)
        for token in ['invalid', '[]', '[3]', '["title",3]', '[ "title" ]']:
            self.assertEqual(self.post('/settings', known_field=[token]).status_code, 400)
        self.assertEqual(self.post('/settings', field=['["title"]']).status_code, 400)
        self.assertEqual(self.displayed_metadata(), {})

    def test_add_duplicate_persist_search_export_delete(self):
        response = self.post('/books', title='The Hobbit', author='Tolkien')
        self.assertIn(b'The Hobbit', response.data)
        self.post('/books', title='the hobbit', author='tolkien')
        self.assertEqual(len(self.client.get('/export').json['titles']), 1)
        self.assertIn(b'The Hobbit', self.client.get('/?q=tolkien').data)
        self.assertIn(b'No Titles Found</h3><p class="search-empty-hint">try another title or author</p>', self.client.get('/?q=missing').data)
        reopened = create_app({'TESTING': True, 'START_WORKER': False, 'DATABASE': self.app.config['DATABASE']})
        self.assertIn(b'The Hobbit', reopened.test_client().get('/').data)
        self.assertEqual(self.client.get('/books/1').status_code, 200)
        self.post('/books/1/delete')
        self.assertEqual(self.client.get('/export').json['titles'], [])

    @patch('app.requests.get')
    def test_author_links_search_only_local_titles(self, get):
        self.post('/books', title='The Hobbit', author='J.R.R. Tolkien')
        self.post('/books', title='The Lord of the Rings', author='J.R.R. Tolkien', reading_status='Read')
        self.post('/books', title='Dune', author='Frank Herbert')
        index = self.client.get('/').data
        self.assertIn(b'/author?name=J.R.R.+Tolkien', index)
        self.assertIn(b'/author?name=J.R.R.+Tolkien', self.client.get('/books/1').data)
        response = self.client.get('/author?name=j+r+r+tolkien')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'The Hobbit', response.data)
        self.assertIn(b'The Lord of the Rings', response.data)
        self.assertNotIn(b'Dune', response.data)
        self.assertIn(b'To Be Read', response.data)
        self.assertIn(b'Read', response.data)
        get.assert_not_called()
        self.assertIn(b'No Titles Found</h3><p class="search-empty-hint">try another title or author</p>', self.client.get('/author?name=Unknown').data)
        self.assertEqual(self.client.get('/author').status_code, 400)

    def test_front_page_categories_filter_collection_and_search(self):
        self.post('/books', title='Dune book', author='Frank Herbert')
        self.post('/books', title='Dune movie', author='Frank Herbert')
        self.post('/books', title='Dune series', author='Frank Herbert')
        with connect(self.app) as db:
            db.execute("UPDATE titles SET kind='movie' WHERE id=2")
            db.execute("UPDATE titles SET kind='tv' WHERE id=3")
        default = self.client.get('/').data
        self.assertIn(b'Dune book', default)
        self.assertNotIn(b'Dune movie', default)
        self.assertNotIn(b'Dune series', default)
        for category, title in [('movie', b'Dune movie'), ('tv', b'Dune series')]:
            page = self.client.get('/', query_string={'category': category, 'q': 'Dune'}).data
            self.assertIn(title, page)
            self.assertNotIn(b'Dune book', page)
            self.assertIn(f'name="category" value="{category}"'.encode(), page)
            self.assertNotIn(b'<h2>Add a book</h2>', page)
            self.assertIn(b'class="reading-status-form"', page)
            self.assertIn(b'To Watch', page)
            self.assertIn(b'data-library-view="list"', page)
        self.assertIn(b'No Titles Found</h3><p class="search-empty-hint">try another title or author</p>', self.client.get('/?category=movie&q=missing').data)
        self.assertNotIn(b'Dune movie', self.client.get('/author?name=Frank+Herbert').data)
        self.assertEqual(self.client.get('/?category=invalid').status_code, 400)

    @patch('app.requests.get')
    def test_coauthors_get_separate_links_and_local_search_results(self, get):
        self.post('/books', title='Joint book', author='Alice Author, Bob Writer')
        with connect(self.app) as db:
            db.execute('UPDATE titles SET metadata_json=? WHERE id=1',
                       (json.dumps({'match':{'author_name':['Alice Author','Bob Writer']}}),))
        index = self.client.get('/').data
        self.assertIn(b'/author?name=Alice+Author', index)
        self.assertIn(b'/author?name=Bob+Writer', index)
        self.assertIn(b'Joint book', self.client.get('/author?name=Bob+Writer').data)
        self.assertIn(b'Joint book', self.client.get('/author?name=Alice+Author').data)
        self.assertNotIn(b'Joint book', self.client.get('/author?name=Alice').data)
        get.assert_not_called()

    def test_reading_status_changes_persists_and_is_independent_of_lookup(self):
        self.post('/books', title='Dune')
        self.assertEqual(self.book()['reading_status'], 'To Be Read')
        original = self.book()
        response = self.post('/books/1/reading-status', reading_status='Read', location='library', q='Dune')
        self.assertIn(b'Status saved: Read.', response.data)
        self.assertIn(b'value="Read" selected', response.data)
        self.assertTrue(response.request.url.endswith('/?q=Dune'))
        self.assertEqual(self.book()['revision'], original['revision'])
        self.assertEqual(self.book()['status'], original['status'])
        self.app.config['LOOKUP'] = lambda book: {'match': {'title': 'Dune'}}
        enrich_one(self.app)
        self.assertEqual(self.book()['reading_status'], 'Read')
        self.post('/books/1/edit', title='Dune', notes='Finished it')
        self.assertEqual(self.book()['reading_status'], 'Read')
        reopened = create_app({'TESTING': True, 'START_WORKER': False, 'DATABASE': self.app.config['DATABASE']})
        self.assertIn(b'value="Read" selected', reopened.test_client().get('/books/1').data)
        exported = self.client.get('/export').data
        self.assertEqual(parse_import('export.json', exported)[0]['reading_status'], 'Read')
        self.post('/books/1/reading-status', reading_status='To Be Read')
        self.assertEqual(self.book()['reading_status'], 'To Be Read')
        self.post('/books/1/reading-status', reading_status='Invalid')
        self.assertEqual(self.book()['reading_status'], 'To Be Read')
        self.assertEqual(self.client.post('/books/1/reading-status', data={'reading_status': 'Read'}).status_code, 400)
        self.assertEqual(self.post('/books/999/reading-status', reading_status='Read').status_code, 404)

    def test_reading_status_can_be_set_on_add_and_import(self):
        self.post('/books', title='Dune', reading_status='Read')
        self.assertEqual(self.book()['reading_status'], 'Read')
        self.post('/import', file=(io.BytesIO(b'title,reading_status\nAnother book,Read'), 'books.csv'))
        exported = self.client.get('/export').json['titles']
        self.assertEqual([book['reading_status'] for book in exported], ['Read', 'Read'])
        self.post('/import', file=(io.BytesIO(b'title,reading_status\nValid,Read\nBad,Unknown'), 'books.csv'))
        self.assertEqual(len(self.client.get('/export').json['titles']), 2)

    def test_import_validation_is_atomic_and_duplicates_skipped(self):
        self.post('/import', file=(io.BytesIO(b'title,author\nDune,Frank Herbert\nDune,Frank Herbert'), 'books.csv'))
        self.assertEqual(len(self.client.get('/export').json['titles']), 1)
        self.post('/import', file=(io.BytesIO(b'title,isbn\nValid,\nBad,nonsense'), 'books.csv'))
        self.assertEqual(len(self.client.get('/export').json['titles']), 1)
        self.assertEqual(parse_import('list.txt', b'One\n\nTwo'), [
            {'title': 'One', 'author': '', 'isbn': '', 'notes': '', 'kind': 'book', 'reading_status': 'To Be Read'},
            {'title': 'Two', 'author': '', 'isbn': '', 'notes': '', 'kind': 'book', 'reading_status': 'To Be Read'}])
        with self.assertRaises(ValueError):
            parse_import('list.json', b'{"titles":[1]}')
        with self.assertRaises(ValueError):
            parse_import('list.txt', ('Title\n' * 26).encode())
        exported = self.client.get('/export').data
        self.assertEqual(parse_import('export.json', exported)[0]['title'], 'Dune')

    def test_metadata_success_failure_retry_and_edit(self):
        self.post('/books', title='Dune')
        metadata = {'provider': 'Open Library', 'match': {'title': 'Dune', 'author_name': ['Frank Herbert'], 'first_publish_year': 1965}, 'response': {'docs': []}}
        self.app.config['LOOKUP'] = lambda book: metadata
        self.assertTrue(enrich_one(self.app))
        self.assertEqual(self.book()['status'], 'matched')
        self.assertEqual(json.loads(self.book()['metadata_json']), metadata)
        self.assertIn(b'Frank Herbert', self.client.get('/?q=herbert').data)
        self.post('/books/1/retry')
        self.app.config['LOOKUP'] = Mock(side_effect=requests.Timeout())
        enrich_one(self.app)
        self.assertEqual(json.loads(self.book()['metadata_json']), metadata)
        self.post('/books/1/retry')
        self.app.config['LOOKUP'] = lambda book: metadata
        enrich_one(self.app)
        self.post('/books/1/edit', title='Dune', author='Frank Herbert', notes='Read again')
        self.assertEqual(self.book()['status'], 'matched')
        self.post('/books/1/edit', title='Dune Messiah')
        self.assertEqual(self.book()['status'], 'pending')
        self.assertEqual(self.book()['metadata_json'], '{}')
        self.app.config['LOOKUP'] = Mock(side_effect=requests.Timeout())
        enrich_one(self.app)
        self.assertEqual(self.book()['status'], 'failed')
        self.post('/books/1/retry')
        self.app.config['LOOKUP'] = lambda book: None
        enrich_one(self.app)
        self.assertEqual(self.book()['status'], 'unmatched')
        self.assertIn(b'No exact match found', self.client.get('/books/1').data)
        self.assertFalse(enrich_one(self.app))

    @patch('app.requests.get')
    def test_lookup_saves_canonical_author_spelling_in_database(self, get):
        get.return_value.json.return_value = {'docs':[{'title':'The Hobbit','author_name':['J. R. R. Tolkien']}]}
        self.post('/books', title='The Hobbit', author='j r r tolkien')
        enrich_one(self.app)
        book = self.book()
        self.assertEqual(book['status'], 'matched')
        self.assertEqual(book['author'], 'J. R. R. Tolkien')
        self.assertEqual(book['identity'], 'the hobbit|j r r tolkien')
        self.assertIn(b'J. R. R. Tolkien', self.client.get('/').data)
        self.assertIn(b'value="J. R. R. Tolkien"', self.client.get('/books/1').data)
        self.assertEqual(self.client.get('/export').json['titles'][0]['author'], 'J. R. R. Tolkien')

    def test_metadata_authors_fill_blank_names_and_keep_all_authors(self):
        self.post('/books', title='A book')
        self.app.config['LOOKUP'] = lambda book: {'match':{'author_name':[' Alice Author ','Bob Writer','Alice Author']}}
        enrich_one(self.app)
        self.assertEqual(self.book()['author'], 'Alice Author, Bob Writer')
        self.assertEqual(self.book()['identity'], 'a book|alice author bob writer')
        self.post('/books/1/retry')
        self.app.config['LOOKUP'] = lambda book: {'match':{}}
        enrich_one(self.app)
        self.assertEqual(self.book()['author'], 'Alice Author, Bob Writer')
        self.post('/books/1/retry')
        self.app.config['LOOKUP'] = Mock(side_effect=requests.Timeout())
        enrich_one(self.app)
        self.assertEqual(self.book()['author'], 'Alice Author, Bob Writer')

    def test_cached_metadata_backfills_author_and_handles_identity_collision(self):
        self.post('/books', title='Dune', author='Frank Herbert')
        self.post('/books', title='Dune')
        with connect(self.app) as db:
            db.execute("UPDATE titles SET metadata_json=?,status='matched' WHERE id=2", (json.dumps({'match':{'author_name':['Frank Herbert']}}),))
        upgraded = create_app({'TESTING':True,'START_WORKER':False,'DATABASE':self.app.config['DATABASE']})
        with connect(upgraded) as db:
            books = db.execute('SELECT * FROM titles ORDER BY id').fetchall()
            self.assertEqual(len(books), 2)
            self.assertEqual(books[1]['author'], 'Frank Herbert')
            self.assertNotEqual(books[0]['identity'], books[1]['identity'])
        self.assertEqual(upgraded.test_client().get('/').status_code, 200)

    def test_edit_during_lookup_does_not_overwrite_new_entry(self):
        self.post('/books', title='Old title')
        def change_during_lookup(book):
            self.post('/books/1/edit', title='New title')
            return {'match': {'title': 'Old title', 'author_name': ['Old Author']}}
        self.app.config['LOOKUP'] = change_during_lookup
        enrich_one(self.app)
        self.assertEqual(self.book()['title'], 'New title')
        self.assertEqual(self.book()['author'], '')
        self.assertEqual(self.book()['status'], 'pending')
        self.assertEqual(self.book()['metadata_json'], '{}')

    def test_security_and_escaping(self):
        self.assertEqual(self.client.post('/books', data={'title': 'Oops'}).status_code, 400)
        self.assertEqual(self.client.get('/', headers={'Host': 'evil.example'}).status_code, 403)
        response = self.post('/books', title='<script>alert(1)</script>')
        self.assertNotIn(b'<script>alert(1)</script>', response.data)
        self.assertIn(b'&lt;script&gt;', response.data)
        self.assertIn("default-src 'self'", response.headers['Content-Security-Policy'])

    def test_cover_cached_served_offline_and_reused(self):
        self.post('/books', title='Dune')
        self.app.config['LOOKUP'] = lambda book: {'match': {'cover_i': 123, 'title': 'Dune'}}
        enrich_one(self.app)
        image = b'\xff\xd8\xfftest-cover'
        fetch = Mock(return_value=image)
        self.app.config['FETCH_COVER'] = fetch
        self.assertTrue(fetch_one_cover(self.app))
        self.assertEqual(self.book()['cover_status'], 'available')
        response = self.client.get('/books/1/cover')
        self.assertEqual(response.data, image)
        self.assertEqual(response.mimetype, 'image/jpeg')
        self.assertEqual(self.client.get('/books/1/cover', headers={'If-None-Match': response.headers['ETag']}).status_code, 304)
        self.assertIn(b'Cover of Dune', self.client.get('/').data)
        self.assertIn(b'Cover of Dune', self.client.get('/books/1').data)
        self.post('/books/1/cover/retry')
        fetch_one_cover(self.app)
        fetch.assert_called_once_with('id:123')
        reopened = create_app({'TESTING': True, 'START_WORKER': False, 'DATABASE': self.app.config['DATABASE']})
        self.assertEqual(reopened.test_client().get('/books/1/cover').data, image)
        self.post('/books/1/edit', title='Another book')
        self.assertEqual(self.book()['cover_key'], '')
        self.assertEqual(self.client.get('/books/1/cover').status_code, 404)

    def test_cover_missing_and_failed_do_not_break_metadata(self):
        self.post('/books', title='Dune', isbn='9780441172719')
        self.app.config['LOOKUP'] = lambda book: {'match': {'title': 'Dune'}}
        enrich_one(self.app)
        fetch = Mock(return_value=None)
        self.app.config['FETCH_COVER'] = fetch
        fetch_one_cover(self.app)
        fetch.assert_called_once_with('isbn:9780441172719')
        self.assertEqual(self.book()['cover_status'], 'unavailable')
        self.assertEqual(self.book()['status'], 'matched')
        self.post('/books/1/cover/retry')
        fetch.side_effect = requests.Timeout()
        fetch_one_cover(self.app)
        self.assertEqual(self.book()['cover_status'], 'failed')
        self.assertEqual(self.book()['status'], 'matched')
        self.assertIn(b'Cover lookup failed', self.client.get('/books/1').data)
        self.assertFalse(fetch_one_cover(self.app))

    def test_edit_during_cover_fetch_cannot_attach_stale_cover(self):
        self.post('/books', title='Dune')
        self.app.config['LOOKUP'] = lambda book: {'match': {'cover_i': 123}}
        enrich_one(self.app)
        def change_during_fetch(identifier):
            self.post('/books/1/edit', title='Different book')
            return b'\xff\xd8\xffold-cover'
        self.app.config['FETCH_COVER'] = change_during_fetch
        fetch_one_cover(self.app)
        self.assertEqual(self.book()['cover_key'], '')
        self.assertEqual(self.book()['cover_status'], 'pending')

    def test_cover_picker_selects_caches_and_preserves_choice(self):
        self.post('/books', title='Dune', isbn='9780441172719')
        self.assertNotIn(b'class="cover-trigger"', self.client.get('/').data)
        self.assertIn(b'class="cover-link"', self.client.get('/').data)
        self.assertIn(b'class="cover-trigger"', self.client.get('/books/1').data)
        self.app.config['LOOKUP'] = lambda book: {'match': {'key': '/works/OL123W', 'cover_i': 5}}
        enrich_one(self.app)
        page = {'options': [{'cover_id': 123, 'edition_key': '/books/OL456M', 'title': 'Dune', 'publish_date': '1990', 'publishers': ['Publisher'], 'isbn': 'different'}], 'next_offset': 50}
        fetch_options = Mock(return_value=page)
        self.app.config['FETCH_COVER_OPTIONS'] = fetch_options
        self.app.config['FETCH_COVER'] = Mock(return_value=b'\xff\xd8\xffcover')
        choices = self.client.get('/books/1/cover-options').json
        self.assertEqual(choices['next_offset'], 50)
        self.assertTrue(choices['options'][0]['image_url'].startswith('/books/1/cover-options/123/image'))
        self.assertEqual(self.client.get(choices['options'][0]['image_url']).status_code, 200)
        response = self.client.post('/books/1/cover/select', data={'csrf': self.csrf, 'cover_id': 123, 'offset': 0})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.book()['cover_choice'], 'id:123')
        self.assertEqual(self.book()['cover_key'], 'id:123')
        self.assertEqual(self.book()['isbn'], '9780441172719')
        self.assertEqual(self.book()['reading_status'], 'To Be Read')
        self.app.config['FETCH_COVER'].assert_called_once_with('id:123')
        self.post('/books/1/retry')
        enrich_one(self.app)
        fetch_one_cover(self.app)
        self.assertEqual(self.book()['cover_key'], 'id:123')
        reopened = create_app({'TESTING': True, 'START_WORKER': False, 'DATABASE': self.app.config['DATABASE']})
        self.assertEqual(reopened.test_client().get('/books/1/cover-options').json['current'], 'id:123')
        fetch_options.assert_called_once_with('/works/OL123W', 0)
        self.post('/books/1/edit', title='Another book')
        self.assertEqual(self.book()['cover_choice'], '')

    def test_cover_picker_rejects_unlisted_cover_and_handles_failures(self):
        self.post('/books', title='Dune')
        self.app.config['LOOKUP'] = lambda book: {'match': {'key': '/works/OL123W'}}
        enrich_one(self.app)
        self.app.config['FETCH_COVER_OPTIONS'] = Mock(side_effect=requests.Timeout())
        self.assertEqual(self.client.get('/books/1/cover-options').status_code, 502)
        self.app.config['FETCH_COVER_OPTIONS'] = lambda key, offset: {'options': [{'cover_id': 123}], 'next_offset': None}
        self.client.get('/books/1/cover-options')
        self.assertEqual(self.client.get('/books/1/cover-options?offset=-1').status_code, 400)
        self.assertEqual(self.client.get('/books/1/cover-options/456/image').status_code, 404)
        self.assertEqual(self.client.post('/books/1/cover/select', data={'csrf': self.csrf, 'cover_id':456}).status_code, 404)
        self.assertEqual(self.client.post('/books/1/cover/select', data={'cover_id':123}).status_code, 400)
        self.app.config['FETCH_COVER'] = Mock(side_effect=requests.Timeout())
        self.assertEqual(self.client.post('/books/1/cover/select', data={'csrf':self.csrf, 'cover_id':123}).status_code, 502)
        self.assertEqual(self.book()['cover_key'], '')

    def test_cover_selection_cannot_overwrite_concurrent_edit(self):
        self.post('/books', title='Dune')
        self.app.config['LOOKUP'] = lambda book: {'match': {'key': '/works/OL123W'}}
        enrich_one(self.app)
        self.app.config['FETCH_COVER_OPTIONS'] = lambda key, offset: {'options': [{'cover_id': 123}], 'next_offset': None}
        self.client.get('/books/1/cover-options')
        def edit_during_download(identifier):
            self.post('/books/1/edit', title='Another book')
            return b'\xff\xd8\xffcover'
        self.app.config['FETCH_COVER'] = edit_during_download
        response = self.client.post('/books/1/cover/select', data={'csrf':self.csrf,'cover_id':123})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.book()['cover_choice'], '')

    @patch('app.requests.get')
    def test_cover_options_parse_deduplicate_and_paginate(self, get):
        get.return_value.json.return_value = {'size': 75, 'entries': [
            {'key': '/books/OL1M', 'title': 'Dune', 'covers': [-1, 123, 123], 'languages': [{'key': '/languages/eng'}], 'publishers': ['Publisher']},
            {'key': '/books/OL2M', 'title': 'Dune', 'covers': [123, 456], 'languages': [{'key': '/languages/eng'}], 'isbn_13': ['9780441172719']} ]}
        page = fetch_cover_options('/works/OL123W', 0)
        self.assertEqual([option['cover_id'] for option in page['options']], [123,456])
        self.assertEqual(page['next_offset'], 50)
        self.assertIsNone(fetch_cover_options('/works/OL123W', 50)['next_offset'])
        with self.assertRaises(ValueError):
            fetch_cover_options('//evil.example', 0)

    @patch('app.requests.get')
    def test_cover_picker_excludes_non_english_unknown_and_mixed_languages(self, get):
        get.return_value.json.return_value = {'size': 100, 'entries': [
            {'title':'French', 'covers':[123], 'languages':[{'key':'/languages/fre'}]},
            {'title':'English', 'covers':[123], 'languages':[{'key':'/languages/eng'}]},
            {'title':'Unknown', 'covers':[456]},
            {'title':'Mixed', 'covers':[789], 'languages':[{'key':'/languages/eng'},{'key':'/languages/fre'}]},
            {'title':'Invalid', 'covers':[999], 'languages':[None]} ]}
        page = fetch_cover_options('/works/OL123W', 0)
        self.assertEqual([option['title'] for option in page['options']], ['English'])
        self.assertEqual(page['language'], 'eng')
        self.assertEqual(page['next_offset'], 50)

    def test_cover_picker_replaces_old_unfiltered_cache(self):
        self.post('/books', title='Dune')
        self.app.config['LOOKUP'] = lambda book: {'match': {'key': '/works/OL123W'}}
        enrich_one(self.app)
        with connect(self.app) as db:
            db.execute('INSERT INTO cover_options (work_key,offset,payload) VALUES (?,?,?)',
                       ('/works/OL123W', 0, json.dumps({'options':[{'cover_id':456}], 'next_offset':None})))
        self.assertEqual(self.client.get('/books/1/cover-options/456/image').status_code, 404)
        self.app.config['FETCH_COVER_OPTIONS'] = Mock(return_value={'language':'eng', 'options':[{'cover_id':123}], 'next_offset':None})
        page = self.client.get('/books/1/cover-options').json
        self.assertEqual([option['cover_id'] for option in page['options']], [123])
        self.assertEqual(page['language'], 'eng')
        self.assertEqual(self.client.post('/books/1/cover/select', data={'csrf':self.csrf, 'cover_id':456}).status_code, 404)
        self.client.get('/books/1/cover-options')
        self.app.config['FETCH_COVER_OPTIONS'].assert_called_once()

    def test_description_saved_displayed_exported_and_reused_offline(self):
        self.post('/books', title='Dune')
        self.app.config['LOOKUP'] = lambda book: {'match': {'key': '/works/OL123W'}}
        enrich_one(self.app)
        description = 'A story of desert worlds.\n\n<script>unsafe</script>'
        result = {'description': description, 'response': {'description': description}}
        fetch = Mock(return_value=result)
        self.app.config['FETCH_DESCRIPTION'] = fetch
        self.assertTrue(fetch_one_description(self.app))
        self.assertEqual(self.book()['description'], description)
        self.assertEqual(self.book()['description_status'], 'available')
        self.assertIn(b'A story of desert worlds.', self.client.get('/').data)
        detail = self.client.get('/books/1').data
        self.assertRegex(detail, rb'About this [bB]ook')
        self.assertIn(b'&lt;script&gt;unsafe&lt;/script&gt;', detail)
        self.assertNotIn(b'<script>unsafe</script>', detail)
        exported = self.client.get('/export').json['titles'][0]
        self.assertEqual(exported['description'], description)
        self.assertEqual(exported['metadata']['work'], result)
        self.post('/books/1/description/retry')
        fetch_one_description(self.app)
        fetch.assert_called_once_with('/works/OL123W')
        reopened = create_app({'TESTING': True, 'START_WORKER': False, 'DATABASE': self.app.config['DATABASE']})
        self.assertIn(b'A story of desert worlds.', reopened.test_client().get('/books/1').data)
        self.post('/books/1/edit', title='Dune', notes='Keep the blurb')
        self.assertEqual(self.book()['description'], description)
        self.post('/books/1/edit', title='Another book')
        self.assertEqual(self.book()['description'], '')
        self.assertEqual(self.book()['description_status'], 'pending')

    def test_description_missing_failure_and_retry(self):
        self.post('/books', title='Dune')
        self.app.config['LOOKUP'] = lambda book: {'match': {'key': '/works/OL123W'}}
        enrich_one(self.app)
        fetch = Mock(return_value={'description': '', 'response': {}})
        self.app.config['FETCH_DESCRIPTION'] = fetch
        fetch_one_description(self.app)
        self.assertEqual(self.book()['description_status'], 'unavailable')
        self.assertIn(b'No description available.', self.client.get('/').data)
        self.post('/books/1/description/retry')
        fetch.side_effect = requests.Timeout()
        fetch_one_description(self.app)
        self.assertEqual(self.book()['description_status'], 'failed')
        self.assertEqual(self.book()['status'], 'matched')
        self.assertIn(b'Retry description lookup', self.client.get('/books/1').data)
        self.post('/books/1/description/retry')
        fetch.side_effect = None
        fetch.return_value = {'description': 'Recovered blurb', 'response': {}}
        fetch_one_description(self.app)
        self.assertEqual(self.book()['description'], 'Recovered blurb')
        # Even if a subsequent request fails, keep the saved blurb.
        with connect(self.app) as db:
            db.execute('DELETE FROM descriptions')
        self.post('/books/1/description/retry')
        fetch.side_effect = requests.Timeout()
        fetch_one_description(self.app)
        self.assertEqual(self.book()['description'], 'Recovered blurb')
        self.assertFalse(fetch_one_description(self.app))

    def test_edit_during_description_fetch_does_not_attach_old_blurb(self):
        self.post('/books', title='Dune')
        self.app.config['LOOKUP'] = lambda book: {'match': {'key': '/works/OL123W'}}
        enrich_one(self.app)
        def change_during_fetch(key):
            self.post('/books/1/edit', title='Different book')
            return {'description': 'Old book description'}
        self.app.config['FETCH_DESCRIPTION'] = change_during_fetch
        fetch_one_description(self.app)
        self.assertEqual(self.book()['description'], '')
        self.assertEqual(self.book()['description_status'], 'pending')
        self.assertEqual(self.book()['metadata_json'], '{}')

    @patch('app.requests.get')
    def test_description_response_formats_and_safe_work_keys(self, get):
        response = get.return_value
        response.status_code = 200
        for value in [' A plain description ', {'type': '/type/text', 'value': 'A plain description'}]:
            response.json.return_value = {'description': value}
            self.assertEqual(fetch_description('/works/OL123W')['description'], 'A plain description')
        response.json.return_value = {}
        self.assertEqual(fetch_description('/works/OL123W')['description'], '')
        response.status_code = 404
        self.assertIsNone(fetch_description('/works/OL123W'))
        before = get.call_count
        with self.assertRaises(ValueError):
            fetch_description('//evil.example/works/OL123W')
        self.assertEqual(before, get.call_count)

    @patch('app.requests.get')
    def test_cover_http_validation(self, get):
        response = get.return_value.__enter__.return_value
        response.status_code = 404
        self.assertIsNone(fetch_cover('id:123'))
        self.assertEqual(get.call_args.kwargs['params'], {'default': 'false'})
        response.status_code = 200
        response.headers = {'Content-Type': 'text/html'}
        with self.assertRaises(ValueError):
            fetch_cover('id:123')
        response.headers = {'Content-Type': 'image/jpeg'}
        response.iter_content.return_value = [b'bad-image']
        with self.assertRaises(ValueError):
            fetch_cover('id:123')
        response.iter_content.return_value = [b'\xff\xd8\xffimage']
        self.assertEqual(fetch_cover('id:123'), b'\xff\xd8\xffimage')
        with self.assertRaises(ValueError):
            fetch_cover('id:../../etc')

    def test_existing_database_upgrade_queues_covers(self):
        database = str(Path(self.temp.name) / 'old.db')
        with sqlite3.connect(database) as db:
            db.execute('''CREATE TABLE titles (id INTEGER PRIMARY KEY, kind TEXT DEFAULT 'book',
                title TEXT,author TEXT,isbn TEXT,notes TEXT,identity TEXT UNIQUE,metadata_json TEXT,
                status TEXT,error TEXT,added_at TEXT,revision INTEGER DEFAULT 0)''')
            db.execute("INSERT INTO titles (title,author,isbn,notes,identity,metadata_json,status,error,added_at) VALUES (?,?,?,?,?,?,?,?,?)",
                       ('Dune','Frank Herbert','','','dune', '{"match":{"title":"Dune"}}','matched','','2026-10-02'))
        upgraded = create_app({'TESTING': True, 'START_WORKER': False, 'DATABASE': database})
        with connect(upgraded) as db:
            book = db.execute('SELECT * FROM titles').fetchone()
            self.assertEqual(book['title'], 'Dune')
            self.assertEqual(book['status'], 'pending')
            self.assertEqual(book['cover_status'], 'pending')
            self.assertEqual(book['description_status'], 'pending')
            self.assertEqual(book['reading_status'], 'To Be Read')
        self.assertEqual(upgraded.test_client().get('/').status_code, 200)

    @patch('app.requests.get')
    def test_lookup_checks_exact_title_author_or_isbn(self, get):
        get.return_value.json.return_value = {'docs': [
            {'title': 'Dune', 'author_name': ['Wrong Author'], 'isbn': ['9780441172719']},
            {'title': 'Dune', 'author_name': ['Frank Herbert']} ]}
        match = lookup({'title': 'Dune', 'author': 'Frank Herbert', 'isbn': ''})
        self.assertEqual(match['match']['author_name'], ['Frank Herbert'])
        self.assertIsNone(lookup({'title': 'Dune Messiah', 'author': '', 'isbn': ''}))
        self.assertIsNotNone(lookup({'title': 'Different title', 'author': '', 'isbn': '9780441172719'}))
        self.assertIn('timeout', get.call_args.kwargs)
        get.return_value.json.return_value = {'invalid': 'response'}
        with self.assertRaises(ValueError):
            lookup({'title': 'Dune', 'author': '', 'isbn': ''})


if __name__ == '__main__':
    unittest.main()
