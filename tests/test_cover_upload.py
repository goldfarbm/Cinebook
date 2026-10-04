"""Manual cover uploads validate files, persist offline, and override providers."""
import io
import json
import unittest
from unittest.mock import Mock

from PIL import Image
import test_app
from app import connect, create_app, enrich_one, fetch_one_cover
from cover_uploads import MAX_COVER_BYTES, validate_cover_image


class CoverUploadTests(unittest.TestCase):
    setUp = test_app.LibraryTests.setUp
    tearDown = test_app.LibraryTests.tearDown
    post = test_app.LibraryTests.post
    book = test_app.LibraryTests.book

    def image(self, format='PNG'):
        stream = io.BytesIO()
        Image.new('RGB', (12, 18), 'blue').save(stream, format=format)
        return stream.getvalue()

    def upload(self, data=None, filename='cover.png', revision=None):
        return self.post('/books/1/cover/upload',
                         revision=str(self.book()['revision'] if revision is None else revision),
                         cover_file=(io.BytesIO(self.image() if data is None else data), filename))

    def test_upload_preserves_original_image_and_other_title_fields(self):
        for kind in ('book', 'movie', 'tv'):
            with self.subTest(kind=kind):
                self.post('/books', title='Example', kind=kind, notes='Keep notes', reading_status='Read')
                before = self.book()
                image = self.image()
                response = self.upload(image)
                self.assertIn(b'Cover added and saved locally', response.data)
                after = self.book()
                self.assertTrue(after['cover_key'].startswith('upload:'))
                self.assertEqual(after['cover_choice'], after['cover_key'])
                self.assertEqual(after['cover_status'], 'available')
                self.assertEqual(after['revision'], before['revision'] + 1)
                for field in ('title', 'author', 'kind', 'notes', 'reading_status', 'status'):
                    self.assertEqual(after[field], before[field])
                cover = self.client.get('/books/1/cover')
                self.assertEqual(cover.data, image)
                self.assertEqual(cover.mimetype, 'image/png')
                reopened = create_app({'TESTING': True, 'START_WORKER': False, 'DATABASE': self.app.config['DATABASE']})
                self.assertEqual(reopened.test_client().get('/books/1/cover').data, image)
                self.post('/books/1/delete')

    def test_jpeg_webp_and_replacement_are_served_with_correct_content_type(self):
        self.post('/books', title='Example')
        for format, mimetype in (('JPEG', 'image/jpeg'), ('WEBP', 'image/webp'), ('PNG', 'image/png')):
            with self.subTest(format=format):
                image = self.image(format)
                # Filename and submitted MIME do not determine validation or storage paths.
                self.upload(image, filename='../../anything.txt')
                response = self.client.get('/books/1/cover')
                self.assertEqual(response.data, image)
                self.assertEqual(response.mimetype, mimetype)

    def test_invalid_missing_oversized_and_stale_uploads_preserve_saved_cover(self):
        self.post('/books', title='Example')
        self.upload()
        before = self.book()
        for image in (b'not an image', b'\x89PNG\r\n\x1a\ncorrupt', self.image('JPEG')[:50],
                      b'a' * (MAX_COVER_BYTES + 1), self.image('GIF')):
            with self.subTest(image_size=len(image)):
                self.upload(image)
                self.assertEqual(self.book(), before)
        self.post('/books/1/cover/upload', revision=str(before['revision']))
        self.assertEqual(self.book(), before)
        self.upload(revision=before['revision'] - 1)
        self.assertEqual(self.book(), before)
        self.assertEqual(self.client.post('/books/1/cover/upload', data={
            'revision': str(before['revision']), 'cover_file': (io.BytesIO(self.image()), 'cover.png')}).status_code, 400)
        self.assertEqual(self.book(), before)

    def test_cover_overrides_metadata_and_cover_retries_without_network_download(self):
        self.post('/books', kind='movie', title='Example')
        self.upload()
        identifier = self.book()['cover_key']
        self.app.config['LOOKUP'] = Mock(return_value={'provider': 'OMDb', 'match': {
            'key': 'omdb:tt1', 'title': 'Example', 'poster_url': 'https://m.media-amazon.com/provider.jpg'}})
        self.app.config['FETCH_COVER'] = Mock(side_effect=AssertionError('must reuse uploaded image'))
        self.post('/books/1/retry')
        enrich_one(self.app)
        self.post('/books/1/cover/retry')
        fetch_one_cover(self.app)
        self.assertEqual(self.book()['cover_key'], identifier)
        self.assertEqual(self.book()['cover_status'], 'available')
        self.app.config['FETCH_COVER'].assert_not_called()

    def test_ambiguous_match_selection_keeps_uploaded_cover(self):
        self.post('/books', kind='movie', title='Example')
        self.upload()
        identifier = self.book()['cover_key']
        self.app.config['LOOKUP'] = Mock(return_value={'candidates': [
            {'provider': 'OMDb', 'match': {'key': 'omdb:tt1', 'title': 'Example'}}]})
        self.post('/books/1/retry')
        enrich_one(self.app)
        self.assertEqual(self.book()['cover_status'], 'available')
        result = self.post('/books/1/match/select', choice='0', revision=str(self.book()['revision']))
        self.assertEqual(result.status_code, 200)
        self.assertEqual(self.book()['cover_key'], identifier)
        self.assertEqual(self.book()['cover_choice'], identifier)
        self.assertEqual(self.book()['cover_status'], 'available')

    def test_detail_button_order_and_file_input_work_without_javascript(self):
        self.post('/books', title='Example')
        page = self.client.get('/books/1').data
        self.assertLess(page.index(b'Retry cover lookup'), page.index(b'>Add Cover</button>'))
        self.assertIn(b'type="file"', page)
        self.assertIn(b'enctype="multipart/form-data"', page)
        self.assertIn(b'action="/books/1/cover/upload"', page)
        self.assertIn(b'id="add-cover-button" type="submit"', page)

    def test_import_limit_stays_one_megabyte(self):
        response = self.post('/import', file=(io.BytesIO(b'a' * (1024 * 1024 + 1)), 'books.txt'))
        self.assertEqual(response.status_code, 413)

    def test_large_dimensions_are_rejected_before_decoding(self):
        # A highly compressible image fits the byte limit but exceeds the pixel limit.
        stream = io.BytesIO()
        Image.new('1', (5000, 5000)).save(stream, format='PNG')
        with self.assertRaisesRegex(ValueError, '20 million pixels'):
            validate_cover_image(stream.getvalue())
