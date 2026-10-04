"""Search suggestions stay within the current collection and use enabled tags."""
import unittest

import test_app
from app import connect


class SearchSuggestionTests(unittest.TestCase):
    setUp = test_app.LibraryTests.setUp
    tearDown = test_app.LibraryTests.tearDown
    post = test_app.LibraryTests.post

    def suggestions(self, **query):
        response = self.client.get('/search-suggestions', query_string=query)
        self.assertEqual(response.status_code, 200)
        return response.json['suggestions']

    def test_titles_and_assigned_tags_are_scoped_deduplicated_and_case_insensitive(self):
        for kind, title in [('book', 'Science Book'), ('book', 'Second Book'), ('movie', 'Science Movie')]:
            self.post('/books', kind=kind, title=title)
        self.post('/books/1/tags', new_tag='Science Fiction')
        with connect(self.app) as db:
            tag_id = db.execute("SELECT id FROM tags WHERE name='Science Fiction'").fetchone()[0]
        self.post('/books/2/tags', tag_id=str(tag_id))
        self.post('/tags', name='Science Unused')
        suggestions = self.suggestions(category='book', q='sCiEnCe')
        self.assertEqual(suggestions, [{'value': 'Science Book', 'label': 'Title'},
                                       {'value': 'Science Fiction', 'label': 'Tag'}])
        self.assertEqual(self.suggestions(category='movie', q='science'),
                         [{'value': 'Science Movie', 'label': 'Title'}])
        self.post('/books/1/tags')
        self.post('/books/2/tags')
        self.assertEqual(self.suggestions(category='book', q='science'),
                         [{'value': 'Science Book', 'label': 'Title'}])
        self.assertEqual(self.suggestions(category='book', q=''), [])

    def test_alternate_titles_and_safe_special_characters(self):
        self.post('/books', kind='movie', title='Example', alternate_title='A <Special> Title')
        self.assertEqual(self.suggestions(category='movie', q='special'),
                         [{'value': 'A <Special> Title', 'label': 'Title'}])
        page = self.client.get('/?category=movie').data
        self.assertIn(b'list="search-suggestions"', page)
        self.assertNotIn(b'id="tag-filter"', page)

    def test_children_stay_within_parent_and_current_provider_match(self):
        for show in ('One', 'Two'):
            self.post('/books', kind='tv', title=show)
        for parent_id, title in [(1, 'Season One'), (2, 'Season Other')]:
            self.post('/books', kind='tv_season', parent_id=str(parent_id), title=title)
        self.post('/books', kind='tv_season', parent_id='1', title='Season Old Provider')
        with connect(self.app) as db:
            db.execute("UPDATE titles SET parent_source_key='tvmaze:old' WHERE title='Season Old Provider'")
        self.assertEqual(self.suggestions(category='tv_season', parent_id=1, q='season'),
                         [{'value': 'Season One', 'label': 'Title'}])
        self.assertEqual(self.client.get('/search-suggestions?category=invalid&q=x').status_code, 400)
        self.assertEqual(self.client.get('/search-suggestions?category=tv_episode&q=x').status_code, 400)
        self.assertEqual(self.client.get('/search-suggestions?category=tv_episode&parent_id=1&q=x').status_code, 400)

    def test_suggestions_are_bounded_and_prefix_matches_rank_first(self):
        for title in ['A matching term'] + [f'Term {number:02}' for number in range(12)]:
            self.post('/books', title=title)
        suggestions = self.suggestions(category='book', q='term')
        self.assertEqual(len(suggestions), 10)
        self.assertEqual(suggestions[0]['value'], 'Term 00')
