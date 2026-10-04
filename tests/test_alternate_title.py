import io
import re
import sqlite3
import unittest
from pathlib import Path

from app import connect, create_app, enrich_one
import test_app


class AlternateTitleTests(unittest.TestCase):
    setUp = test_app.LibraryTests.setUp
    tearDown = test_app.LibraryTests.tearDown
    post = test_app.LibraryTests.post
    book = test_app.LibraryTests.book

    def test_movie_directors_remain_plain_text(self):
        director = 'Dan Trachtenberg in his directorial debut'
        self.post('/books', kind='movie', title='10 Cloverfield Lane', author=director,
                  alternate_title='Alternate movie title')
        for url in ('/books/1', '/?category=movie'):
            html = self.client.get(url).data.decode()
            self.assertIn(director, html)
            self.assertIn('Alternate movie title', html)
            self.assertIsNone(re.search(r'<a\b[^>]*>\s*' + re.escape(director) + r'\s*</a>', html))
            self.assertNotIn('?category=movie&amp;q=Dan', html)

    def test_unicode_save_search_edit_and_metadata_preservation(self):
        alternate = '千と千尋の神隠し'
        page = self.client.get('/?category=movie').data
        self.assertIn(b'name="alternate_title"', page)
        self.assertNotIn(b'name="alternate_title"', self.client.get('/?category=book').data)
        self.assertNotIn(b'name="alternate_title"', self.client.get('/?category=tv').data)
        self.post('/books', kind='movie', title='Spirited Away', alternate_title='  ' + alternate + '  ')
        self.assertEqual(self.book()['alternate_title'], alternate)
        self.assertIn(alternate.encode(), self.client.get('/books/1').data)
        self.assertIn(b'Spirited Away', self.client.get('/?category=movie&q=' + alternate).data)
        self.app.config['LOOKUP'] = lambda item: {'match': {'title': item['title']}}
        enrich_one(self.app)
        with connect(self.app) as db:
            db.execute("UPDATE titles SET cover_key='saved-poster',description='Saved summary' WHERE id=1")
        metadata = self.book()['metadata_json']
        self.post('/books/1/edit', title='Spirited Away', alternate_title='Le Voyage de Chihiro')
        saved = self.book()
        self.assertEqual(saved['alternate_title'], 'Le Voyage de Chihiro')
        self.assertEqual((saved['status'], saved['metadata_json'], saved['cover_key'], saved['description']),
                         ('matched', metadata, 'saved-poster', 'Saved summary'))
        self.post('/books/1/edit', title='Spirited Away', notes='A review')
        self.assertEqual(self.book()['alternate_title'], 'Le Voyage de Chihiro')
        self.post('/books/1/edit', title='Spirited Away', alternate_title='')
        self.assertEqual(self.book()['alternate_title'], '')

    def test_import_export_round_trip_and_old_imports(self):
        csv = 'title,alternate_title\nSpirited Away,千と千尋の神隠し'
        self.post('/import', kind='movie', file=(io.BytesIO(csv.encode()), 'movies.csv'))
        exported = self.client.get('/export')
        self.assertEqual(exported.json['titles'][0]['alternate_title'], '千と千尋の神隠し')
        self.post('/books/1/delete')
        self.post('/import', file=(io.BytesIO(exported.data), 'library.json'))
        self.assertEqual(self.book()['alternate_title'], '千と千尋の神隠し')
        self.post('/import', kind='movie', file=(io.BytesIO(b'Arrival'), 'movies.txt'))
        self.assertEqual(self.client.get('/export').json['titles'][1]['alternate_title'], '')

    def test_length_validation_and_non_movie_scope(self):
        self.post('/books', kind='movie', title='Valid', alternate_title='a' * 500)
        self.assertEqual(len(self.book()['alternate_title']), 500)
        response = self.post('/books/1/edit', title='Valid', alternate_title='a' * 501)
        self.assertIn(b'at most 500 characters', response.data)
        self.assertEqual(len(self.book()['alternate_title']), 500)
        self.post('/books', kind='book', title='Book', alternate_title='Ignored')
        self.assertEqual(self.client.get('/export').json['titles'][1]['alternate_title'], '')

    def test_existing_database_gets_empty_alternate_title(self):
        database = str(Path(self.temp.name) / 'old.db')
        with sqlite3.connect(database) as db:
            db.execute('''CREATE TABLE titles (id INTEGER PRIMARY KEY, kind TEXT DEFAULT 'book',
                title TEXT,author TEXT,isbn TEXT,notes TEXT,identity TEXT UNIQUE,metadata_json TEXT,
                status TEXT,error TEXT,added_at TEXT,revision INTEGER DEFAULT 0)''')
            db.execute("INSERT INTO titles VALUES (1,'movie','Arrival','','','','movie:arrival','{}','pending','','2026-10-03',0)")
        upgraded = create_app({'TESTING': True, 'START_WORKER': False, 'DATABASE': database})
        with connect(upgraded) as db:
            row = db.execute('SELECT * FROM titles').fetchone()
            self.assertEqual((row['title'], row['alternate_title']), ('Arrival', ''))
        self.assertEqual(upgraded.test_client().get('/books/1').status_code, 200)
