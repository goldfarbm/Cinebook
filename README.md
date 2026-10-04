
<p align="center"><img width="700" src="https://github.com/goldfarbm/Cinebook/blob/main/Cinebook.png" alt="Cinebook Header Image" /></p>

# Cinebook

A local personal collection of books, movies, and TV shows. Flask serves the interface on loopback; SQLite stores your collection and JSON metadata on your computer. No accounts, cloud storage, remote fonts, or browser requests to external services. The server fetches metadata and artwork; saved information remains available offline.

## Run

Use a current Python with OpenSSL (Python 3.10 or later recommended). 

To set up a new environment:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python app.py
```

Open [Cinebook at localhost:5000](http://127.0.0.1:5000). Stop the server with Ctrl+C. Run one server process, with no reloader, so only one metadata worker runs. The default runner already does this. If port 5000 is occupied (for example by macOS AirPlay Receiver), use:

```sh
.venv/bin/python -c 'from app import create_app; create_app().run(host="127.0.0.1", port=5050, use_reloader=False)'
```

To enable the primary movie source, obtain an [OMDb API key](https://www.omdbapi.com/apikey.aspx), open **Settings → OMDb movie data**, enter it in the masked field, and click **Save and test**. Settings shows the last connection-test result and provides **Replace key**, **Test connection**, and **Remove key** controls. Saving, replacing, or removing a key takes effect on the next provider request without restarting; already running requests may finish with the previous key. Failed tests distinguish invalid keys, request limits, and temporary unavailability. Existing unmatched movies can be retried from their detail pages.

The key is stored without Keychain in `instance/credentials.json`, separate from the collection database and JSON exports. The file is readable and writable only by its owner on macOS/Linux (permissions `0600`). The key is stored as plain text locally, never prefilled in Settings, and is not included in movie metadata or browser responses. Connection status reflects the last explicit test rather than continuous monitoring.

Terminal users can optionally set `OMDB_API_KEY` in the server's environment before starting the app:

```sh
export OMDB_API_KEY='your-api-key'
.venv/bin/python app.py
```

`OMDB_API_KEY` takes precedence over the key saved in Settings. While an override is active, Settings can test it and remove a stored key, but replacing the active key requires changing or removing the environment variable. OMDb is skipped when neither key is configured, its service fails, or no exact match for the supplied title, director, and year is found.

## Use

- On a title's detail page, **Add Cover** below **Retry cover lookup** opens the browser's native file chooser on macOS, Windows, and Linux. Select a still JPEG, PNG, or WebP image up to 5 MB and 20 million pixels to save it immediately. Cancelling leaves the current cover unchanged. Uploaded images retain their original bytes and stay in the local SQLite cache across restarts, metadata/cover retries, and match selection. Choosing a provider cover replaces the uploaded cover; changing the title, author, or ISBN resets cover choices as usual. Without JavaScript, select a file in the visible file field and click **Add Cover**.

- On **Books**, **Movies**, and **TV Shows**, the two rows below Search filter titles by their first letter. **All** clears the alphabet filter; **#** selects titles starting with numbers or other characters. Leading punctuation is ignored and accented initials use their base letter. The filter combines with search and tags, works in grid/list views, and stays selected when saving a title's status. Switching categories clears the search text, alphabet filter, and tag filter.

- Under **Settings → Movie data lookup**, click **Look up movies without covers** to queue fresh metadata lookups for every movie without a locally cached cover image. The background worker searches the configured providers and downloads available posters. Movies already queued are left in the queue; ambiguous matches require selection. Saved notes, watched status, and explicit cover choices are retained.

- Choose **Light** or **Dark** under **Settings → Appearance**. Select **Paper & Forest**, **Slate & Blue**, or **Parchment & Plum** under **Colour scheme**. Each has paired light and dark colours. Both preferences apply to every page and are saved locally across restarts.
- Open a TV show to browse its **Seasons**, then select a season to browse **Episodes**. These collections share grid/list views, search, adding/importing, artwork selection, watched status, notes, editing, and metadata controls. Season and episode data is fetched from the matched show’s provider (Balloonerismm or TVmaze) and saved locally; **Refresh seasons/episodes** checks for new entries while retaining your edits and statuses. Children inherit the parent artwork when no specific image is available. Removing a show removes its seasons and episodes; removing a season removes its episodes. JSON exports retain the hierarchy and imports remap parent IDs. Child imports accept an optional `position` column for the season or episode number.
- Select **Books**, **Movies**, or **TV Shows** on the front page to browse each category. Choose the default Home category under **Settings → Default category**; Books is the initial default. Search stays within the selected category. Each category supports adding, imports, search, grid/list views, detail pages, notes, editing, removal, metadata retries, descriptions, and locally cached cover/poster selection. Movies and TV use **To Watch** / **Watched** status labels; exports retain the shared `To Be Read` / `Read` stored values. CSV and JSON may specify `kind` as `book`, `movie`, or `tv`; otherwise imports use the selected category.

- Use **Grid** or **List** beside the collection heading to switch the main collection layout. Choose the starting view for every collection under **Settings → Default view**. The default is saved locally across restarts; collection toggles change the current page. List covers use the same saved images, scaled in the browser; no thumbnail files are generated.

- Open **Settings** from the top navigation to choose displayed metadata JSON fields. Search the fields, uncheck individual nested fields, or use Show all / Hide all, then Save settings. Preferences persist locally across restarts. Reset restores the default of showing all fields. This only changes the JSON viewer on detail pages; complete stored metadata and exports remain intact. New metadata fields are shown by default.

- Add a title manually, with optional author and ISBN. Each book has a Status: **To Be Read** (the default for new and existing books) or **Read**. Change it with the Status selector and Save button on a library card or the book page. It is saved locally and unaffected by metadata lookups.
- Import up to 25 entries per file, with a 1 MB upload limit. CSV uses a required `title` header and optional `author`, `isbn`, `notes`, and `reading_status` (`Read` or `To Be Read`). Movies also accept `year` and `alternate_title`; movies and TV shows accept `eidr`. Reading status is preserved in JSON exports and imports. UTF-8 text uses one title per line. JSON accepts an array of objects with those fields or an exported `titles` list. See `examples/books.csv`.
- When a lookup finds multiple distinct titles with the same name, a modal shows their covers, years, authors/directors, and available descriptions. Choose the intended title to continue enrichment, or Choose later and reopen the chooser from its card or detail page. Choices are saved locally across restarts and retries; changing the title, author/director/creator, ISBN, or movie year resets the match choice. An explicit author/director, ISBN, or movie year can resolve a lookup to one match.
- Title lookups run in the background. Movies first use [OMDb](https://www.omdbapi.com/) when configured, then fall back in order to [Balloonerismm’s IMDb-backed API](https://api.balloonerismm.workers.dev/docs), Apple’s iTunes Search API (Canadian catalog), and Wikipedia. TV shows first use Balloonerismm and fall back to TVmaze. Books use Open Library because Balloonerismm has no book endpoints. Movie matches require the exact normalized title and any supplied director and year; TV matches require the exact title. The optional TV creator is kept as entered. Catalog coverage varies; unmatched titles remain saved and editable. The page refreshes when metadata arrives unless you are editing a form. Unmatched books and network failures remain saved; open a book to edit its details or retry.
- Descriptions are fetched from the matched Open Library work and cached locally, with a short excerpt on each library card and the full text on the book page. Existing books are queued automatically when upgrading. Failed description lookups can be retried independently; books without a provider description show a placeholder. Work responses and descriptions are included in the metadata JSON/export, and remain viewable offline.
- On a book detail page, click its cover (or title-card fallback) to open a modal with alternate English-edition covers. Only editions explicitly marked as English are included; editions with unknown or mixed languages are excluded. Front-page covers link to the book detail page. Select a cover to save it locally; your choice survives refreshes, restarts, and metadata retries. Cover choice changes the artwork; the entered ISBN stays as entered. Edition lists and fetched previews are cached for offline reuse. Use More covers to browse further. Movie and TV detail pages use the same picker for provider posters; available alternatives depend on the provider. Changing title, author, or ISBN resets the cover choice.
- Author names are links on library cards and book pages. Each opens a page showing all locally stored titles by that author, including coauthored books. Coauthors have separate links. Author searches use only the local database and make no external requests.
- Search by title, author, or ISBN. Open a book to view metadata JSON, edit notes, or remove it.
- **Export JSON** opens a local Save As dialog so you can name the file and choose its folder. Browsers with the File System Access API use their own picker; the local macOS app uses the native system dialog. Cancelling leaves the export unsaved. Export JSON includes your entries and cached metadata. Importing an export restores book fields and queues fresh lookups; it does not restore previous metadata, IDs, or dates. For a full backup, stop the app and copy `instance/cinabook.sqlite3`.
- **Add a Movie** includes an optional **IMDb link** above EIDR. The link confirms that your entered title matches the IMDb record’s displayed or original title; it does not import other fields or replace your title. Invalid links, mismatched titles, and failed confirmation requests prevent adding until corrected or the optional link is removed. Normal background metadata lookup still runs after adding.
- Movies include an optional **Alternate Title** in add/edit forms, movie details, and library cards for original-language or translated titles (up to 500 characters). Collection search matches it; CSV/JSON imports accept `alternate_title`, and JSON exports preserve it. Existing movies start with an empty field. Editing it preserves saved metadata and artwork; metadata lookups continue to use the main Title.
- Movies include an optional **Year** in add/edit forms, accepting four-digit years from 1000 to 9999. A supplied year narrows metadata matches across movie providers and distinguishes movies with the same title from different years. CSV/JSON imports accept `year`, and JSON exports preserve it. Changing only the year queues a fresh metadata lookup while retaining the current cover.
- Movies and TV Shows include an optional **EIDR** field in their add/edit forms and title details, linked to the EIDR registry. Enter a content ID or its DOI/registry URL; the app normalizes the ID and validates its format and check character. Current metadata providers do not populate EIDR automatically. CSV/JSON imports accept `eidr`, and exports retain it.

The database keeps its original filename for compatibility with existing libraries and is created on first run in `instance/cinabook.sqlite3`. Metadata is a JSON document in the `metadata_json` column, including the selected result, original response, provider, and fetch time. The media `kind` field separates book, movie, and TV entries, including duplicate detection. Movie artwork and TV posters are saved unchanged in the same cover cache and scaled in the browser; no thumbnail versions are created. Book covers are fetched from [Open Library’s Covers API](https://openlibrary.org/dev/docs/api/covers) using the matched cover ID or your ISBN, and saved as JPEG blobs in the SQLite `covers` table. Saved covers are served locally on library and detail pages, including offline. Missing covers use decorative title cards; failed downloads can be retried from the book page. Existing books are automatically queued for a metadata refresh on the first upgrade to fetch cover IDs. JSON exports include cover references but not image bytes; a database backup includes the images.

## Metadata and privacy

The server searches these sources in order, stopping when a provider returns a match or candidates for you to choose:

| Collection | Primary source | Fallback order |
| --- | --- | --- |
| Movies | [OMDb](https://www.omdbapi.com/) when configured | [Balloonerismm (IMDb)](https://api.balloonerismm.workers.dev/docs) → Apple iTunes (Canada) → Wikipedia |
| TV shows | Balloonerismm (IMDb) | [TVmaze](https://www.tvmaze.com/api) |
| Books | [Open Library](https://openlibrary.org/dev/docs/api/search) | None |

OMDb receives the movie title and optional year, searches up to fifty results, and fetches exact-title records by IMDb ID to check the supplied director and obtain genres, full plots, runtime, and posters. Missing, invalid, or exhausted keys, service failures, and unmatched titles continue to Balloonerismm. OMDb data is attributed under its CC BY-NC 4.0 license.

Balloonerismm checks movie directors against record credits. If it has no movie match or is unavailable, the server searches [Apple’s iTunes Search API](https://developer.apple.com/library/archive/documentation/AudioVideo/Conceptual/iTuneSearchAPI/Searching.html), checking the supplied director and year locally. When iTunes has no exact movie match, the server searches Wikipedia for the film title and checks its introduction for the supplied director and year. Wikipedia descriptions link to their source article. TVmaze data is attributed under its CC BY-SA license.

Existing saved metadata remains available locally. Explicit provider choices take precedence over the default order; selecting a match keeps subsequent lookups tied to that provider until the choice is reset. Use **Look up again** on a title’s detail page to search again with the current source order. TV seasons and episodes use the same provider as their matched parent. IMDb posters are downloaded from `m.media-amazon.com`. Provider requests and poster downloads have bounded timeouts; artwork downloads are restricted to provider image hosts.

For books, the server sends the entered title and author, or ISBN, to [Open Library's Search API](https://openlibrary.org/dev/docs/api/search). Internet is needed for enrichment; saved books remain viewable offline. Results must match the normalized title and supplied author exactly, or contain the supplied ISBN. This conservatively leaves uncertain matches unresolved; matching titles can still be ambiguous, so review metadata. Your entered title is preserved. Once a match is found, the author field is updated to the names returned by Open Library, including capitalization and punctuation (and all authors for coauthored books). If author data is missing or the lookup fails, the current name is retained. Existing matched entries are updated from their cached metadata on startup.

Requests are serialized with at least 3.1 seconds between lookups, timeout after a bounded wait, and are cached locally. This is for small personal lists, not bulk harvesting. To identify regular API use with your contact address, set `CINEBOOK_USER_AGENT='Cinebook/0.1 (you@example.com)'` before starting. See [Open Library's API usage guidelines](https://openlibrary.org/developers/api).

The app binds to `127.0.0.1`, rejects non-local Host headers, checks CSRF tokens, and disables debug mode. It is designed for a single local user; do not deploy it publicly. Pending lookups survive restarts. Failed lookups require a manual retry.

## Tests

```sh
.venv/bin/python -m unittest discover -s tests -v
```

Tests use temporary databases and mocked API requests; they do not alter your library or contact external metadata providers.

## Tags

Use **Tags** in the navigation to create and delete tags. A compact starter set is derived once from selected metadata subjects/genres. Known genres and themes are normalized (for example Horror fiction → Horror); names, catalog codes, provider lists, and unrecognized subjects are omitted. A title with no useful subjects is left untagged.

For Wikipedia movie matches, genres are read from the selected article’s opening film definition (for example “science fiction drama film”), including older saved matches with empty subject lists. Rerun automatic tagging to apply these to existing movies without fetching metadata again.

Tags appear on library cards and title pages. Collection search also matches assigned tag names (case-insensitive, including partial names), alongside title, author, and ISBN. Start typing in the search field for suggestions of titles and assigned tag names from the current collection. Choose a suggestion, then press Enter or click Search. On a title page, choose tags or enter a new name and click **Save tags**. Unchecked tags stay unchecked after metadata refreshes. Custom tags stay attached when title metadata changes. Deleting a tag removes its assignments, including exclusions, without removing titles; it stays deleted across restarts and lookups. Click **Run automatic tagging** on the Tags page after adding titles to apply supported genre and theme tags from saved metadata across the library. This can recreate deleted tags while keeping manual selections.

Tags are stored in `tags` and `title_tags` with cascading foreign keys. JSON exports include the full catalog (including unused custom tags) and each title’s tag names. JSON imports restore these names and assignments; imports of duplicate titles merge their tags. Existing exports without tags continue to work. SQLite backups include all tag data.
