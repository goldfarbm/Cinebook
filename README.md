!(https://github.com/goldfarbm/Cinebook/blob/main/Cinebook.png)

# Cinebook

A local personal collection of books, movies, and TV shows. Flask serves the interface on loopback; SQLite stores your collection and JSON metadata on your computer. No accounts, cloud storage, remote fonts, or browser requests to external services. The server fetches metadata and artwork; saved information remains available offline.

## Run

Use a current Python with OpenSSL (Python 3.10 or later recommended). To set up a new environment:

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python app.py
```

Open http://127.0.0.1:5000. Stop the server with Ctrl+C. Run one server process, with no reloader, so only one metadata worker runs. The default runner already does this. If port 5000 is occupied (for example by macOS AirPlay Receiver), use:

```sh
.venv/bin/python -c 'from app import create_app; create_app().run(host="127.0.0.1", port=5050, use_reloader=False)'
```

## Use

- Movies and TV Shows include an optional **EIDR** field in their add/edit forms and title details, linked to the EIDR registry. Enter a content ID or its DOI/registry URL; the app normalizes the ID and validates its format and check character. Current metadata providers do not populate EIDR automatically. CSV/JSON imports accept `eidr`, and exports retain it.
- Choose **Light** or **Dark** under **Settings → Appearance**. Select **Paper & Forest**, **Slate & Blue**, or **Parchment & Plum** under **Colour scheme**. Each has paired light and dark colours. Both preferences apply to every page and are saved locally across restarts.
- Open a TV show to browse its **Seasons**, then select a season to browse **Episodes**. These collections share grid/list views, search, adding/importing, artwork selection, watched status, notes, editing, and metadata controls. Season and episode data is fetched from TVmaze and saved locally; **Refresh seasons/episodes** checks for new entries while retaining your edits and statuses. Children inherit the parent artwork when no specific image is available. Removing a show removes its seasons and episodes; removing a season removes its episodes. JSON exports retain the hierarchy and imports remap parent IDs. Child imports accept an optional `position` column for the season or episode number.
- Select **Books**, **Movies**, or **TV Shows** on the front page to browse each category. Choose the default Home category under **Settings → Default category**; Books is the initial default. Search stays within the selected category. Each category supports adding, imports, search, grid/list views, detail pages, notes, editing, removal, metadata retries, descriptions, and locally cached cover/poster selection. Movies and TV use **To Watch** / **Watched** status labels; exports retain the shared `To Be Read` / `Read` stored values. CSV and JSON may specify `kind` as `book`, `movie`, or `tv`; otherwise imports use the selected category.

- Use **Grid** or **List** beside the collection heading to switch the main collection layout. Choose the starting view for every collection under **Settings → Default view**. The default is saved locally across restarts; collection toggles change the current page. List covers use the same saved images, scaled in the browser; no thumbnail files are generated.

- Open **Settings** from the top navigation to choose displayed metadata JSON fields. Search the fields, uncheck individual nested fields, or use Show all / Hide all, then Save settings. Preferences persist locally across restarts. Reset restores the default of showing all fields. This only changes the JSON viewer on detail pages; complete stored metadata and exports remain intact. New metadata fields are shown by default.

- Add a title manually, with optional author and ISBN. Each book has a Status: **To Be Read** (the default for new and existing books) or **Read**. Change it with the Status selector and Save button on a library card or the book page. It is saved locally and unaffected by metadata lookups.
- Import up to 25 entries per file, with a 1 MB upload limit. CSV uses a required `title` header and optional `author`, `isbn`, `notes`, and `reading_status` (`Read` or `To Be Read`). Reading status is preserved in JSON exports and imports. UTF-8 text uses one title per line. JSON accepts an array of objects with those fields or an exported `titles` list. See `examples/books.csv`.
- When a lookup finds multiple distinct titles with the same name, a modal shows their covers, years, authors/directors, and available descriptions. Choose the intended title to continue enrichment, or Choose later and reopen the chooser from its card or detail page. Choices are saved locally across restarts and retries; changing the title, author/director/creator, or ISBN resets the choice. Explicit author/director or ISBN input can resolve a lookup to one match.
- Title lookups run in the background. Books use Open Library, movies use Apple’s iTunes Search API (Canadian catalog) with a Wikipedia fallback, and TV shows use TVmaze. Movie matches require the exact title and supplied director; TV matches require the exact title. The optional TV creator is kept as entered. Catalog coverage varies; unmatched titles remain saved and editable. The page refreshes when metadata arrives unless you are editing a form. Unmatched books and network failures remain saved; open a book to edit its details or retry.
- Descriptions are fetched from the matched Open Library work and cached locally, with a short excerpt on each library card and the full text on the book page. Existing books are queued automatically when upgrading. Failed description lookups can be retried independently; books without a provider description show a placeholder. Work responses and descriptions are included in the metadata JSON/export, and remain viewable offline.
- On a book detail page, click its cover (or title-card fallback) to open a modal with alternate English-edition covers. Only editions explicitly marked as English are included; editions with unknown or mixed languages are excluded. Front-page covers link to the book detail page. Select a cover to save it locally; your choice survives refreshes, restarts, and metadata retries. Cover choice changes the artwork; the entered ISBN stays as entered. Edition lists and fetched previews are cached for offline reuse. Use More covers to browse further. Movie and TV detail pages use the same picker for provider posters; available alternatives depend on the provider. Changing title, author, or ISBN resets the cover choice.
- Author names are links on library cards and book pages. Each opens a page showing all locally stored titles by that author, including coauthored books. Coauthors have separate links. Author searches use only the local database and make no external requests.
- Search by title, author, or ISBN. Open a book to view metadata JSON, edit notes, or remove it.
- **Export JSON** opens a local Save As dialog so you can name the file and choose its folder. Browsers with the File System Access API use their own picker; the local macOS app uses the native system dialog. Cancelling leaves the export unsaved. Export JSON includes your entries and cached metadata. Importing an export restores book fields and queues fresh lookups; it does not restore previous metadata, IDs, or dates. For a full backup, stop the app and copy `instance/cinabook.sqlite3`.

The database keeps its original filename for compatibility with existing libraries and is created on first run in `instance/cinabook.sqlite3`. Metadata is a JSON document in the `metadata_json` column, including the selected result, original response, provider, and fetch time. The media `kind` field separates book, movie, and TV entries, including duplicate detection. Movie artwork and TV posters are saved unchanged in the same cover cache and scaled in the browser; no thumbnail versions are created. Book covers are fetched from [Open Library’s Covers API](https://openlibrary.org/dev/docs/api/covers) using the matched cover ID or your ISBN, and saved as JPEG blobs in the SQLite `covers` table. Saved covers are served locally on library and detail pages, including offline. Missing covers use decorative title cards; failed downloads can be retried from the book page. Existing books are automatically queued for a metadata refresh on the first upgrade to fetch cover IDs. JSON exports include cover references but not image bytes; a database backup includes the images.

## Metadata and privacy

Movie titles and optional directors are sent to [Apple’s iTunes Search API](https://developer.apple.com/library/archive/documentation/AudioVideo/Conceptual/iTuneSearchAPI/Searching.html); TV titles are sent to [TVmaze](https://www.tvmaze.com/api). TV data is attributed to TVmaze under its CC BY-SA license. When Apple has no exact movie match, the server searches Wikipedia for the exact film title and director, fetching its introduction and original page image. Wikipedia descriptions link to their source article. Provider requests and poster downloads have bounded timeouts; artwork downloads are restricted to provider image hosts.

For books, the server sends the entered title and author, or ISBN, to [Open Library's Search API](https://openlibrary.org/dev/docs/api/search). Internet is needed for enrichment; saved books remain viewable offline. Results must match the normalized title and supplied author exactly, or contain the supplied ISBN. This conservatively leaves uncertain matches unresolved; matching titles can still be ambiguous, so review metadata. Your entered title is preserved. Once a match is found, the author field is updated to the names returned by Open Library, including capitalization and punctuation (and all authors for coauthored books). If author data is missing or the lookup fails, the current name is retained. Existing matched entries are updated from their cached metadata on startup.

Requests are serialized with at least 3.1 seconds between lookups, timeout after a bounded wait, and are cached locally. This is for small personal lists, not bulk harvesting. To identify regular API use with your contact address, set `CINEBOOK_USER_AGENT='Cinebook/0.1 (you@example.com)'` before starting. See [Open Library's API usage guidelines](https://openlibrary.org/developers/api).

The app binds to `127.0.0.1`, rejects non-local Host headers, checks CSRF tokens, and disables debug mode. It is designed for a single local user; do not deploy it publicly. Pending lookups survive restarts. Failed lookups require a manual retry.

## Tests

```sh
.venv/bin/python -m unittest discover -s tests -v
```

Tests use temporary databases and mocked API requests; they do not alter your library or contact Open Library.
