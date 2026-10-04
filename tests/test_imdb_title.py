"""Optional IMDb links verify movie titles without importing other record fields."""
import unittest
from unittest.mock import patch

import requests
import test_app
from app import connect


class IMDbTitleTests(unittest.TestCase):
    setUp = test_app.LibraryTests.setUp
    tearDown = test_app.LibraryTests.tearDown
    post = test_app.LibraryTests.post

    def titles(self):
        with connect(self.app) as db:
            return [dict(row) for row in db.execute('SELECT * FROM titles')]

    @patch('balloon.get_json')
    def test_confirms_title_without_importing_metadata_or_replacing_user_fields(self, fetch):
        fetch.return_value = {'id': 'tt2543164', 'title': 'Arrival', 'original_title': 'Arrival',
                              'overview': 'Do not import this.', 'genres': [{'name': 'Drama'}],
                              'poster_path': 'https://m.media-amazon.com/poster.jpg'}
        response = self.post('/books', kind='movie', title='ARRIVAL', author='My director',
                             alternate_title='My alternate', imdb_link='https://www.imdb.com/title/tt2543164/?ref_=test')
        self.assertIn(b'IMDb title confirmed.', response.data)
        fetch.assert_called_once_with('/movie/tt2543164')
        movie = self.titles()[0]
        self.assertEqual(movie['title'], 'ARRIVAL')
        self.assertEqual(movie['author'], 'My director')
        self.assertEqual(movie['alternate_title'], 'My alternate')
        self.assertEqual(movie['metadata_json'], '{}')
        self.assertEqual(movie['description'], '')
        self.assertEqual(movie['status'], 'pending')
        self.assertEqual(movie['source_key'], '')

    @patch('balloon.get_json')
    def test_accepts_original_title_and_rejects_a_different_title(self, fetch):
        fetch.return_value = {'id': 'tt1', 'title': 'It Boy', 'original_title': "20 ans d'écart"}
        response = self.post('/books', kind='movie', title='Wrong movie', imdb_link='https://imdb.com/title/tt1/')
        self.assertIn(b'The IMDb link is for', response.data)
        self.assertEqual(self.titles(), [])
        self.post('/books', kind='movie', title="20 ans d'écart", imdb_link='https://imdb.com/title/tt1/')
        self.assertEqual(self.titles()[0]['title'], "20 ans d'écart")

    @patch('balloon.get_json')
    def test_invalid_links_are_rejected_without_network_requests(self, fetch):
        for link in ['https://example.com/title/tt1/', 'https://imdb.com.evil.example/title/tt1/',
                     'https://user:password@imdb.com/title/tt1/', 'https://imdb.com/name/nm1/',
                     'https://imdb.com/title/not-an-id/', 'https://imdb.com:invalid/title/tt1/']:
            with self.subTest(link=link):
                response = self.post('/books', kind='movie', title='Example', imdb_link=link)
                self.assertIn(b'Enter an IMDb title link', response.data)
        fetch.assert_not_called()
        self.assertEqual(self.titles(), [])

    @patch('balloon.get_json')
    def test_api_failure_cannot_confirm_title_and_optional_link_can_be_omitted(self, fetch):
        for failure in [requests.ConnectionError(), ValueError('upstream 403')]:
            fetch.side_effect = failure
            response = self.post('/books', kind='movie', title='Example', imdb_link='https://imdb.com/title/tt1/')
            self.assertIn(b'Could not confirm the title from IMDb.', response.data)
            self.assertEqual(self.titles(), [])
        fetch.reset_mock()
        self.post('/books', kind='movie', title='Example')
        fetch.assert_not_called()
        self.assertEqual(len(self.titles()), 1)

    def test_field_is_movie_only_and_precedes_eidr(self):
        page = self.client.get('/?category=movie').data
        self.assertLess(page.index(b'id="new-imdb-link"'), page.index(b'id="new-eidr"'))
        for category in ('book', 'tv'):
            self.assertNotIn(b'id="new-imdb-link"', self.client.get('/?category=' + category).data)
