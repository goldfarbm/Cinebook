"""Primary IMDb API lookup, fallback, artwork, and TV hierarchy integration."""
import json
import unittest
from unittest.mock import Mock, patch

import requests
import test_app
from app import connect, enrich_one, fetch_one_description, lookup, validate
from balloon import BASE_URL, lookup_balloon
from media import fetch_media_description, poster_url


class BalloonTests(unittest.TestCase):
    setUp = test_app.LibraryTests.setUp
    tearDown = test_app.LibraryTests.tearDown
    post = test_app.LibraryTests.post

    def response(self, payload):
        return Mock(json=Mock(return_value=payload))

    def movie(self, identifier='tt2543164', title='Arrival'):
        return {'id': identifier, 'title': title, 'original_title': title,
                'release_date': '2016-11-11', 'overview': 'A linguist meets aliens.',
                'genres': [{'id': 'Sci-Fi', 'name': 'Sci-Fi'}, {'id': 'Drama', 'name': 'Drama'}],
                'poster_path': 'https://m.media-amazon.com/images/M/arrival.jpg'}

    def credits(self, director='Denis Villeneuve'):
        return {'crew': [{'job': 'Director', 'name': director}]}

    @patch('balloon.requests.get')
    def test_primary_movie_enrichment_tags_description_and_cover_picker(self, get):
        movie = self.movie()
        def serve(url, **kwargs):
            self.assertTrue(url.startswith(BASE_URL))
            if '/search/' in url:
                return self.response({'results': [movie]})
            if url.endswith('/credits'):
                return self.response(self.credits())
            if url.endswith('/images'):
                return self.response({'posters': [{'file_path': movie['poster_path']},
                    {'file_path': 'https://m.media-amazon.com/images/M/alternate.jpg'}]})
            return self.response(movie)
        get.side_effect = serve
        self.post('/books', kind='movie', title='Arrival', author='Denis Villeneuve')
        enrich_one(self.app)
        fetch_one_description(self.app)
        self.post('/tags/suggest')
        with connect(self.app) as db:
            saved = dict(db.execute('SELECT * FROM titles WHERE id=1').fetchone())
        self.assertEqual(saved['status'], 'matched')
        self.assertEqual(json.loads(saved['metadata_json'])['match']['key'], 'balloon-movie:tt2543164')
        self.assertEqual(saved['description'], movie['overview'])
        self.assertIn(b'class="tag-chip">Science Fiction', self.client.get('/?category=movie').data)
        options = self.client.get('/books/1/cover-options').json['options']
        self.assertEqual(len(options), 2)
        self.assertEqual(get.call_args_list[0].args[0], BASE_URL + '/search/movie')
        self.assertEqual(get.call_args_list[0].kwargs['timeout'], (4, 8))

    @patch('balloon.requests.get')
    def test_ambiguous_records_and_saved_choice(self, get):
        rows = [self.movie('tt1', 'Shared name'), self.movie('tt2', 'Shared name')]
        def serve(url, **kwargs):
            if '/search/' in url:
                return self.response({'results': rows})
            if url.endswith('/credits'):
                return self.response(self.credits())
            return self.response(next(row for row in rows if url.endswith('/' + row['id'])))
        get.side_effect = serve
        item = {'kind': 'movie', 'title': 'Shared name', 'author': '', 'isbn': ''}
        self.assertEqual(len(lookup(item)['candidates']), 2)
        self.assertEqual(lookup({**item, 'match_choice': 'balloon-movie:tt2'})['match']['key'], 'balloon-movie:tt2')
        self.assertIsNone(lookup_balloon({**item, 'author': 'Wrong Director'}, str.casefold, lambda: 'now'))
        self.assertIsNone(lookup_balloon({**item, 'title': 'Wrong Title'}, str.casefold, lambda: 'now'))

    @patch('balloon.requests.get')
    def test_unavailable_primary_falls_back_to_existing_movie_provider(self, get):
        apple = {'results': [{'kind': 'feature-movie', 'trackId': 123,
                             'trackName': 'Arrival', 'primaryGenreName': 'Sci-Fi'}]}
        for failure in [requests.ConnectionError(), {'success': False, 'status_message': 'upstream 403'},
                        {'results': []}, {'results': 'invalid'}]:
            with self.subTest(failure=failure):
                get.side_effect = [failure if isinstance(failure, Exception) else self.response(failure),
                                   self.response(apple)]
                self.assertEqual(lookup({'kind': 'movie', 'title': 'Arrival', 'author': '', 'isbn': ''})['provider'], 'Apple iTunes')
                self.assertEqual(get.call_args_list[-2].args[0], BASE_URL + '/search/movie')
                self.assertIn('itunes.apple.com', get.call_args.args[0])

    @patch('balloon.requests.get')
    def test_tv_seasons_episodes_genres_and_offline_navigation(self, get):
        show = {'id': 'tt11280740', 'name': 'Severance', 'first_air_date': '2022-02-18',
                'overview': 'A workplace mystery.', 'genres': [{'name': 'Drama'}],
                'poster_path': 'https://m.media-amazon.com/images/M/show.jpg',
                'seasons': [{'season_number': 1, 'label': 'Season 1'}]}
        season = {'id': 'tt11280740:1', 'name': 'Season 1', 'season_number': 1,
                  'overview': 'First season.', 'episodes': [{'id': 'tt1', 'name': 'Pilot',
                  'episode_number': 1, 'season_number': 1, 'overview': 'Pilot summary.',
                  'air_date': '2022-02-18', 'still_path': None}]}
        def serve(url, **kwargs):
            if '/search/' in url:
                return self.response({'results': [show]})
            return self.response(season if '/season/' in url else show)
        get.side_effect = serve
        self.post('/books', kind='tv', title='Severance')
        enrich_one(self.app)
        self.post('/tags/suggest')
        page = self.client.get('/tv/1/seasons')
        self.assertIn(b'Season 1', page.data)
        with connect(self.app) as db:
            season_id = db.execute("SELECT id FROM titles WHERE kind='tv_season'").fetchone()[0]
        page = self.client.get(f'/tv/seasons/{season_id}/episodes')
        self.assertIn(b'Pilot summary.', page.data)
        with connect(self.app) as db:
            episode = dict(db.execute("SELECT * FROM titles WHERE kind='tv_episode'").fetchone())
        self.assertEqual(episode['source_key'], 'balloon-episode:tt11280740:1:1')
        self.assertIn(b'class="tag-chip">Drama', self.client.get(f'/books/{episode["id"]}').data)
        validate({**episode, 'reading_status': 'To Watch'})
        get.reset_mock()
        get.side_effect = requests.ConnectionError()
        self.assertEqual(self.client.get('/tv/1/seasons').status_code, 200)
        self.assertEqual(self.client.get(f'/tv/seasons/{season_id}/episodes').status_code, 200)
        get.assert_not_called()

    @patch('balloon.requests.get')
    def test_description_refresh_uses_selected_id(self, get):
        get.return_value = self.response(self.movie())
        result = fetch_media_description({'kind': 'movie', 'match': {'key': 'balloon-movie:tt2543164'}})
        self.assertEqual(result['description'], 'A linguist meets aliens.')
        self.assertEqual(get.call_args.args[0], BASE_URL + '/movie/tt2543164')
        self.assertEqual(poster_url(self.movie()['poster_path']), self.movie()['poster_path'])
        with self.assertRaises(ValueError):
            poster_url('https://m.media-amazon.com.evil.example/poster.jpg')
