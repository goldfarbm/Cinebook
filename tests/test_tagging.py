"""Tag management, conservative metadata mapping, persistence, and portable exports."""
import io
import json
import unittest

import test_app
from app import connect, create_app, enrich_one, parse_import
from tagging import metadata_tags, tag_catalog, attach_tags, ensure_tag, sync_metadata_tags


class TagTests(unittest.TestCase):
    setUp = test_app.LibraryTests.setUp
    tearDown = test_app.LibraryTests.tearDown
    post = test_app.LibraryTests.post

    def catalog(self):
        with connect(self.app) as db:
            return tag_catalog(db)

    def add_tag(self, name):
        self.post('/tags', name=name)
        return next(tag['id'] for tag in self.catalog() if tag['name'].casefold() == name.strip().casefold())

    def names(self, title_id=1):
        with connect(self.app) as db:
            books = [{'id': title_id}]
            attach_tags(db, books)
        return [tag['name'] for tag in books[0]['tags']]

    def test_metadata_mapping_ignores_catalog_noise_and_unselected_candidates(self):
        metadata = {'match': {'subject': ['Fiction', 'horror fiction', 'gothic & horror', 'Thrillers',
            'suspense & thriller', 'Science Fiction', 'post-apocalyptic', 'Fiction Classics',
            'nyt:trade-fiction-paperback=2021-01-03', 'Telephone directories', 'English language',
            'Frodo Baggins (Fictitious character)', 'Fiction, romance, historical', 'Family life']}}
        self.assertEqual(metadata_tags(metadata), {'Fiction', 'Horror', 'Thriller', 'Science Fiction',
            'Post-Apocalyptic', 'Classics', 'Romance', 'Historical Fiction', 'Family'})
        self.assertEqual(metadata_tags({'candidates': [metadata]}), set())
        self.assertEqual(metadata_tags({'match': {'subject': ['Non-fiction']}}), {'Nonfiction'})
        self.assertEqual(metadata_tags({'match': {'subject': None}}), set())
        self.assertEqual(metadata_tags({'match': {'subject': ['nyt:trade-fiction-paperback=2021-01-03']}}), set())

    def test_create_normalizes_and_deduplicates_names_and_validates_input(self):
        first = self.add_tag('Favorites')
        self.post('/tags', name='  favorites  ')
        self.assertEqual(len(self.catalog()), 1)
        for name in ('', 'x' * 61, 'Bad\x00Name'):
            page = self.post('/tags', name=name)
            self.assertEqual(page.status_code, 200)
        self.assertEqual(self.catalog()[0]['id'], first)
        self.assertEqual(len(self.catalog()), 1)

    def test_wikipedia_movie_tags_use_explicit_genres_in_selected_definition(self):
        def metadata(description, key='wikipedia:2'):
            return {'provider': 'Wikipedia', 'match': {
                'key': key, 'subject': [], 'description': description}}
        self.assertEqual(metadata_tags(metadata(
            'Arrival is a 2016 American science fiction drama film directed by Denis Villeneuve. '
            'The story includes romance and war.')), {'Fiction', 'Science Fiction', 'Drama'})
        self.assertEqual(metadata_tags(metadata(
            'Example is a 2020 animated musical comedy film.')), {'Animation', 'Musical', 'Comedy'})
        self.assertEqual(metadata_tags(metadata(
            'War is a 2020 feature film. Its story includes crime and horror.')), set())
        self.assertEqual(metadata_tags(metadata(
            'Example is a science fiction film.', key='/works/OL1W')), set())
        self.assertEqual(metadata_tags({'candidates': [metadata(
            'Example is a horror film.')]}), set())

    def test_rerun_tags_existing_wikipedia_movies_and_preserves_manual_choices(self):
        drama = self.add_tag('Drama')
        self.post('/books', kind='movie', title='Arrival')
        with connect(self.app) as db:
            metadata = {'provider': 'Wikipedia', 'match': {'key': 'wikipedia:43991244',
                'subject': [], 'description': 'Arrival is a 2016 American science fiction drama film '
                'directed by Denis Villeneuve.'}}
            db.execute("UPDATE titles SET status='matched',metadata_json=? WHERE id=1",
                       (json.dumps(metadata),))
        self.post('/books/1/tags', new_tag='Favorites')
        self.post('/tags/suggest')
        self.assertEqual(self.names(), ['Favorites', 'Fiction', 'Science Fiction'])
        self.assertNotIn('Drama', self.names())
        page = self.client.get('/?category=movie').data
        self.assertIn(b'class="tag-chip">Science Fiction', page)
        self.post('/tags/suggest')
        self.assertEqual(self.names(), ['Favorites', 'Fiction', 'Science Fiction'])

    def test_assign_create_remove_and_show_tags(self):
        self.post('/books', title='Tagged title')
        tag_id = self.add_tag('Favorites')
        self.post('/books/1/tags', tag_id=str(tag_id), new_tag='To Discuss')
        self.assertEqual(self.names(), ['Favorites', 'To Discuss'])
        self.assertIn(b'class="tag-chip">Favorites', self.client.get('/').data)
        self.assertIn(b'value="%d" checked' % tag_id, self.client.get('/books/1').data)
        self.post('/books/1/tags')
        self.assertEqual(self.names(), [])
        self.assertEqual(len(self.catalog()), 2)

    def test_stale_assignment_is_atomic_and_mutations_require_csrf(self):
        self.post('/books', title='Saved')
        tag_id = self.add_tag('Keep')
        self.post('/books/1/tags', tag_id=str(tag_id))
        self.post('/books/1/tags', tag_id='999999', new_tag='Must Not Be Created')
        self.assertEqual(self.names(), ['Keep'])
        self.assertEqual(len(self.catalog()), 1)
        for url, data in [('/tags', {'name': 'No Token'}),
                          ('/books/1/tags', {'tag_id': tag_id}),
                          (f'/tags/{tag_id}/delete', {}), ('/tags/suggest', {})]:
            self.assertEqual(self.client.post(url, data=data).status_code, 400)
        self.assertEqual(self.client.post('/books/999/tags', data={'csrf': self.csrf}).status_code, 404)

    def test_filter_combines_category_search_and_tag(self):
        tag_id = self.add_tag('Favorites')
        for kind, title in [('book', 'Alpha tagged'), ('book', 'Beta untagged'), ('movie', 'Movie tagged')]:
            self.post('/books', kind=kind, title=title)
        for title_id in (1, 3):
            self.post(f'/books/{title_id}/tags', tag_id=str(tag_id))
        page = self.client.get(f'/?category=book&tag={tag_id}').data
        self.assertIn(b'Alpha tagged', page)
        self.assertNotIn(b'Beta untagged', page)
        self.assertNotIn(b'Movie tagged', page)
        page = self.client.get(f'/?category=movie&tag={tag_id}').data
        self.assertIn(b'Movie tagged', page)
        self.assertNotIn(b'Alpha tagged', page)
        page = self.client.get(f'/?category=book&tag={tag_id}&q=Beta').data
        self.assertIn(b'No Titles Found', page)
        self.assertEqual(self.client.get('/?tag=invalid').status_code, 400)
        self.assertEqual(self.client.get('/?tag=99999').status_code, 404)

    def test_search_matches_assigned_tags_with_category_and_filter_scope(self):
        science = self.add_tag('Science Fiction')
        favorite = self.add_tag('Favorites')
        for kind, title in [('book', 'Tagged Book'), ('book', 'Unassigned Book'),
                            ('movie', 'Tagged Movie'), ('tv', 'Tagged Show')]:
            self.post('/books', kind=kind, title=title, author='Example Author')
        for title_id in (1, 3, 4):
            self.post(f'/books/{title_id}/tags', tag_id=str(science))
        for category, expected in [('book', b'Tagged Book'), ('movie', b'Tagged Movie'), ('tv', b'Tagged Show')]:
            page = self.client.get(f'/?category={category}&q=sCiEnCe%20fIcTiOn').data
            self.assertIn(expected, page)
            self.assertNotIn(b'Unassigned Book', page)
            for other in (b'Tagged Book', b'Tagged Movie', b'Tagged Show'):
                if other != expected:
                    self.assertNotIn(other, page)
        self.assertIn(b'Tagged Book', self.client.get('/?category=book&q=fiction').data)
        self.assertIn(b'No Titles Found', self.client.get('/?category=book&q=favorites').data)
        self.assertIn(b'No Titles Found', self.client.get(f'/?category=book&q=fiction&tag={favorite}').data)
        self.post('/books/1/tags')
        self.assertIn(b'No Titles Found', self.client.get('/?category=book&q=science').data)
        self.assertIn(b'Tagged Book', self.client.get('/?category=book&q=example').data)

    def test_status_save_preserves_tag_filter(self):
        self.post('/books', title='Book')
        tag_id = self.add_tag('Keep')
        self.post('/books/1/tags', tag_id=str(tag_id))
        response = self.client.post('/books/1/reading-status', data={
            'csrf': self.csrf, 'reading_status': 'Read', 'location': 'library', 'tag': str(tag_id), 'q': 'Book'})
        self.assertIn(f'tag={tag_id}', response.location)
        self.assertIn('q=Book', response.location)

    def test_tv_children_inherit_metadata_tags_and_keep_manual_choices_on_refresh(self):
        import test_tv
        test_tv.TVHierarchyTests.seed(self)
        action = self.add_tag('Action')
        drama = self.add_tag('Drama')
        with connect(self.app) as db:
            raw = json.loads(db.execute('SELECT metadata_json FROM titles WHERE id=1').fetchone()[0])
            raw['match']['subject'] = ['Action', 'Drama']
            db.execute('UPDATE titles SET metadata_json=? WHERE id=1', (json.dumps(raw),))
        self.client.get('/tv/1/seasons')
        with connect(self.app) as db:
            season = db.execute("SELECT id FROM titles WHERE source_key='tvmaze-season:20'").fetchone()['id']
        self.assertEqual(self.names(season), ['Action', 'Drama'])
        self.post(f'/books/{season}/tags', tag_id=str(drama), new_tag='Favorites')
        self.post('/tv/1/refresh-children')
        self.assertEqual(self.names(season), ['Drama', 'Favorites'])
        page = self.client.get(f'/tv/1/seasons?tag={action}').data
        self.assertNotIn(b'>Season 1</a>', page)
        self.assertIn(b'>Season 2</a>', page)
        page = self.client.get('/tv/1/seasons?q=favorites').data
        self.assertIn(b'>Season 1</a>', page)
        self.assertNotIn(b'>Season 2</a>', page)
        self.client.get(f'/tv/seasons/{season}/episodes')
        with connect(self.app) as db:
            episode = db.execute("SELECT id FROM titles WHERE source_key='tvmaze-episode:30'").fetchone()['id']
        self.assertEqual(self.names(episode), ['Action', 'Drama'])
        self.post(f'/books/{episode}/tags', new_tag='Pilot Favorite')
        page = self.client.get(f'/tv/seasons/{season}/episodes?q=pilot%20favorite').data
        self.assertEqual(page.count(b'class="book-title-link"'), 1)
        self.assertIn(b'Pilot summary.', page)
        self.post('/books/1/delete')
        with connect(self.app) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM title_tags').fetchone()[0], 0)

    def test_chosen_ambiguous_match_tags_only_selected_candidate(self):
        from matching import resolve_candidates
        self.post('/books', title='Shared name')
        self.add_tag('Fantasy')
        self.add_tag('Horror')
        candidates = [{'match': {'key': '/works/OL1W', 'subject': ['Fantasy']}},
                      {'match': {'key': '/works/OL2W', 'subject': ['Horror']}}]
        self.app.config['LOOKUP'] = lambda item: resolve_candidates(candidates, item)
        enrich_one(self.app)
        self.assertEqual(self.names(), [])
        choices = self.client.get('/books/1/match-options').json
        response = self.client.post('/books/1/match/select', data={
            'csrf': self.csrf, 'choice': 1, 'revision': choices['revision']})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.names(), ['Horror'])

    def test_seed_from_existing_metadata_only_once(self):
        self.post('/books', title='Book')
        with connect(self.app) as db:
            db.execute("UPDATE titles SET status='matched',metadata_json=? WHERE id=1",
                       (json.dumps({'match': {'subject': ['Fantasy fiction', 'Fiction']}}),))
            db.execute("DELETE FROM settings WHERE key='tags_initialized'")
        config = {'TESTING': True, 'START_WORKER': False, 'DATABASE': self.app.config['DATABASE']}
        create_app(config)
        self.assertEqual(self.names(), ['Fantasy', 'Fiction'])
        fantasy = next(tag['id'] for tag in self.catalog() if tag['name'] == 'Fantasy')
        self.post(f'/tags/{fantasy}/delete')
        create_app(config)
        self.assertEqual(self.names(), ['Fiction'])
        self.assertEqual([tag['name'] for tag in self.catalog()], ['Fiction'])
        self.post('/tags/suggest')
        self.assertEqual(self.names(), ['Fantasy', 'Fiction'])

    def test_run_automatic_tagging_covers_books_movies_and_tv_shows(self):
        self.add_tag('Fiction')
        titles = [('book', 'New fiction', ['Fiction'], ['Fiction']),
                  ('movie', 'New fantasy movie', ['Fantasy'], ['Fantasy']),
                  ('tv', 'New drama show', ['Drama'], ['Drama'])]
        for kind, title, _, _ in titles:
            self.post('/books', kind=kind, title=title)
        with connect(self.app) as db:
            for title_id, (_, _, subjects, _) in enumerate(titles, start=1):
                db.execute("UPDATE titles SET status='matched',metadata_json=? WHERE id=?",
                           (json.dumps({'match': {'subject': subjects}}), title_id))
        page = self.client.get('/tags').data
        self.assertIn(b'Run automatic tagging</button>', page)
        self.assertIn(b'action="/tags/suggest"', page)
        for title_id in range(1, len(titles) + 1):
            self.assertEqual(self.names(title_id), [])

        result = self.post('/tags/suggest')
        self.assertIn(b'Automatic tagging complete.', result.data)
        self.assertIn(b'added 2 new tags.', result.data)
        for title_id, (kind, title, _, expected) in enumerate(titles, start=1):
            self.assertEqual(self.names(title_id), expected)
            tag_id = next(tag['id'] for tag in self.catalog() if tag['name'] == expected[0])
            self.assertIn(title.encode(), self.client.get(f'/?category={kind}&tag={tag_id}').data)
        result = self.post('/tags/suggest')
        self.assertIn(b'added 0 new tags.', result.data)
        for title_id, (_, _, _, expected) in enumerate(titles, start=1):
            self.assertEqual(self.names(title_id), expected)

    def test_refresh_respects_exclusions_and_replaces_only_automatic_tags(self):
        self.post('/books', title='Book')
        fiction = self.add_tag('Fiction')
        fantasy = self.add_tag('Fantasy')
        self.add_tag('Horror')
        metadata = {'match': {'subject': ['Fiction', 'Fantasy']}}
        self.app.config['LOOKUP'] = lambda item: metadata
        enrich_one(self.app)
        self.assertEqual(self.names(), ['Fantasy', 'Fiction'])
        self.post('/books/1/tags', tag_id=str(fiction), new_tag='Favorites')
        self.post('/books/1/retry')
        enrich_one(self.app)
        self.assertEqual(self.names(), ['Favorites', 'Fiction'])
        self.assertNotIn('Fantasy', self.names())
        self.post('/tags/suggest')
        self.assertNotIn('Fantasy', self.names())
        # A second title has no manual overrides, so a new match replaces inferred genres.
        self.post('/books', title='Second')
        enrich_one(self.app)
        self.assertEqual(self.names(2), ['Fantasy', 'Fiction'])
        self.app.config['LOOKUP'] = lambda item: {'match': {'subject': ['Horror']}}
        self.post('/books/2/retry')
        enrich_one(self.app)
        self.assertEqual(self.names(2), ['Horror'])
        self.post('/books/1/edit', title='Changed', author='', notes='')
        self.assertEqual(self.names(), ['Favorites', 'Fiction'])

    def test_delete_tag_cascades_assignments_and_keeps_titles(self):
        self.post('/books', title='One')
        self.post('/books', title='Two')
        tag_id = self.add_tag('Shared')
        for title_id in (1, 2):
            self.post(f'/books/{title_id}/tags', tag_id=str(tag_id))
        self.post(f'/tags/{tag_id}/delete')
        self.assertEqual(self.names(), [])
        with connect(self.app) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM title_tags').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM titles').fetchone()[0], 2)
        self.assertEqual(self.client.post(f'/tags/{tag_id}/delete', data={'csrf': self.csrf}).status_code, 404)
        new_id = self.add_tag('Another')
        self.assertGreater(new_id, tag_id)
        self.post('/books/1/tags', tag_id=str(new_id))
        self.post('/books/1/delete')
        with connect(self.app) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM title_tags WHERE title_id=1').fetchone()[0], 0)
        self.assertEqual(len(self.catalog()), 1)

    def test_export_import_preserves_catalog_assignments_and_merges_duplicates(self):
        self.post('/books', title='One')
        self.add_tag('Unused')
        favorite = self.add_tag('Favorites')
        self.post('/books/1/tags', tag_id=str(favorite))
        exported = self.client.get('/export').json
        self.assertEqual(exported['tags'], ['Favorites', 'Unused'])
        self.assertEqual(exported['titles'][0]['tags'], ['Favorites'])
        self.post('/books/1/delete')
        for tag in self.catalog():
            self.post(f"/tags/{tag['id']}/delete")
        content = json.dumps(exported).encode()
        self.post('/import', file=(io.BytesIO(content), 'library.json'))
        self.assertEqual(self.names(), ['Favorites'])
        self.assertEqual([tag['name'] for tag in self.catalog()], ['Favorites', 'Unused'])
        extra = self.add_tag('Another')
        self.post('/books/1/tags', tag_id=str(extra))
        self.post('/import', file=(io.BytesIO(content), 'library.json'))
        self.assertEqual(self.names(), ['Another', 'Favorites'])
        with connect(self.app) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM titles').fetchone()[0], 1)

    def test_empty_library_export_restores_unused_tags(self):
        self.add_tag('Unused')
        exported = self.client.get('/export').json
        tag_id = self.catalog()[0]['id']
        self.post(f'/tags/{tag_id}/delete')
        self.post('/import', file=(io.BytesIO(json.dumps(exported).encode()), 'empty-library.json'))
        self.assertEqual([tag['name'] for tag in self.catalog()], ['Unused'])
        self.assertEqual(self.catalog()[0]['title_count'], 0)

    def test_invalid_import_does_not_create_catalog_or_titles(self):
        for tags in ('invalid', ['x' * 61], [None]):
            content = json.dumps({'version': 1, 'tags': ['Must Not Be Created'],
                                  'titles': [{'id': 1, 'title': 'Book', 'tags': tags}]}).encode()
            self.post('/import', file=(io.BytesIO(content), 'library.json'))
        self.assertEqual(self.catalog(), [])
        with connect(self.app) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM titles').fetchone()[0], 0)
        self.assertNotIn('tags', parse_import('old.json', b'[{"title":"Old"}]')[0])

    def test_escaped_tag_names_and_counts(self):
        self.post('/books', title='Book')
        self.post('/books/1/tags', new_tag='<script>alert(1)</script>')
        page = self.client.get('/tags').data
        self.assertIn(b'&lt;script&gt;alert(1)&lt;/script&gt;', page)
        self.assertNotIn(b'<script>alert(1)</script>', page)
        self.assertEqual(self.catalog()[0]['title_count'], 1)
