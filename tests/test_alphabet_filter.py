"""Alphabet filters compose with collection search and survive status changes."""
import unittest

import test_app
from app import title_initial


class AlphabetFilterTests(unittest.TestCase):
    setUp = test_app.LibraryTests.setUp
    tearDown = test_app.LibraryTests.tearDown
    post = test_app.LibraryTests.post

    def test_filter_is_scoped_to_each_top_level_collection(self):
        for kind in ('book', 'movie', 'tv'):
            for title in ('Alpha', 'Beta'):
                self.post('/books', kind=kind, title=title + ' ' + kind)
        for kind in ('book', 'movie', 'tv'):
            page = self.client.get('/', query_string={'category': kind, 'letter': 'A'}).data
            self.assertIn(('Alpha ' + kind).encode(), page)
            self.assertNotIn(('Beta ' + kind).encode(), page)
            for other in {'book', 'movie', 'tv'} - {kind}:
                self.assertNotIn(('Alpha ' + other).encode(), page)
            self.assertEqual(page.count(b'class="alphabet-row"'), 2)
            self.assertIn(b'value="A" aria-pressed="true"', page)
            self.assertIn(('category=' + kind).encode(), page)
        page = self.client.get('/?category=book&letter=').data
        self.assertIn(b'Alpha book', page)
        self.assertIn(b'Beta book', page)

    def test_title_initial_handles_accents_punctuation_and_numbers(self):
        for title, expected in (('  "Arrival"', 'A'), ('Éclair', 'E'), ('10 Things', '#'),
                                ('The Arrival', 'T'), ('東京', '#'), ('...Zulu', 'Z')):
            self.assertEqual(title_initial(title), expected)
        for title in ('Éclair', '10 Things', 'Arrival'):
            self.post('/books', kind='movie', title=title)
        page = self.client.get('/?category=movie&letter=%23').data
        self.assertIn(b'10 Things', page)
        self.assertNotIn('Éclair'.encode(), page)
        page = self.client.get('/?category=movie&letter=e').data
        self.assertIn('Éclair'.encode(), page)
        self.assertNotIn(b'10 Things', page)
        self.assertEqual(self.client.get('/?letter=AB').status_code, 400)

    def test_search_and_tag_filters_are_combined_and_retained(self):
        for title in ('Alpha Ocean', 'Alpha Desert', 'Beta Ocean'):
            self.post('/books', title=title)
        self.post('/books/1/tags', new_tag='Favorites')
        self.post('/books/3/tags', new_tag='Favorites')
        page = self.client.get('/?category=book&letter=A&q=Ocean&tag=1').data
        self.assertIn(b'Alpha Ocean', page)
        self.assertNotIn(b'Alpha Desert', page)
        self.assertNotIn(b'Beta Ocean', page)
        self.assertIn(b'name="letter" value="A"', page)
        self.assertIn(b'name="q" value="Ocean"', page)
        self.assertIn(b'name="tag" value="1"', page)
        nav = page.split(b'<nav class="category-nav"', 1)[1].split(b'</nav>', 1)[0]
        for kind in ('book', 'movie', 'tv'):
            self.assertIn(('href="/?category=' + kind + '"').encode(), nav)
        for parameter in (b'q=', b'letter=', b'tag='):
            self.assertNotIn(parameter, nav)
        self.assertIn(b'No Titles Found', self.client.get('/?letter=Z').data)

    def test_status_save_keeps_letter_and_search(self):
        self.post('/books', kind='movie', title='Arrival')
        response = self.post('/books/1/reading-status', reading_status='Read', location='library', letter='A', q='Arrival')
        self.assertIn('letter=A', response.request.url)
        self.assertIn('category=movie', response.request.url)
        self.assertIn('q=Arrival', response.request.url)
        self.post('/settings', action='default_category', default_category='movie')
        self.post('/books', kind='book', title='Alpha')
        response = self.post('/books/2/reading-status', reading_status='Read', location='library', letter='A')
        self.assertIn('category=book', response.request.url)
        self.assertIn('letter=A', response.request.url)

    def test_tv_children_do_not_get_alphabet_buttons(self):
        self.post('/books', kind='tv', title='Example')
        self.assertNotIn(b'class="alphabet-filter"', self.client.get('/tv/1/seasons').data)
