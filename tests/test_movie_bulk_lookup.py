"""Bulk metadata retries select movies lacking actual cached cover images."""
import json
import unittest
from unittest.mock import Mock

import test_app
from app import connect, enrich_one, fetch_one_cover


class MovieBulkLookupTests(unittest.TestCase):
    setUp = test_app.LibraryTests.setUp
    tearDown = test_app.LibraryTests.tearDown
    post = test_app.LibraryTests.post
    endpoint = '/settings/movies/lookup-missing-covers'

    def add(self, title, kind='movie', status='unmatched', cover_key='', image=None):
        self.post('/books', kind=kind, title=title, notes='Keep my notes', reading_status='Read')
        with connect(self.app) as db:
            identifier = db.execute('SELECT id FROM titles WHERE title=?', (title,)).fetchone()[0]
            db.execute("UPDATE titles SET status=?,cover_key=?,cover_status='unavailable',error='old error',"
                       "cover_error='old cover error',description_error='old description error',"
                       "match_choice='itunes:1' WHERE id=?", (status, cover_key, identifier))
            if image is not None:
                db.execute("INSERT INTO covers (identifier,image,fetched_at) VALUES (?,?,'now')", (cover_key, image))
        return identifier

    def rows(self):
        with connect(self.app) as db:
            return {row['id']: dict(row) for row in db.execute('SELECT * FROM titles')}

    def test_selects_missing_stale_and_empty_images_but_preserves_other_titles(self):
        missing = self.add('No cover')
        stale = self.add('Missing cached image', status='matched', cover_key='poster:missing')
        empty = self.add('Empty cached image', status='failed', cover_key='poster:empty', image=b'')
        covered = self.add('Covered', status='failed', cover_key='poster:cached', image=b'\xff\xd8\xffimage')
        book = self.add('Book', kind='book')
        show = self.add('Show', kind='tv')
        pending = self.add('Already pending', status='pending')
        before = self.rows()
        self.app.config['LOOKUP'] = Mock()
        response = self.post(self.endpoint)
        self.assertIn(b'Metadata lookup queued for 3 movies', response.data)
        after = self.rows()
        for identifier in (missing, stale, empty):
            self.assertEqual(after[identifier]['status'], 'pending')
            self.assertEqual(after[identifier]['match_choice'], '')
            self.assertEqual(after[identifier]['cover_status'], 'pending')
            self.assertEqual(after[identifier]['description_status'], 'pending')
            self.assertEqual(after[identifier]['revision'], before[identifier]['revision'] + 1)
            for field in ('error', 'cover_error', 'description_error'):
                self.assertEqual(after[identifier][field], '')
            for field in ('title', 'author', 'notes', 'reading_status', 'metadata_json', 'cover_choice'):
                self.assertEqual(after[identifier][field], before[identifier][field])
        for identifier in (covered, book, show, pending):
            self.assertEqual(after[identifier], before[identifier])
        self.app.config['LOOKUP'].assert_not_called()
        response = self.post(self.endpoint)
        self.assertIn(b'already queued', response.data)
        self.assertEqual(self.rows(), after)

    def test_worker_fetches_metadata_then_cover_and_keeps_explicit_cover_choice(self):
        identifier = self.add('Arrival', status='failed')
        poster = 'https://m.media-amazon.com/images/M/arrival.jpg'
        chosen = 'poster:https://m.media-amazon.com/images/M/selected.jpg'
        with connect(self.app) as db:
            db.execute('UPDATE titles SET cover_choice=? WHERE id=?', (chosen, identifier))
        self.app.config['LOOKUP'] = Mock(return_value={'provider': 'OMDb', 'match': {
            'key': 'omdb:tt2543164', 'title': 'Arrival', 'poster_url': poster}})
        self.app.config['FETCH_COVER'] = Mock(return_value=b'\xff\xd8\xffimage')
        self.post(self.endpoint)
        enrich_one(self.app)
        fetch_one_cover(self.app)
        row = self.rows()[identifier]
        self.assertEqual(row['status'], 'matched')
        self.assertEqual(row['cover_status'], 'available')
        self.assertEqual(row['cover_key'], chosen)
        self.app.config['FETCH_COVER'].assert_called_once_with(chosen)
        self.assertEqual(json.loads(row['metadata_json'])['provider'], 'OMDb')

    def test_settings_count_and_empty_selection(self):
        self.assertIn(b'0 movies have no saved cover image', self.client.get('/settings').data)
        self.assertIn(b'Every movie already has a cover image', self.post(self.endpoint).data)
        self.add('Missing')
        self.assertIn(b'1 movie has no saved cover image', self.client.get('/settings').data)
        self.assertIn(b'Look up movies without covers', self.client.get('/settings').data)

    def test_post_requires_csrf_and_get_does_not_queue(self):
        self.add('Missing')
        before = self.rows()
        self.assertEqual(self.client.post(self.endpoint).status_code, 400)
        self.assertEqual(self.client.get(self.endpoint).status_code, 405)
        self.assertEqual(self.rows(), before)
