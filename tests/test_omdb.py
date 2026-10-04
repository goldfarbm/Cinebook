"""OMDb fallback ordering, exact matching, saved choices, and offline metadata."""
import json
import os
import unittest
from unittest.mock import Mock, patch

import requests
import test_app
from app import connect, enrich_one, fetch_one_description, lookup
from media import fetch_media_description, poster_url
from omdb import BASE_URL, lookup_omdb


class OmdbTests(unittest.TestCase):
    tearDown = test_app.LibraryTests.tearDown
    post = test_app.LibraryTests.post

    def setUp(self):
        test_app.LibraryTests.setUp(self)
        env = patch.dict(os.environ, {'OMDB_API_KEY': 'test-private-key'})
        env.start()
        self.addCleanup(env.stop)

    def response(self, payload):
        return Mock(json=Mock(return_value=payload))

    def item(self, **changes):
        return {'kind': 'movie', 'title': 'Arrival', 'author': 'Denis Villeneuve', 'isbn': '', **changes}

    def movie(self, **changes):
        return {'Response': 'True', 'Type': 'movie', 'imdbID': 'tt2543164', 'Title': 'Arrival',
                'Director': 'Denis Villeneuve', 'Year': '2016', 'Genre': 'Drama, Sci-Fi',
                'Plot': 'A linguist meets aliens.', 'Runtime': '116 min',
                'Poster': 'https://m.media-amazon.com/images/M/arrival.jpg', **changes}

    def search(self, *movies):
        return {'Response': 'True', 'totalResults': str(len(movies)), 'Search': [
            {field: movie[field] for field in ('Type', 'imdbID', 'Title')} for movie in movies]}

    @patch('omdb.requests.get')
    def test_primary_then_omdb_short_circuits_itunes_and_wikipedia(self, get):
        movie = self.movie()
        get.side_effect = [requests.ConnectionError(), self.response(self.search(movie)), self.response(movie)]
        result = lookup(self.item())
        self.assertEqual(result['provider'], 'OMDb')
        self.assertEqual(result['match']['key'], 'omdb:tt2543164')
        self.assertEqual(result['match']['author_name'], ['Denis Villeneuve'])
        self.assertEqual(result['match']['subject'], ['Drama', 'Sci-Fi'])
        self.assertEqual(result['match']['runtime'], 116)
        self.assertEqual(get.call_count, 3)
        self.assertIn('/search/movie', get.call_args_list[0].args[0])
        self.assertEqual(get.call_args_list[1].args[0], BASE_URL)
        self.assertEqual(get.call_args.kwargs['params']['plot'], 'full')
        self.assertEqual(get.call_args.kwargs['timeout'], (4, 8))
        self.assertNotIn('test-private-key', json.dumps(result))

    @patch('media.lookup_balloon')
    @patch('omdb.requests.get')
    def test_primary_success_does_not_query_omdb(self, get, primary):
        primary.return_value = {'provider': 'Balloonerismm (IMDb)', 'match': {'key': 'balloon-movie:tt1'}}
        self.assertEqual(lookup(self.item()), primary.return_value)
        get.assert_not_called()

    @patch('media.lookup_balloon', return_value=None)
    @patch('omdb.requests.get')
    def test_missing_key_skips_to_itunes(self, get, primary):
        with patch.dict(os.environ, {'OMDB_API_KEY': ''}):
            get.return_value = self.response({'results': [
                {'kind': 'feature-movie', 'trackId': 1, 'trackName': 'Arrival', 'artistName': 'Denis Villeneuve'}]})
            self.assertEqual(lookup(self.item())['provider'], 'Apple iTunes')
        get.assert_called_once()
        self.assertIn('itunes.apple.com', get.call_args.args[0])

    @patch('media.lookup_balloon', return_value=None)
    @patch('omdb.requests.get')
    def test_errors_and_misses_continue_to_itunes(self, get, primary):
        apple = self.response({'results': [
            {'kind': 'feature-movie', 'trackId': 1, 'trackName': 'Arrival', 'artistName': 'Denis Villeneuve'}]})
        for failure in (requests.Timeout(), {'Response': 'False', 'Error': 'Invalid API key!'},
                        {'Response': 'False', 'Error': 'Request limit reached!'},
                        {'Response': 'False', 'Error': 'Movie not found!'},
                        {'Response': 'True', 'Search': 'invalid'}, []):
            with self.subTest(failure=failure):
                get.side_effect = [failure if isinstance(failure, Exception) else self.response(failure), apple]
                self.assertEqual(lookup(self.item())['provider'], 'Apple iTunes')
                self.assertIn('itunes.apple.com', get.call_args.args[0])

    @patch('omdb.requests.get')
    def test_exact_title_director_and_type_checks(self, get):
        for changes in ({'Title': 'Arrival II'}, {'Director': 'Wrong Director'}, {'Type': 'series'},
                        {'imdbID': 'tt123'}, {'Director': 'N/A'}):
            with self.subTest(changes=changes):
                get.side_effect = [self.response(self.search(self.movie())), self.response(self.movie(**changes))]
                self.assertIsNone(lookup_omdb(self.item(), str.casefold, lambda: 'now', poster_url))
        # Similar titles and TV records are filtered before a detail request.
        get.reset_mock()
        get.side_effect = None
        get.return_value = self.response(self.search(self.movie(Title='Arrival II'), self.movie(Type='series')))
        self.assertIsNone(lookup_omdb(self.item(), str.casefold, lambda: 'now', poster_url))
        get.assert_called_once()

    @patch('omdb.requests.get')
    def test_ambiguity_and_explicit_provider_choices(self, get):
        first, second = self.movie(), self.movie(imdbID='tt123', Year='1996')
        get.side_effect = [self.response(self.search(first, second)), self.response(first), self.response(second)]
        with patch('media.lookup_balloon', return_value=None):
            result = lookup(self.item(author=''))
        self.assertEqual(len(result['candidates']), 2)
        get.reset_mock()
        get.side_effect = [self.response(second)]
        with patch('media.lookup_balloon') as primary:
            selected = lookup(self.item(author='', match_choice='omdb:tt123'))
            primary.assert_not_called()
        self.assertEqual(selected['match']['first_publish_year'], '1996')
        get.assert_called_once()
        self.assertEqual(get.call_args.kwargs['params']['i'], 'tt123')
        # A saved iTunes selection bypasses the new fallback too.
        get.reset_mock()
        get.side_effect = [self.response({'results': [
            {'kind': 'feature-movie', 'trackId': 1, 'trackName': 'Arrival'}]})]
        self.assertEqual(lookup(self.item(author='', match_choice='itunes:1'))['provider'], 'Apple iTunes')
        get.assert_called_once()
        self.assertIn('itunes.apple.com', get.call_args.args[0])

    @patch('omdb.requests.get')
    def test_search_pagination_is_bounded(self, get):
        get.return_value = self.response({'Response': 'True', 'totalResults': '1000', 'Search': [
            {'Type': 'movie', 'imdbID': 'tt1', 'Title': 'Other'}]})
        self.assertIsNone(lookup_omdb(self.item(), str.casefold, lambda: 'now', poster_url))
        self.assertEqual(get.call_count, 5)
        self.assertEqual([call.kwargs['params']['page'] for call in get.call_args_list], [1, 2, 3, 4, 5])

    @patch('omdb.requests.get')
    def test_optional_fields_and_untrusted_posters_do_not_break_match(self, get):
        for poster in ('N/A', 'https://evil.example/poster.jpg'):
            with self.subTest(poster=poster):
                movie = self.movie(Poster=poster, Plot='N/A', Genre='N/A', Runtime='N/A', Director='N/A')
                get.side_effect = [self.response(self.search(movie)), self.response(movie)]
                result = lookup_omdb(self.item(author=''), str.casefold, lambda: 'now', poster_url)
                self.assertEqual(result['poster_options'], [])
                self.assertEqual(result['match']['description'], '')
                self.assertEqual(result['match']['subject'], [])
                self.assertIsNone(result['match']['runtime'])

    @patch('media.lookup_balloon', return_value=None)
    @patch('omdb.requests.get')
    def test_enrichment_cover_picker_and_description_refresh(self, get, primary):
        movie = self.movie()
        get.side_effect = [self.response(self.search(movie)), self.response(movie)]
        self.post('/books', kind='movie', title='Arrival', author='Denis Villeneuve')
        enrich_one(self.app)
        fetch_one_description(self.app)
        with connect(self.app) as db:
            saved = dict(db.execute('SELECT * FROM titles WHERE id=1').fetchone())
        self.assertEqual(saved['status'], 'matched')
        self.assertEqual(saved['description'], movie['Plot'])
        get.reset_mock()
        get.side_effect = requests.ConnectionError()
        options = self.client.get('/books/1/cover-options').json['options']
        self.assertEqual(options[0]['poster_url'], movie['Poster'])
        get.assert_not_called()
        get.side_effect = [self.response(self.movie(Plot='Refreshed full plot.'))]
        result = fetch_media_description({'kind': 'movie', 'match': {'key': 'omdb:tt2543164'}})
        self.assertEqual(result['description'], 'Refreshed full plot.')
        self.assertEqual(get.call_args.kwargs['params']['i'], 'tt2543164')

    @patch('media.lookup_balloon', return_value=None)
    @patch('omdb.requests.get')
    def test_tv_fallback_does_not_query_omdb(self, get, primary):
        get.return_value = self.response([{'show': {'id': 1, 'name': 'Arrival'}}])
        self.assertEqual(lookup(self.item(kind='tv'))['provider'], 'TVmaze')
        get.assert_called_once()
        self.assertIn('api.tvmaze.com', get.call_args.args[0])
