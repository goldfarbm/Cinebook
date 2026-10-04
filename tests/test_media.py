# Media tests reuse the isolated library fixture and mock external providers and artwork.
import io
import json
import unittest
from unittest.mock import Mock, patch

from app import connect, create_app, enrich_one, fetch_one_cover, fetch_one_description, lookup, parse_import
from media import poster_url
import test_app


class MediaTests(unittest.TestCase):
    setUp = test_app.LibraryTests.setUp
    tearDown = test_app.LibraryTests.tearDown
    post = test_app.LibraryTests.post
    def test_media_add_edit_status_delete_and_duplicates(self):
        for kind in ('book', 'movie', 'tv'):
            response = self.post('/books', title='Shared title', kind=kind)
            self.assertIn(f'category={kind}', response.request.url)
        with connect(self.app) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM titles').fetchone()[0], 3)
        self.post('/books', title='Shared title', kind='movie')
        self.assertEqual(len(self.client.get('/export').json['titles']), 3)
        for item_id, kind in [(2, 'movie'), (3, 'tv')]:
            page = self.client.get(f'/books/{item_id}').data
            self.assertIn(b'To Watch', page)
            self.assertNotIn(b'name="isbn"', page)
            self.post(f'/books/{item_id}/edit', title='Renamed', author='Someone', notes='Keep this')
            with connect(self.app) as db:
                row = db.execute('SELECT * FROM titles WHERE id=?', (item_id,)).fetchone()
                self.assertEqual(row['kind'], kind)
                self.assertTrue(row['identity'].startswith(kind + ':'))
                self.assertEqual(row['notes'], 'Keep this')
            response = self.post(f'/books/{item_id}/reading-status', reading_status='Read', location='library', q='Renamed')
            self.assertIn(f'category={kind}', response.request.url)
            self.assertIn(b'Status saved: Watched.', response.data)
            response = self.post(f'/books/{item_id}/delete')
            self.assertIn(f'category={kind}', response.request.url)
        self.assertEqual(len(self.client.get('/export').json['titles']), 1)
        self.post('/books', title='Invalid', kind='unknown')
        self.assertEqual(len(self.client.get('/export').json['titles']), 1)

    def test_media_import_defaults_and_mixed_export_roundtrip(self):
        self.post('/import', kind='movie', file=(io.BytesIO(b'Arrival\nAlien'), 'movies.txt'))
        self.post('/import', kind='tv', file=(io.BytesIO(b'title,reading_status\nSeverance,Watched'), 'shows.csv'))
        exported = self.client.get('/export').data
        entries = parse_import('export.json', exported)
        self.assertEqual([entry['kind'] for entry in entries], ['movie', 'movie', 'tv'])
        self.assertEqual(entries[-1]['reading_status'], 'Read')
        self.assertEqual(parse_import('a.txt', b'Arrival', 'movie')[0]['kind'], 'movie')
        self.post('/import', kind='tv', file=(io.BytesIO(exported), 'export.json'))
        self.assertEqual(len(self.client.get('/export').json['titles']), 3)

    @patch('media.requests.get')
    @patch('media.lookup_balloon', new=lambda *args: None)
    def test_lookup_uses_the_right_provider_and_strips_tv_html(self, get):
        response = Mock()
        get.return_value = response
        response.json.return_value = {'results': [{'kind': 'feature-movie', 'trackId': 123, 'trackName': 'Arrival',
            'artistName': 'Denis Villeneuve', 'releaseDate': '2016-11-11', 'primaryGenreName': 'Sci-Fi',
            'longDescription': 'A linguist meets aliens.', 'artworkUrl100': 'https://is1-ssl.mzstatic.com/image.jpg'}]}
        movie = lookup({'kind': 'movie', 'title': 'Arrival', 'author': 'Denis Villeneuve', 'isbn': ''})
        self.assertEqual(movie['provider'], 'Apple iTunes')
        self.assertEqual(movie['match']['first_publish_year'], '2016')
        self.assertIn('itunes.apple.com', get.call_args.args[0])
        self.assertIsNone(lookup({'kind': 'movie', 'title': 'Wrong', 'author': '', 'isbn': ''}))
        response.json.return_value = [{'show': {'id': 456, 'name': 'Severance', 'premiered': '2022-02-18',
            'genres': ['Drama'], 'summary': '<p>Work &amp; life.</p>', 'image': {'original': 'https://static.tvmaze.com/a.jpg'}}}]
        tv = lookup({'kind': 'tv', 'title': 'Severance', 'author': '', 'isbn': ''})
        self.assertEqual(tv['provider'], 'TVmaze')
        self.assertEqual(tv['match']['description'], 'Work & life.')
        self.assertIn('api.tvmaze.com', get.call_args.args[0])

    def test_media_enrichment_cache_poster_choice_and_retry(self):
        for kind in ('movie', 'tv'):
            with self.subTest(kind=kind):
                self.post('/books', title='Example ' + kind, kind=kind)
                with connect(self.app) as db:
                    item_id = db.execute('SELECT MAX(id) FROM titles').fetchone()[0]
                url = 'https://static.tvmaze.com/original.jpg'
                self.app.config['LOOKUP'] = lambda item: {'provider': 'TVmaze', 'match': {
                    'key': 'tvmaze:123', 'title': item['title'], 'description': 'Cached summary.', 'poster_url': url}}
                enrich_one(self.app)
                fetch_one_description(self.app)
                image = b'\xff\xd8\xffposter'
                self.app.config['FETCH_COVER'] = Mock(return_value=image)
                fetch_one_cover(self.app)
                self.assertEqual(self.client.get(f'/books/{item_id}/cover').data, image)
                self.assertIn(b'Cached summary.', self.client.get(f'/?category={kind}').data)
                self.app.config['FETCH_MEDIA_DESCRIPTION'] = Mock(return_value={'description': 'Refreshed summary.'})
                self.post(f'/books/{item_id}/description/retry')
                fetch_one_description(self.app)
                self.assertIn(b'Refreshed summary.', self.client.get(f'/books/{item_id}').data)
                self.app.config['FETCH_MEDIA_DESCRIPTION'].assert_called_once()
                alternate = 'https://static.tvmaze.com/alternate.jpg'
                self.app.config['FETCH_MEDIA_COVER_OPTIONS'] = Mock(return_value={'options': [
                    {'cover_id': 999, 'title': 'Example', 'publish_date': '2022', 'publishers': [], 'poster_url': alternate}], 'next_offset': None})
                self.client.get(f'/books/{item_id}/cover-options')
                response = self.client.post(f'/books/{item_id}/cover/select', data={'csrf': self.csrf, 'cover_id': 999})
                self.assertEqual(response.status_code, 200)
                self.post(f'/books/{item_id}/retry')
                enrich_one(self.app)
                fetch_one_description(self.app)
                fetch_one_cover(self.app)
                with connect(self.app) as db:
                    row = db.execute('SELECT * FROM titles WHERE id=?', (item_id,)).fetchone()
                    self.assertEqual(row['cover_choice'], 'poster:' + alternate)
                    self.assertEqual(row['cover_key'], 'poster:' + alternate)
                reopened = create_app({'TESTING': True, 'START_WORKER': False, 'DATABASE': self.app.config['DATABASE']})
                self.assertEqual(reopened.test_client().get(f'/books/{item_id}/cover').data, image)
                self.post(f'/books/{item_id}/delete')

    def test_poster_urls_reject_untrusted_hosts(self):
        for url in ['http://static.tvmaze.com/a.jpg', 'https://localhost/a.jpg',
                    'https://static.tvmaze.com.evil.com/a.jpg', 'https://evil.com/a.jpg',
                    'https://user:password@static.tvmaze.com/a.jpg']:
            with self.assertRaises(ValueError):
                poster_url(url)
        self.assertEqual(poster_url('https://is1-ssl.mzstatic.com/a.jpg'), 'https://is1-ssl.mzstatic.com/a.jpg')

    @patch('media.requests.get')
    @patch('media.lookup_balloon', new=lambda *args: None)
    def test_movie_catalog_fallback_requires_exact_film_and_director(self, get):
        apple = Mock()
        apple.json.return_value = {'results': []}
        wiki = Mock()
        wiki.json.return_value = {'query': {'pages': {
            '1': {'pageid': 1, 'title': 'Arrival (soundtrack)', 'index': 1, 'extract': 'A film soundtrack.'},
            '2': {'pageid': 2, 'title': 'Arrival (film)', 'index': 2,
                  'extract': 'Arrival is a 2016 science fiction film directed by Denis Villeneuve and written by Eric Heisserer.',
                  'original': {'source': 'https://upload.wikimedia.org/wikipedia/en/a/arrival.jpg'}}}}}
        get.side_effect = [apple, wiki]
        result = lookup({'kind': 'movie', 'title': 'Arrival', 'author': 'Denis Villeneuve', 'isbn': ''})
        self.assertEqual(result['provider'], 'Wikipedia')
        self.assertEqual(result['match']['key'], 'wikipedia:2')
        self.assertEqual(result['match']['author_name'], ['Denis Villeneuve'])
        self.assertEqual(result['match']['first_publish_year'], '2016')
        self.assertEqual(result['match']['subject'], ['Science Fiction'])
        get.side_effect = [apple, wiki]
        self.assertIsNone(lookup({'kind': 'movie', 'title': 'Arrival', 'author': 'Wrong Director', 'isbn': ''}))

    @patch('media.requests.get')
    @patch('media.lookup_balloon', new=lambda *args: None)
    def test_wikipedia_accepts_comedy_directed_by_without_early_film_keyword(self, get):
        apple, wiki = Mock(), Mock()
        apple.json.return_value = {'results': []}
        title = '10 Things I Hate About You'
        intro = (title + ' is a 1999 American teen romantic comedy directed by Gil Junger '
                 '(in his directorial debut) from a screenplay by Karen McCullah Lutz and Kirsten Smith. '
                 "Loosely inspired by and based on William Shakespeare's comedy The Taming of the Shrew, "
                 'it stars Julia Stiles, Heath Ledger, Joseph Gordon-Levitt, Larisa Oleynik, Larry Miller, '
                 'Andrew Keegan, David Krumholtz, Susan May Pratt, and Gabrielle Union. '
                 '\nIn the film, high-school student Cameron James is unable to date Bianca.')
        wiki.json.return_value = {'query': {'pages': {
            '398934': {'pageid': 398934, 'title': title, 'extract': intro}}}}
        for author in ('', 'Gil Junger', 'Wrong Director'):
            with self.subTest(author=author):
                get.side_effect = [apple, wiki]
                result = lookup({'kind': 'movie', 'title': title, 'author': author, 'isbn': ''})
                if author == 'Wrong Director':
                    self.assertIsNone(result)
                else:
                    self.assertEqual(result['match']['key'], 'wikipedia:398934')
                    self.assertEqual(result['match']['author_name'], ['Gil Junger'])

    @patch('media.requests.get')
    @patch('media.lookup_balloon', new=lambda *args: None)
    def test_wikipedia_director_excludes_directorial_debut_phrase(self, get):
        apple, wiki = Mock(), Mock()
        apple.json.return_value = {'results': []}
        title = 'Anchorman: The Legend of Ron Burgundy'
        for phrase in ('in his directorial debut', 'in her directorial debut',
                       'in their feature directorial debut'):
            with self.subTest(phrase=phrase):
                wiki.json.return_value = {'query': {'pages': {
                    '709490': {'pageid': 709490, 'title': title, 'extract':
                        title + ' is a 2004 American satirical comedy film directed by Adam McKay '
                        + phrase + ', produced by Judd Apatow, starring Will Ferrell.'}}}}
                for author in ('Adam McKay', 'Wrong Director'):
                    get.side_effect = [apple, wiki]
                    result = lookup({'kind': 'movie', 'title': title, 'author': author, 'isbn': ''})
                    if author == 'Wrong Director':
                        self.assertIsNone(result)
                    else:
                        self.assertEqual(result['match']['key'], 'wikipedia:709490')
                        self.assertEqual(result['match']['author_name'], ['Adam McKay'])

    @patch('media.requests.get')
    @patch('media.lookup_balloon', new=lambda *args: None)
    def test_wikipedia_rejects_other_media_that_mention_a_film(self, get):
        apple, wiki = Mock(), Mock()
        apple.json.return_value = {'results': []}
        for intro in ('Example is a song from a film.',
                      'Example is a comedy television series directed by Someone.',
                      'Example is a novel adapted into a film.',
                      'Example is a soundtrack album for a film.'):
            with self.subTest(intro=intro):
                wiki.json.return_value = {'query': {'pages': {
                    '1': {'pageid': 1, 'title': 'Example', 'extract': intro}}}}
                get.side_effect = [apple, wiki]
                self.assertIsNone(lookup({'kind': 'movie', 'title': 'Example', 'author': '', 'isbn': ''}))
