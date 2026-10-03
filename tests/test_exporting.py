# Export tests replace native dialogs with mocks and use temporary files to check atomic saves.
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from exporting import save_json_export
import test_app


class ExportSaveTests(unittest.TestCase):
    setUp = test_app.LibraryTests.setUp
    tearDown = test_app.LibraryTests.tearDown
    post = test_app.LibraryTests.post

    def test_save_receives_complete_json_and_reports_filename(self):
        self.post('/books', title='A title', notes='My review')
        save = Mock(return_value='My collection.json')
        self.app.config['SAVE_EXPORT'] = save
        response = self.client.post('/export/save', data={'csrf': self.csrf, 'path': '/untrusted.json'})
        self.assertEqual(response.json, {'cancelled': False, 'filename': 'My collection.json'})
        content, database = save.call_args.args
        self.assertEqual(json.loads(content)['titles'][0]['notes'], 'My review')
        self.assertEqual(database, self.app.config['DATABASE'])
        self.assertEqual(len(save.call_args.args), 2)

    def test_cancel_failure_and_retry(self):
        save = Mock(return_value=None)
        self.app.config['SAVE_EXPORT'] = save
        self.assertTrue(self.client.post('/export/save', data={'csrf': self.csrf}).json['cancelled'])
        save.side_effect = OSError('Save failed')
        self.assertEqual(self.client.post('/export/save', data={'csrf': self.csrf}).status_code, 500)
        save.side_effect = None
        save.return_value = 'Retry.json'
        self.assertEqual(self.client.post('/export/save', data={'csrf': self.csrf}).json['filename'], 'Retry.json')

    def test_token_and_concurrent_dialog_guard(self):
        save = Mock()
        self.app.config['SAVE_EXPORT'] = save
        self.assertEqual(self.client.post('/export/save').status_code, 400)
        save.assert_not_called()
        def simultaneous(content, database):
            with self.app.test_client() as other:
                other.get('/')
                with other.session_transaction() as session:
                    csrf = session['csrf']
                self.assertEqual(other.post('/export/save', data={'csrf': csrf}).status_code, 409)
            return 'Saved.json'
        save.side_effect = simultaneous
        self.assertEqual(self.client.post('/export/save', data={'csrf': self.csrf}).status_code, 200)

    @patch('exporting.choose_export_path')
    def test_native_save_writes_selected_path_atomically(self, choose):
        destination = Path(self.temp.name) / 'My collection é.json'
        destination.write_text('Previous content')
        choose.return_value = str(destination)
        content = '{"title":"Café"}'
        self.assertEqual(save_json_export(content, self.app.config['DATABASE']), destination.name)
        self.assertEqual(destination.read_text(), content)
        self.assertFalse(list(destination.parent.glob('.cinebook-export-*')))

    @patch('exporting.choose_export_path')
    def test_cancel_and_database_protection(self, choose):
        choose.return_value = None
        self.assertIsNone(save_json_export('{}', self.app.config['DATABASE']))
        before = Path(self.app.config['DATABASE']).read_bytes()
        choose.return_value = self.app.config['DATABASE']
        with self.assertRaises(ValueError):
            save_json_export('{}', self.app.config['DATABASE'])
        self.assertEqual(Path(self.app.config['DATABASE']).read_bytes(), before)

    @patch('exporting.choose_export_path')
    @patch('exporting.os.replace', side_effect=OSError('Write failed'))
    def test_failed_save_preserves_existing_file(self, replace, choose):
        destination = Path(self.temp.name) / 'existing.json'
        destination.write_text('Previous content')
        choose.return_value = str(destination)
        with self.assertRaises(OSError):
            save_json_export('{}', self.app.config['DATABASE'])
        self.assertEqual(destination.read_text(), 'Previous content')
        self.assertFalse(list(destination.parent.glob('.cinebook-export-*')))
