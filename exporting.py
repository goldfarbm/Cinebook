"""Native Save As support for the local Cinebook server."""
# Native dialogs run on the local server machine; browsers with Save As support use their own picker.
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def choose_export_path():
    # The local operating-system dialog owns filename selection; cancellation returns None.
    if sys.platform == 'darwin':
        # AppKit limits selection to JSON and supplies the normal overwrite prompt.
        script = '''ObjC.import('AppKit');
const app = $.NSApplication.sharedApplication;
app.setActivationPolicy($.NSApplicationActivationPolicyAccessory);
const panel = $.NSSavePanel.savePanel;
panel.title = 'Export Cinebook library';
panel.nameFieldStringValue = 'cinebook.json';
panel.allowedFileTypes = $(['json']);
panel.allowsOtherFileTypes = false;
panel.canCreateDirectories = true;
app.activateIgnoringOtherApps(true);
panel.runModal === $.NSModalResponseOK ? ObjC.unwrap(panel.URL.path) : '';
'''
        result = subprocess.run(['/usr/bin/osascript', '-l', 'JavaScript', '-e', script],
                                capture_output=True, text=True, timeout=600, check=True)
        return result.stdout.rstrip('\r\n') or None
    # Use Tk’s native save dialog on other platforms and always dispose of the hidden root window.
    try:
        import tkinter
        from tkinter import filedialog
        root = tkinter.Tk()
        root.withdraw()
        try:
            return filedialog.asksaveasfilename(parent=root, title='Export Cinebook library',
                initialfile='cinebook.json', defaultextension='.json',
                filetypes=[('JSON files', '*.json')]) or None
        finally:
            root.destroy()
    except (ImportError, RuntimeError) as exc:
        raise RuntimeError('A local save dialog is unavailable. Open Cinebook in a browser with Save As support.') from exc


def save_json_export(content, database_path):
    # Write beside the destination and replace it atomically so a failed write cannot truncate an existing export.
    filename = choose_export_path()
    if not filename:
        return None
    destination = Path(filename)
    # Do not allow the export to replace the live SQLite library.
    if destination.resolve() == Path(database_path).resolve():
        raise ValueError('Choose a JSON file, rather than the library database.')
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=destination.parent,
                                         prefix='.cinebook-export-', delete=False) as output:
            temporary = Path(output.name)
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        # The temporary file is on the same filesystem, allowing one atomic replacement after flushing.
        os.replace(temporary, destination)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()
    return destination.name
