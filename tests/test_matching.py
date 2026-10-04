# Match-selection tests verify ambiguity, saved choices, and rejection of stale candidate data.
import json
import unittest
from unittest.mock import Mock, patch

from app import create_app, connect, enrich_one, fetch_one_cover, fetch_one_description, lookup
from matching import resolve_candidates
import test_app


class MatchTests(unittest.TestCase):
    setUp = test_app.LibraryTests.setUp
    tearDown = test_app.LibraryTests.tearDown
    post = test_app.LibraryTests.post

    def candidates(self, provider='Open Library'):
        # Two same-name works with different authors and years exercise the match-selection modal.
        return [{'provider': provider, 'match': {'key': f'/works/OL{i}W', 'title': 'Shared name',
                   'author_name': [author], 'first_publish_year': year}, 'response': {'original': True}}
                for i, author, year in [(1, 'Alice Author', 1990), (2, 'Bob Writer', 2020)]]

    def row(self, item_id=1):
        with connect(self.app) as db:
            return dict(db.execute('SELECT * FROM titles WHERE id=?', (item_id,)).fetchone())

    def make_ambiguous(self, kind='book'):
        # Run the enrichment step explicitly to create deterministic ambiguity without network requests.
        self.post('/books', kind=kind, title='Shared name')
        self.app.config['LOOKUP'] = lambda item: resolve_candidates(self.candidates(), item)
        enrich_one(self.app)
        return self.row()

    @patch('app.requests.get')
    def test_books_collect_all_matches_and_deduplicate_provider_keys(self, get):
        candidates = self.candidates()
        get.return_value.json.return_value = {'docs': [c['match'] for c in candidates] + [candidates[0]['match']]}
        result = lookup({'title': 'Shared name', 'author': '', 'isbn': ''})
        self.assertEqual(len(result['candidates']), 2)
        self.assertNotIn('match', result)
        selected = lookup({'title': 'Shared name', 'author': '', 'isbn': '', 'match_choice': '/works/OL2W'})
        self.assertEqual(selected['match']['author_name'], ['Bob Writer'])
        specific = lookup({'title': 'Shared name', 'author': 'Alice Author', 'isbn': ''})
        self.assertEqual(specific['match']['key'], '/works/OL1W')

    @patch('media.requests.get')
    @patch('media.lookup_balloon', new=lambda *args: None)
    def test_movies_and_tv_collect_all_same_name_matches(self, get):
        get.return_value.json.return_value = {'results': [
            {'kind': 'feature-movie', 'trackId': 1, 'trackName': 'Shared name', 'artistName': 'One', 'releaseDate': '1990'},
            {'kind': 'feature-movie', 'trackId': 2, 'trackName': 'Shared name', 'artistName': 'Two', 'releaseDate': '2020'}]}
        result = lookup({'kind': 'movie', 'title': 'Shared name', 'author': '', 'isbn': ''})
        self.assertEqual(len(result['candidates']), 2)
        selected = lookup({'kind': 'movie', 'title': 'Shared name', 'author': '', 'isbn': '', 'match_choice': 'itunes:2'})
        self.assertEqual(selected['match']['first_publish_year'], '2020')
        get.return_value.json.return_value = [{'show': {'id': i, 'name': 'Shared name', 'premiered': year}}
                                              for i, year in [(1, '1990'), (2, '2020')]]
        result = lookup({'kind': 'tv', 'title': 'Shared name', 'author': '', 'isbn': ''})
        self.assertEqual(len(result['candidates']), 2)
        selected = lookup({'kind': 'tv', 'title': 'Shared name', 'author': '', 'isbn': '', 'match_choice': 'tvmaze:2'})
        self.assertEqual(selected['match']['first_publish_year'], '2020')

    def test_choice_survives_restart_and_notes_but_retry_reopens_selection(self):
        row = self.make_ambiguous()
        self.assertEqual(row['status'], 'ambiguous')
        self.assertEqual(row['author'], '')
        self.assertFalse(fetch_one_cover(self.app))
        self.assertFalse(fetch_one_description(self.app))
        page = self.client.get('/').data
        self.assertIn(b'data-match-choice="1"', page)
        choices = self.client.get('/books/1/match-options').json
        self.assertEqual([c['year'] for c in choices['candidates']], [1990, 2020])
        self.assertEqual(choices['candidates'][1]['authors'], ['Bob Writer'])
        response = self.client.post('/books/1/match/select', data={'csrf': self.csrf, 'choice': 1, 'revision': choices['revision']})
        self.assertEqual(response.status_code, 200)
        row = self.row()
        self.assertEqual(row['status'], 'matched')
        self.assertEqual(row['author'], 'Bob Writer')
        self.assertEqual(row['match_choice'], '/works/OL2W')
        self.assertEqual(json.loads(row['metadata_json'])['response'], {'original': True})
        reopened = create_app({'TESTING': True, 'START_WORKER': False, 'DATABASE': self.app.config['DATABASE'],
                               'LOOKUP': lambda item: resolve_candidates(self.candidates(), item)})
        self.assertEqual(reopened.test_client().get('/books/1').status_code, 200)
        self.post('/books/1/edit', title='Shared name', author='Bob Writer', notes='My notes')
        self.assertEqual(self.row()['match_choice'], '/works/OL2W')
        page = self.post('/books/1/retry').data
        self.assertIn(b'data-pending="1"', page)
        self.assertEqual(self.row()['match_choice'], '')
        enrich_one(reopened)
        self.assertEqual(self.row()['status'], 'ambiguous')
        self.assertEqual(self.client.get('/status').json['books'][0]['status'], 'ambiguous')
        self.assertIn(b'data-match-choice="1"', self.client.get('/books/1').data)
        choices = self.client.get('/books/1/match-options').json
        self.assertEqual(len(choices['candidates']), 2)
        self.assertEqual(self.client.post('/books/1/match/select', data={
            'csrf': self.csrf, 'choice': 0, 'revision': choices['revision']}).status_code, 200)
        self.assertEqual(self.row()['match_choice'], '/works/OL1W')
        self.post('/books/1/edit', title='Another title')
        self.assertEqual(self.row()['match_choice'], '')

    @patch('app.requests.get')
    def test_retry_checks_provider_for_same_author_matches(self, get):
        candidates = self.candidates()
        for candidate in candidates:
            candidate['match']['author_name'] = ['Alice Author']
        get.return_value.json.return_value = {'docs': [candidate['match'] for candidate in candidates]}
        self.post('/books', title='Shared name', author='Alice Author')
        with connect(self.app) as db:
            db.execute("UPDATE titles SET status='matched',match_choice=?,metadata_json=? WHERE id=1",
                       ('/works/OL2W', json.dumps(candidates[1])))
        self.post('/books/1/retry')
        enrich_one(self.app)
        self.assertEqual(self.row()['status'], 'ambiguous')
        self.assertEqual(len(self.client.get('/books/1/match-options').json['candidates']), 2)
        self.assertIn(b'data-match-choice="1"', self.client.get('/books/1').data)

    def test_retry_with_one_match_does_not_require_chooser(self):
        self.make_ambiguous()
        self.app.config['LOOKUP'] = lambda item: resolve_candidates(self.candidates()[:1], item)
        self.post('/books/1/retry')
        enrich_one(self.app)
        self.assertEqual(self.row()['status'], 'matched')
        self.assertEqual(self.client.get('/books/1/match-options').status_code, 409)
        self.assertNotIn(b'data-match-choice="1"', self.client.get('/books/1').data)

    def test_selection_rejects_stale_invalid_and_missing_csrf(self):
        row = self.make_ambiguous()
        base = {'csrf': self.csrf, 'revision': row['revision']}
        self.assertEqual(self.client.post('/books/1/match/select', data={**base, 'choice': -1}).status_code, 400)
        self.assertEqual(self.client.post('/books/1/match/select', data={**base, 'choice': 5}).status_code, 400)
        self.assertEqual(self.client.post('/books/1/match/select', data={'choice': 0, 'revision': row['revision']}).status_code, 400)
        self.post('/books/1/edit', title='Edited')
        self.assertEqual(self.client.post('/books/1/match/select', data={**base, 'choice': 0}).status_code, 409)
        self.assertEqual(self.row()['title'], 'Edited')
        self.assertEqual(self.row()['status'], 'pending')

    def test_edit_during_ambiguous_lookup_discards_outdated_options(self):
        self.post('/books', title='Shared name')
        def lookup_and_edit(item):
            self.post('/books/1/edit', title='New title')
            return resolve_candidates(self.candidates(), item)
        self.app.config['LOOKUP'] = lookup_and_edit
        enrich_one(self.app)
        self.assertEqual(self.row()['title'], 'New title')
        self.assertEqual(self.row()['status'], 'pending')
        self.assertEqual(self.row()['metadata_json'], '{}')

    @patch('media.requests.get')
    @patch('media.lookup_balloon', new=lambda *args: None)
    def test_wikipedia_remakes_are_ambiguous(self, get):
        apple = Mock()
        apple.json.return_value = {'results': []}
        wiki = Mock()
        wiki.json.return_value = {'query': {'pages': {
            str(i): {'pageid': i, 'title': f'Shared name ({year} film)', 'index': i,
                     'extract': f'Shared name is a {year} film directed by {author}.'}
            for i, year, author in [(1, 1990, 'Alice Author'), (2, 2020, 'Bob Writer')]}}}
        get.side_effect = [apple, wiki]
        result = lookup({'kind': 'movie', 'title': 'Shared name', 'author': '', 'isbn': ''})
        self.assertEqual(len(result['candidates']), 2)
        self.assertEqual([candidate['match']['first_publish_year'] for candidate in result['candidates']], ['1990', '2020'])

    def test_candidate_images_reuse_original_cache_and_reject_stale_revisions(self):
        self.post('/books', title='Shared name')
        candidates = self.candidates()
        candidates[0]['match']['cover_i'] = 123
        self.app.config['LOOKUP'] = lambda item: resolve_candidates(candidates, item)
        enrich_one(self.app)
        row = self.row()
        image = b'\xff\xd8\xfforiginal'
        self.app.config['FETCH_COVER'] = Mock(return_value=image)
        option = self.client.get('/books/1/match-options').json['candidates'][0]
        self.assertEqual(self.client.get(option['image_url']).data, image)
        self.assertEqual(self.client.get(option['image_url']).data, image)
        self.app.config['FETCH_COVER'].assert_called_once_with('id:123')
        self.post('/books/1/edit', title='Edited')
        self.assertEqual(self.client.get(option['image_url']).status_code, 409)
