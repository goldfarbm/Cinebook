"""Local OMDb credentials, connection testing, and immediate worker activation."""
import json
import os
from pathlib import Path
import stat
import unittest
from unittest.mock import Mock, patch

import requests
import test_app
from app import create_app, enrich_one
from credentials import read_credentials, using_credentials
from omdb import api_key, test_api_key


class OmdbSettingsTests(unittest.TestCase):
    tearDown = test_app.LibraryTests.tearDown
    post = test_app.LibraryTests.post
    book = test_app.LibraryTests.book

    def setUp(self):
        test_app.LibraryTests.setUp(self)
        env = patch.dict(os.environ, {'OMDB_API_KEY': ''})
        env.start()
        self.addCleanup(env.stop)
        self.path = Path(self.app.config['CREDENTIALS_FILE'])

    def active_key(self):
        with using_credentials(self.path):
            return api_key()

    @patch('app.test_omdb_api_key', return_value='Connected')
    def test_save_replace_remove_and_persistence_without_restart(self, test):
        self.assertIn(b'Not configured', self.client.get('/settings').data)
        response = self.post('/settings/omdb', action='save', api_key='private-test-key')
        self.assertIn(b'Connected', response.data)
        self.assertIn(b'type="password"', response.data)
        self.assertIn(b'Replace key', response.data)
        self.assertIn(b'Remove key', response.data)
        self.assertNotIn(b'private-test-key', response.data)
        self.assertEqual(self.active_key(), 'private-test-key')
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        reopened = create_app({'TESTING': True, 'START_WORKER': False,
                               'DATABASE': self.app.config['DATABASE']})
        self.assertEqual(reopened.config['CREDENTIALS_FILE'], str(self.path))
        self.assertIn(b'Connected', reopened.test_client().get('/settings').data)
        self.assertNotIn(b'private-test-key', self.client.get('/export').data)
        self.post('/settings/omdb', action='save', api_key='replacement-key')
        self.assertEqual(self.active_key(), 'replacement-key')
        response = self.post('/settings/omdb', action='remove')
        self.assertIn(b'Not configured', response.data)
        self.assertEqual(self.active_key(), '')
        self.assertNotIn('omdb_api_key', read_credentials(self.path))

    @patch('app.test_omdb_api_key', return_value='Connected')
    def test_environment_override_is_never_displayed_and_can_be_tested(self, test):
        self.post('/settings/omdb', action='save', api_key='local-private-key')
        with patch.dict(os.environ, {'OMDB_API_KEY': 'override-private-key'}):
            self.assertEqual(self.active_key(), 'override-private-key')
            response = self.post('/settings/omdb', action='test')
            test.assert_called_with('override-private-key')
            self.assertIn(b'Connected', response.data)
            self.assertIn(b'Using OMDB_API_KEY', response.data)
            self.assertNotIn(b'override-private-key', response.data)
            self.post('/settings/omdb', action='save', api_key='ignored-key')
            self.assertEqual(read_credentials(self.path)['omdb_api_key'], 'local-private-key')
            with patch.dict(os.environ, {'OMDB_API_KEY': 'different-key'}):
                self.assertIn(b'Not tested', self.client.get('/settings').data)
            self.post('/settings/omdb', action='remove')
            self.assertEqual(self.active_key(), 'override-private-key')
        self.assertEqual(self.active_key(), '')

    @patch('app.test_omdb_api_key')
    def test_connection_status_and_retesting(self, test):
        for status in ('Invalid key', 'Unavailable', 'Request limit reached', 'Connected'):
            with self.subTest(status=status):
                test.return_value = status
                response = self.post('/settings/omdb', action='save', api_key='private-key')
                self.assertIn(status.encode(), response.data)
                self.assertNotIn(b'private-key', response.data)
        test.return_value = 'Invalid key'
        response = self.post('/settings/omdb', action='test')
        self.assertIn(b'Invalid key', response.data)
        test.assert_called_with('private-key')

    @patch('app.test_omdb_api_key')
    def test_empty_invalid_and_csrf_rejected_requests_preserve_key(self, test):
        for key in ('', ' ', '<script>', 'a' * 129):
            self.post('/settings/omdb', action='save', api_key=key)
            self.assertEqual(self.active_key(), '')
        self.client.post('/settings/omdb', data={'action': 'save', 'api_key': 'secret'})
        self.assertEqual(self.active_key(), '')
        test.assert_not_called()
        self.assertEqual(self.post('/settings/omdb', action='unknown').status_code, 400)

    @patch('app.test_omdb_api_key', return_value='Connected')
    @patch('media.lookup_balloon', return_value=None)
    @patch('omdb.requests.get')
    def test_worker_reads_replaced_credentials_on_next_lookup(self, get, primary, test):
        self.post('/settings/omdb', action='save', api_key='first-key')
        movie = {'Response': 'True', 'Type': 'movie', 'imdbID': 'tt2543164',
                 'Title': 'Arrival', 'Director': 'Denis Villeneuve', 'Plot': 'A full plot.'}
        search = {'Response': 'True', 'totalResults': '1', 'Search': [movie]}
        for key in ('first-key', 'second-key'):
            if key == 'second-key':
                self.post('/settings/omdb', action='save', api_key=key)
                self.post('/books/1/retry')
            else:
                self.post('/books', kind='movie', title='Arrival')
            get.side_effect = [Mock(json=Mock(return_value=search)), Mock(json=Mock(return_value=movie))]
            enrich_one(self.app)
            self.assertEqual(get.call_args.kwargs['params']['apikey'], key)
            self.assertEqual(self.book()['status'], 'matched')
            self.assertNotIn(key, self.book()['metadata_json'])

    @patch('omdb.requests.get')
    def test_api_key_validation_classifies_errors_without_echoing_secrets(self, get):
        for payload, expected in (
                ({'Response': 'True', 'imdbID': 'tt0133093'}, 'Connected'),
                ({'Response': 'False', 'Error': 'Invalid API key!'}, 'Invalid key'),
                ({'Response': 'False', 'Error': 'Request limit reached!'}, 'Request limit reached'),
                ({'Response': 'False', 'Error': 'Some private upstream text'}, 'Unavailable'),
                ([], 'Unavailable')):
            get.return_value = Mock(status_code=200, json=Mock(return_value=payload))
            self.assertEqual(test_api_key('private-key'), expected)
        get.return_value = Mock(status_code=401)
        self.assertEqual(test_api_key('private-key'), 'Invalid key')
        get.side_effect = requests.Timeout('private-key')
        self.assertEqual(test_api_key('private-key'), 'Unavailable')

    @patch('app.test_omdb_api_key', return_value='Connected')
    def test_storage_failure_reports_safe_error(self, test):
        with patch('app.update_credentials', side_effect=OSError('private-key')):
            response = self.post('/settings/omdb', action='save', api_key='private-key')
        self.assertIn(b'Could not update OMDb settings', response.data)
        self.assertNotIn(b'private-key', response.data)
