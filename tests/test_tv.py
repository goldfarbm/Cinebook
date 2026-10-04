# Hierarchy tests cover offline navigation, refresh preservation, parent remapping, and stale responses.
import io
import json
import unittest
from unittest.mock import Mock, patch

import requests
from app import connect, enrich_one, fetch_one_cover
from tv import fetch_tv_children, lookup_tv_child
import test_app


class TVHierarchyTests(unittest.TestCase):
    setUp = test_app.LibraryTests.setUp
    tearDown = test_app.LibraryTests.tearDown
    post = test_app.LibraryTests.post

    def seed(self):
        # Mock out-of-order seasons, repeated episode titles, and an unnumbered special to exercise hierarchy handling.
        self.post('/books', title='Example Show', kind='tv')
        with connect(self.app) as db:
            db.execute("UPDATE titles SET status='matched',metadata_json=? WHERE id=1",
                       (json.dumps({'match': {'key': 'tvmaze:123', 'poster_url': 'https://static.tvmaze.com/show.jpg'}}),))
        self.fetch = Mock(side_effect=lambda kind, key: [
            {'id': 21, 'number': 2, 'name': '', 'summary': '<p>Second season.</p>'},
            {'id': 20, 'number': 1, 'name': '', 'premiereDate': '2022-01-01'}
        ] if kind == 'tv' else [
            {'id': 31, 'number': 2, 'name': 'Same title', 'airdate': '2022-01-08'},
            {'id': 30, 'number': 1, 'name': 'Same title', 'summary': '<p>Pilot summary.</p>'},
            {'id': 32, 'number': None, 'name': 'Special'}])
        self.app.config['FETCH_TV_CHILDREN'] = self.fetch

    def row(self, key):
        with connect(self.app) as db:
            return dict(db.execute('SELECT * FROM titles WHERE source_key=?', (key,)).fetchone())

    def test_navigation_order_shared_controls_and_offline_cache(self):
        self.seed()
        page = self.client.get('/tv/1/seasons')
        self.assertEqual(page.status_code, 200)
        self.assertLess(page.data.index(b'>Season 1</a>'), page.data.index(b'>Season 2</a>'))
        season = self.row('tvmaze-season:20')
        page = self.client.get(f'/tv/seasons/{season["id"]}/episodes')
        self.assertEqual(page.status_code, 200)
        self.assertRegex(page.data, rb'Add an [eE]pisode')
        for content in [b'List', b'Grid', b'name="parent_id"', b'Watched', b'Special', b'Pilot summary.']:
            self.assertIn(content, page.data)
        self.assertEqual(self.fetch.call_count, 2)
        self.fetch.side_effect = requests.ConnectionError()
        self.assertEqual(self.client.get('/tv/1/seasons').status_code, 200)
        self.assertEqual(self.client.get(f'/tv/seasons/{season["id"]}/episodes').status_code, 200)
        self.assertEqual(self.fetch.call_count, 2)
        self.assertNotIn(b'>Season 1</a>', self.client.get('/?category=tv').data)
        self.assertEqual(self.client.get('/tv/1/seasons?q=absent').status_code, 200)
        self.assertEqual(self.client.get('/tv/seasons/1/episodes').status_code, 404)

    def test_refresh_preserves_edits_and_cascade_delete(self):
        self.seed()
        self.client.get('/tv/1/seasons')
        season = self.row('tvmaze-season:20')
        self.client.get(f'/tv/seasons/{season["id"]}/episodes')
        episode = self.row('tvmaze-episode:30')
        self.post(f'/books/{episode["id"]}/edit', title='My pilot', notes='Favorite', reading_status='Read')
        self.post(f'/tv/{season["id"]}/refresh-children')
        saved = self.row('tvmaze-episode:30')
        self.assertEqual((saved['title'], saved['notes'], saved['reading_status']), ('My pilot', 'Favorite', 'Read'))
        self.assertEqual(len(self.client.get('/export').json['titles']), 6)
        self.fetch.side_effect = requests.ConnectionError()
        self.assertIn(b'Saved titles are still available', self.post('/tv/1/refresh-children').data)
        self.post('/books/1/delete')
        self.assertEqual(self.client.get('/export').json['titles'], [])
        with connect(self.app) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM tv_collections').fetchone()[0], 0)

    def test_scoped_adding_number_deduplication_and_export_remapping(self):
        self.seed()
        self.client.get('/tv/1/seasons')
        season = self.row('tvmaze-season:20')
        for number in (1, 2):
            self.post('/books', title='Same title', kind='tv_episode', parent_id=season['id'], position=number)
        self.client.get(f'/tv/seasons/{season["id"]}/episodes')
        self.assertEqual(len(self.client.get('/export').json['titles']), 6)
        self.post('/import', kind='tv_episode', parent_id=season['id'], file=(io.BytesIO(b'title,position\nNew episode,3'), 'episodes.csv'))
        exported = self.client.get('/export').data
        self.post('/books/1/delete')
        self.post('/books', title='Unrelated book')
        response = self.post('/import', file=(io.BytesIO(exported), 'export.json'))
        self.assertIn(b'Imported 7 titles', response.data)
        with connect(self.app) as db:
            rows = db.execute('SELECT * FROM titles').fetchall()
            by_id = {row['id']: row for row in rows}
            for row in rows:
                if row['parent_id']:
                    self.assertEqual(by_id[row['parent_id']]['kind'], 'tv' if row['kind'] == 'tv_season' else 'tv_season')

    def test_waits_for_parent_and_ignores_stale_fetch(self):
        self.post('/books', title='Pending', kind='tv')
        self.post('/books', title='Season 1', kind='tv_season', parent_id=1, position=1)
        with connect(self.app) as db:
            db.execute("UPDATE titles SET status='ambiguous' WHERE id=1")
        self.assertFalse(enrich_one(self.app))
        with connect(self.app) as db:
            db.execute("UPDATE titles SET status='matched',metadata_json=? WHERE id=1", ('{"match":{"key":"tvmaze:123"}}',))
        def changed_parent(kind, key):
            with connect(self.app) as db:
                db.execute('UPDATE titles SET revision=revision+1 WHERE id=1')
            return [{'id': 20, 'number': 1}]
        self.app.config['FETCH_TV_CHILDREN'] = changed_parent
        self.assertIn(b'parent title changed', self.client.get('/tv/1/seasons').data)
        with connect(self.app) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM tv_collections').fetchone()[0], 0)

    def test_child_artwork_status_and_selected_cover_survive_refresh(self):
        self.seed()
        self.client.get('/tv/1/seasons')
        season = self.row('tvmaze-season:20')
        with connect(self.app) as db:
            db.execute("UPDATE titles SET cover_status='unavailable' WHERE id=1")
        self.app.config['FETCH_COVER'] = Mock(return_value=b'\xff\xd8\xffimage')
        while fetch_one_cover(self.app):
            pass
        options = self.client.get(f'/books/{season["id"]}/cover-options').json
        self.assertTrue(options['options'])
        choice = options['options'][0]['cover_id']
        self.assertEqual(self.client.post(f'/books/{season["id"]}/cover/select', data={'csrf': self.csrf, 'cover_id': choice}).status_code, 200)
        self.post(f'/books/{season["id"]}/reading-status', reading_status='Read', location='library')
        before = self.row('tvmaze-season:20')
        self.post('/tv/1/refresh-children')
        after = self.row('tvmaze-season:20')
        self.assertEqual(after['cover_choice'], before['cover_choice'])
        self.assertEqual(after['reading_status'], 'Read')
        self.assertEqual(self.client.get(f'/books/{season["id"]}/cover').data, b'\xff\xd8\xffimage')

    @patch('tv.requests.get')
    def test_provider_paths_and_known_episode_lookup(self, get):
        response = Mock()
        get.return_value = response
        response.json.return_value = [{'id': 20, 'number': 1}]
        fetch_tv_children('tv', 'tvmaze:123')
        self.assertEqual(get.call_args.args[0], 'https://api.tvmaze.com/shows/123/seasons')
        fetch_tv_children('tv_season', 'tvmaze-season:20')
        self.assertEqual(get.call_args.args[0], 'https://api.tvmaze.com/seasons/20/episodes')
        response.json.return_value = {'id': 30, 'name': 'Pilot', 'number': 1}
        metadata = lookup_tv_child({'kind': 'tv_episode', 'source_key': 'tvmaze-episode:30'},
                                  {'metadata_json': '{}', 'cover_choice': '', 'cover_key': ''}, str.casefold, lambda: 'now')
        self.assertEqual(metadata['match']['title'], 'Pilot')
        self.assertEqual(get.call_args.args[0], 'https://api.tvmaze.com/episodes/30')
