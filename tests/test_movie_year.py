"""Movie years persist, distinguish remakes, and constrain all metadata providers."""
import json
import os
import sqlite3
import unittest
from unittest.mock import Mock, patch

import test_app
from app import connect, create_app, lookup, parse_import, validate, normalize, now
from balloon import lookup_balloon
from media import lookup_movie_wikipedia, poster_url
from omdb import lookup_omdb


class MovieYearTests(unittest.TestCase):
    setUp = test_app.LibraryTests.setUp
    tearDown = test_app.LibraryTests.tearDown
    post = test_app.LibraryTests.post
    book = test_app.LibraryTests.book

    def response(self, value):
        return Mock(json=Mock(return_value=value))

    def test_add_edit_clear_preserve_and_export_year(self):
        self.post('/books', kind='movie', title='Arrival', year=' 2016 ')
        self.assertEqual(self.book()['year'], '2016')
        self.assertIn(b'name="year"', self.client.get('/?category=movie').data)
        self.assertIn(b'value="2016"', self.client.get('/books/1').data)
        self.assertNotIn(b'name="year"', self.client.get('/?category=book').data)
        self.assertNotIn(b'name="year"', self.client.get('/?category=tv').data)
        with connect(self.app) as db:
            db.execute("UPDATE titles SET status='matched',cover_choice='upload:example',cover_key='upload:example' WHERE id=1")
        self.post('/books/1/edit', title='Arrival', year='1996')
        self.assertEqual(self.book()['year'], '1996')
        self.assertEqual(self.book()['status'], 'pending')
        self.assertEqual(self.book()['cover_choice'], 'upload:example')
        self.post('/books/1/edit', title='Arrival', notes='Keep the year')
        self.assertEqual(self.book()['year'], '1996')
        self.assertEqual(self.client.get('/export').json['titles'][0]['year'], '1996')
        self.post('/books/1/edit', title='Arrival', year='')
        self.assertEqual(self.book()['year'], '')

    def test_validation_and_imports(self):
        for year in ('0', '999', '10000', '-2016', '2016.5', '2016-2017', 'year'):
            with self.subTest(year=year), self.assertRaises(ValueError):
                validate({'kind': 'movie', 'title': 'Arrival', 'year': year})
        self.assertEqual(parse_import('movies.csv', b'title,year\nArrival,2016', 'movie')[0]['year'], '2016')
        self.assertEqual(parse_import('movies.json', b'[{"kind":"movie","title":"Arrival","year":2016}]')[0]['year'], '2016')
        self.post('/books', kind='book', title='Book', year='invalid')
        self.assertEqual(self.book()['year'], '')

    def test_duplicate_detection_distinguishes_same_name_movies_by_year(self):
        for year in ('2016', '1996', '2016'):
            self.post('/books', kind='movie', title='Arrival', year=year)
        rows = self.client.get('/export').json['titles']
        self.assertEqual(len(rows), 2)
        self.assertEqual({row['year'] for row in rows}, {'2016', '1996'})

    def test_existing_database_migrates_to_empty_year_without_losing_titles(self):
        self.post('/books', kind='movie', title='Arrival')
        with connect(self.app) as db:
            db.execute('ALTER TABLE titles DROP COLUMN year')
        create_app({'TESTING': True, 'START_WORKER': False, 'DATABASE': self.app.config['DATABASE']})
        self.assertEqual((self.book()['title'], self.book()['year']), ('Arrival', ''))

    @patch('balloon.get_json')
    def test_primary_movie_year_disambiguates_same_title(self, get):
        first = {'id': 'tt1', 'title': 'Arrival', 'release_date': '2016-01-01', 'genres': []}
        second = {**first, 'id': 'tt2', 'release_date': '1996-01-01'}
        def serve(path, *args):
            if path.startswith('/search/'):
                return {'results': [first, second]}
            if path.endswith('/credits'):
                return {'crew': []}
            return second if path.endswith('tt2') else first
        get.side_effect = serve
        item = {'kind': 'movie', 'title': 'Arrival', 'author': '', 'year': '1996'}
        result = lookup_balloon(item, normalize, now)
        self.assertEqual(result['match']['key'], 'balloon-movie:tt2')

    @patch('media.lookup_balloon', return_value=None)
    @patch('media.lookup_omdb', return_value=None)
    @patch('media.requests.get')
    def test_itunes_uses_year_to_filter_same_title(self, get, omdb, primary):
        get.return_value = self.response({'results': [
            {'kind': 'feature-movie', 'trackId': identifier, 'trackName': 'Arrival', 'releaseDate': year + '-01-01'}
            for identifier, year in ((1, '2016'), (2, '1996'))]})
        result = lookup({'kind': 'movie', 'title': 'Arrival', 'author': '', 'year': '1996'})
        self.assertEqual(result['match']['key'], 'itunes:2')

    @patch('omdb.requests.get')
    def test_omdb_sends_year_and_checks_detail_year(self, get):
        detail = {'Response': 'True', 'Type': 'movie', 'Title': 'Arrival', 'imdbID': 'tt1', 'Year': '2016'}
        search = {'Response': 'True', 'totalResults': '1', 'Search': [detail]}
        for year in ('2016', '1996'):
            with self.subTest(year=year), patch.dict(os.environ, {'OMDB_API_KEY': 'test-key'}):
                get.side_effect = [self.response(search), self.response(detail)]
                result = lookup_omdb({'kind': 'movie', 'title': 'Arrival', 'author': '', 'year': year}, normalize, now, poster_url)
                self.assertEqual(get.call_args_list[-2].kwargs['params']['y'], year)
                if year == '2016':
                    self.assertEqual(result['match']['key'], 'omdb:tt1')
                else:
                    self.assertIsNone(result)

    @patch('media.requests.get')
    def test_wikipedia_uses_release_year_instead_of_year_in_title(self, get):
        get.return_value = self.response({'query': {'pages': {'1': {'pageid': 1,
            'title': 'Blade Runner 2049', 'extract': 'Blade Runner 2049 is a 2017 science fiction film directed by Denis Villeneuve.'}}}})
        for year in ('2017', '2049'):
            result = lookup_movie_wikipedia({'kind': 'movie', 'title': 'Blade Runner 2049', 'author': '', 'year': year}, normalize, now)
            if year == '2017':
                self.assertEqual(result['match']['first_publish_year'], '2017')
            else:
                self.assertIsNone(result)
