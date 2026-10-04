const addCoverForm = document.getElementById('add-cover-form');
if (addCoverForm) {
  const fileInput = document.getElementById('cover-file');
  const button = document.getElementById('add-cover-button');
  const message = document.getElementById('add-cover-message');
  fileInput.hidden = true;
  button.type = 'button';
  button.addEventListener('click', () => fileInput.click());
  fileInput.addEventListener('change', () => {
    const file = fileInput.files[0];
    if (!file) return;
    if (file.size > 5 * 1024 * 1024) {
      message.textContent = 'Choose a cover image no larger than 5 MB.';
      message.hidden = false;
      fileInput.value = '';
      return;
    }
    button.disabled = true;
    message.textContent = 'Saving cover…';
    message.hidden = false;
    addCoverForm.requestSubmit();
  });
}

const exportLink = document.getElementById('export-json');
const exportMessage = document.getElementById('export-message');
let exporting = false;
if (exportLink && exportMessage) {
  exportLink.addEventListener('click', async event => {
    event.preventDefault();
    if (exporting) return;
    exporting = true;
    exportLink.setAttribute('aria-disabled', 'true');
    const showExportMessage = message => {
      exportMessage.textContent = message;
      exportMessage.hidden = false;
    };
    try {
      if (typeof window.showSaveFilePicker === 'function') {
        // Open before fetching to preserve the browser's user activation.
        const handle = await window.showSaveFilePicker({
          suggestedName: 'cinebook.json', id: 'cinebook-export',
          types: [{description: 'JSON library export', accept: {'application/json': ['.json']}}],
          excludeAcceptAllOption: true
        });
        const response = await fetch(exportLink.href);
        if (!response.ok) throw new Error('Could not prepare the export. Please try again.');
        const writable = await handle.createWritable();
        try {
          await writable.write(await response.blob());
          await writable.close();
        } catch (error) {
          await writable.abort().catch(() => {});
          throw error;
        }
        showExportMessage(`Export saved: ${handle.name}`);
      } else {
        showExportMessage('Choose a filename and folder in the local save dialog.');
        const response = await fetch(exportLink.dataset.saveUrl, {
          method: 'POST', body: new URLSearchParams({csrf: exportLink.dataset.csrf})
        });
        const result = await response.json();
        if (!response.ok) throw new Error(result.error || 'Could not save the export. Please try again.');
        showExportMessage(result.cancelled ? 'Export cancelled.' : `Export saved: ${result.filename}`);
      }
    } catch (error) {
      showExportMessage(error.name === 'AbortError' ? 'Export cancelled.' : error.message || 'Could not save the export. Please try again.');
    } finally {
      exporting = false;
      exportLink.removeAttribute('aria-disabled');
    }
  });
}

const libraryBooks = document.getElementById('library-books');
const viewToggle = document.querySelector('.collection .view-toggle');
if (libraryBooks && viewToggle) {
  const setLibraryView = view => {
    const selected = view === 'list' ? 'list' : 'grid';
    libraryBooks.dataset.view = selected;
    viewToggle.querySelectorAll('[data-library-view]').forEach(button => {
      button.setAttribute('aria-pressed', String(button.dataset.libraryView === selected));
    });
  };
  setLibraryView(document.body.dataset.defaultView);
  viewToggle.hidden = false;
  viewToggle.addEventListener('click', event => {
    const button = event.target.closest('[data-library-view]');
    if (!button) return;
    setLibraryView(button.dataset.libraryView);
  });
}

const searchInput = document.getElementById('search');
const searchSuggestions = document.getElementById('search-suggestions');
if (searchInput && searchSuggestions) {
  let suggestionTimer;
  let suggestionRequest;
  let suggestionVersion = 0;
  searchInput.addEventListener('input', () => {
    clearTimeout(suggestionTimer);
    suggestionRequest?.abort();
    searchSuggestions.replaceChildren();
    const version = ++suggestionVersion;
    const query = searchInput.value.trim();
    if (!query) return;
    suggestionTimer = setTimeout(async () => {
      const controller = new AbortController();
      suggestionRequest = controller;
      try {
        const url = new URL(searchInput.dataset.suggestionsUrl, window.location.origin);
        url.searchParams.set('q', query);
        const response = await fetch(url, {signal: controller.signal});
        if (!response.ok) return;
        const data = await response.json();
        if (version !== suggestionVersion) return;
        searchSuggestions.replaceChildren(...data.suggestions.map(suggestion => {
          const option = document.createElement('option');
          option.value = suggestion.value;
          option.label = suggestion.label;
          return option;
        }));
      } catch (_) { /* Keep normal search usable if suggestions are unavailable. */ }
    }, 150);
  });
}

const pending = Array.from(document.querySelectorAll('[data-pending]'), el => Number(el.dataset.pending));
const pendingCovers = Array.from(document.querySelectorAll('[data-cover-pending]'), el => Number(el.dataset.coverPending));
const pendingDescriptions = Array.from(document.querySelectorAll('[data-description-pending]'), el => Number(el.dataset.descriptionPending));
document.querySelectorAll('.cover-image').forEach(image => {
  const fallback = () => { image.hidden = true; image.parentElement.querySelector('.cover').hidden = false; };
  image.addEventListener('error', fallback);
  if (image.complete && !image.naturalWidth) fallback();
});
let dirty = false;
document.querySelectorAll('input:not([type=hidden]), textarea, select').forEach(el => ['input', 'change'].forEach(event => el.addEventListener(event, () => { dirty = true; })));
if (pending.length || pendingCovers.length || pendingDescriptions.length) {
  const timer = setInterval(async () => {
    try {
      const response = await fetch('/status');
      if (!response.ok) return;
      const data = await response.json();
      const ambiguous = data.books.filter(book => pending.includes(book.id) && book.status === 'ambiguous');
      ambiguous.forEach(book => queueMatchChoice(book.id));
      if (pending.some(id => !data.books.some(book => book.id === id && (book.status === 'pending' || book.status === 'ambiguous'))) ||
          pendingCovers.some(id => !data.books.some(book => book.id === id && (book.cover_status === 'pending' || book.status === 'ambiguous'))) ||
          pendingDescriptions.some(id => !data.books.some(book => book.id === id && (book.description_status === 'pending' || book.status === 'ambiguous')))) {
        clearInterval(timer);
        if (dirty || exporting || document.querySelector('dialog[open]') || document.activeElement.matches('input, textarea, select')) {
          document.getElementById('updated').hidden = false;
        } else { window.location.reload(); }
      }
    } catch (_) { /* The local server may be restarting. Try again next time. */ }
  }, 4000);
}
document.querySelectorAll('form[method=post]').forEach(form => form.addEventListener('submit', () => {
  form.querySelectorAll('button[type=submit]').forEach(button => { button.disabled = true; button.textContent = 'Saving…'; });
}));

const picker = document.getElementById('cover-picker');
const pickerOptions = document.getElementById('cover-picker-options');
const pickerMessage = document.getElementById('cover-picker-message');
const pickerRetry = document.getElementById('cover-picker-retry');
const pickerMore = document.getElementById('cover-picker-more');
let pickerBook = null;
let pickerOffset = 0;
let nextOffset = null;
let pickerGeneration = 0;
let savingCover = false;

async function loadCoverOptions(offset = 0) {
  const generation = ++pickerGeneration;
  const book = pickerBook;
  pickerOffset = offset;
  pickerMore.hidden = true;
  pickerRetry.hidden = true;
  pickerMessage.textContent = 'Loading covers…';
  if (offset === 0) pickerOptions.replaceChildren();
  try {
    const response = await fetch(`/books/${book}/cover-options?offset=${offset}`);
    const data = await response.json();
    if (generation !== pickerGeneration || !picker.open) return;
    if (!response.ok) throw new Error(data.error || 'Could not load covers. Please try again.');
    data.options.forEach(option => {
      const tile = document.createElement('button');
      tile.type = 'button';
      tile.className = 'cover-option';
      const selected = option.selected ?? (data.current === `id:${option.cover_id}`);
      tile.setAttribute('aria-label', `Choose ${option.title}, ${option.publish_date || 'date unknown'}, ${(option.publishers || []).join(', ')}`);
      tile.setAttribute('aria-pressed', String(selected));
      const image = document.createElement('img');
      image.src = option.image_url;
      image.alt = `Cover of ${option.title}`;
      image.loading = 'lazy';
      const caption = document.createElement('span');
      caption.textContent = [option.publish_date, ...(option.publishers || [])].filter(Boolean).join(' · ') || 'Edition details unavailable';
      const label = document.createElement('strong');
      label.textContent = selected ? 'Current cover' : 'Use this cover';
      image.addEventListener('error', () => { image.hidden = true; tile.disabled = true; label.textContent = 'Cover unavailable'; });
      tile.append(image, caption, label);
      tile.addEventListener('click', () => saveCover(option.cover_id, offset));
      pickerOptions.append(tile);
    });
    nextOffset = data.next_offset;
    pickerMore.hidden = nextOffset === null;
    pickerMessage.textContent = pickerOptions.children.length ? 'Select a cover to save it to your library.' : 'No covers found on this page. Try more covers if available.';
  } catch (error) {
    if (generation !== pickerGeneration || !picker.open) return;
    pickerMessage.textContent = error.message || 'Could not load covers. Please try again.';
    pickerRetry.hidden = false;
  }
}

async function saveCover(coverId, offset) {
  if (savingCover) return;
  savingCover = true;
  const generation = pickerGeneration;
  const book = pickerBook;
  pickerMessage.textContent = 'Saving cover…';
  pickerOptions.querySelectorAll('button').forEach(button => { button.disabled = true; });
  pickerMore.disabled = true;
  try {
    const response = await fetch(`/books/${book}/cover/select`, {
      method: 'POST',
      body: new URLSearchParams({csrf: picker.dataset.csrf, cover_id: coverId, offset})
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Could not save this cover. Please try again.');
    document.querySelectorAll(`.cover-trigger[data-book-id="${book}"]`).forEach(trigger => {
      let image = trigger.querySelector('.cover-image');
      if (!image) {
        image = document.createElement('img');
        image.className = 'cover-image';
        image.alt = `Cover of ${trigger.dataset.bookTitle}`;
        trigger.prepend(image);
      }
      image.hidden = false;
      image.src = data.image_url;
      trigger.querySelector('.cover').hidden = true;
      trigger.closest('.cover-frame').removeAttribute('data-cover-pending');
    });
    if (generation === pickerGeneration) {
      picker.close();
      const notice = document.getElementById('updated');
      notice.textContent = 'Cover saved.';
      notice.hidden = false;
    }
  } catch (error) {
    if (generation === pickerGeneration && picker.open) {
      pickerMessage.textContent = error.message || 'Could not save cover. Please try again.';
      pickerOptions.querySelectorAll('button').forEach(button => { button.disabled = button.querySelector('img').hidden; });
    }
  } finally { savingCover = false; pickerMore.disabled = false; }
}

document.querySelectorAll('.cover-trigger').forEach(trigger => trigger.addEventListener('click', () => {
  if (savingCover) return;
  pickerBook = trigger.dataset.bookId;
  document.getElementById('cover-picker-book').textContent = `${trigger.dataset.kind === 'book' ? 'English edition covers' : 'Posters'} for ${trigger.dataset.bookTitle}`;
  picker.showModal();
  loadCoverOptions();
}));
document.getElementById('cover-picker-close').addEventListener('click', () => picker.close());
picker.addEventListener('close', () => { ++pickerGeneration; });
picker.addEventListener('click', event => { if (event.target === picker) {
  const bounds = picker.getBoundingClientRect();
  if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) picker.close();
}});
pickerRetry.addEventListener('click', () => loadCoverOptions(pickerOffset));
pickerMore.addEventListener('click', () => { if (nextOffset !== null) loadCoverOptions(nextOffset); });

const metadataSettings = document.getElementById('metadata-settings-form');
if (metadataSettings) {
  const fields = Array.from(metadataSettings.querySelectorAll('input[type=checkbox][name=field]'));
  const updateCount = () => { document.getElementById('field-selection-count').textContent = `${fields.filter(field => field.checked).length} of ${fields.length} fields shown`; };
  fields.forEach(field => field.addEventListener('change', updateCount));
  ['show', 'hide'].forEach(action => document.getElementById(`fields-${action}-all`).addEventListener('click', () => {
    fields.forEach(field => { field.checked = action === 'show'; });
    dirty = true;
    updateCount();
  }));
  document.getElementById('field-search').addEventListener('input', event => {
    const query = event.target.value.toLowerCase().trim();
    let matches = 0;
    metadataSettings.querySelectorAll('.metadata-field-option').forEach(option => {
      option.hidden = !option.dataset.fieldLabel.toLowerCase().includes(query);
      if (!option.hidden) matches++;
    });
    metadataSettings.querySelectorAll('.settings-field-group').forEach(group => {
      group.hidden = !Array.from(group.querySelectorAll('.metadata-field-option')).some(option => !option.hidden);
    });
    document.getElementById('field-search-empty').hidden = matches > 0;
  });
  updateCount();
}

const matchPicker = document.getElementById('match-picker');
const matchOptions = document.getElementById('match-picker-options');
const matchMessage = document.getElementById('match-picker-message');
const matchRetry = document.getElementById('match-picker-retry');
const matchClose = document.getElementById('match-picker-close');
const matchLater = document.getElementById('match-picker-later');
const matchQueue = [];
const seenMatches = new Set();
let matchBook = null;
let matchRevision = null;
let matchGeneration = 0;
let savingMatch = false;

function queueMatchChoice(id, manual = false) {
  id = Number(id);
  if (id === matchBook || matchQueue.includes(id) || (!manual && seenMatches.has(id))) return;
  seenMatches.add(id);
  matchQueue.push(id);
  showNextMatch();
}

function showNextMatch() {
  if (!matchQueue.length || document.querySelector('dialog[open]')) return;
  matchBook = matchQueue.shift();
  matchPicker.showModal();
  loadMatchOptions();
}

async function loadMatchOptions() {
  const generation = ++matchGeneration;
  matchOptions.replaceChildren();
  matchRetry.hidden = true;
  document.getElementById('match-picker-title-name').textContent = '';
  matchMessage.textContent = 'Loading matching titles…';
  try {
    const response = await fetch(`/books/${matchBook}/match-options`);
    if (!response.ok) throw new Error(response.status === 409 ? 'This title changed. Refresh the page before choosing a match.' : 'Could not load matching titles. Please try again.');
    const data = await response.json();
    if (generation !== matchGeneration || !matchPicker.open) return;
    matchRevision = data.revision;
    document.getElementById('match-picker-title-name').textContent = data.title;
    matchMessage.textContent = 'More than one title matches. Select the one you intended, or choose later.';
    data.candidates.forEach(candidate => {
      const option = document.createElement('button');
      option.type = 'button';
      option.className = 'match-option';
      if (candidate.image_url) {
        const image = document.createElement('img');
        image.src = candidate.image_url;
        image.alt = `Cover of ${candidate.title}`;
        image.loading = 'lazy';
        image.addEventListener('error', () => { image.hidden = true; });
        option.append(image);
      }
      const info = document.createElement('span');
      info.className = 'match-option-info';
      const title = document.createElement('strong');
      title.textContent = candidate.title;
      const details = document.createElement('span');
      details.className = 'match-details';
      details.textContent = [candidate.year || 'Year unknown', ...(candidate.authors || []), candidate.provider].filter(Boolean).join(' · ');
      info.append(title, details);
      if (candidate.description) {
        const description = document.createElement('span');
        description.className = 'match-description';
        description.textContent = candidate.description;
        info.append(description);
      }
      const select = document.createElement('span');
      select.className = 'match-select';
      select.textContent = 'Choose this title →';
      info.append(select);
      option.append(info);
      option.addEventListener('click', () => saveMatch(candidate.choice));
      matchOptions.append(option);
    });
  } catch (error) {
    if (generation !== matchGeneration || !matchPicker.open) return;
    matchMessage.textContent = error.message;
    matchRetry.hidden = false;
  }
}

async function saveMatch(choice) {
  if (savingMatch) return;
  savingMatch = true;
  const book = matchBook;
  matchMessage.textContent = 'Saving your choice…';
  matchOptions.querySelectorAll('button').forEach(button => { button.disabled = true; });
  matchClose.disabled = true;
  matchLater.disabled = true;
  try {
    const response = await fetch(`/books/${book}/match/select`, {
      method: 'POST', body: new URLSearchParams({csrf: matchPicker.dataset.csrf, choice, revision: matchRevision})
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Could not save your choice. Please try again.');
    document.querySelectorAll(`[data-match-choice="${book}"]`).forEach(button => button.remove());
    if (!dirty) { window.location.reload(); return; }
    matchPicker.close();
    const notice = document.getElementById('updated');
    notice.replaceChildren(document.createTextNode('Title selected. '));
    const refresh = document.createElement('a');
    refresh.href = '';
    refresh.textContent = 'Refresh this page';
    notice.append(refresh, document.createTextNode(' after saving your edits to see the updated information.'));
    notice.hidden = false;
  } catch (error) {
    matchMessage.textContent = error.message;
    matchOptions.querySelectorAll('button').forEach(button => { button.disabled = false; });
  } finally {
    savingMatch = false;
    matchClose.disabled = false;
    matchLater.disabled = false;
  }
}

matchClose.addEventListener('click', () => matchPicker.close());
matchLater.addEventListener('click', () => matchPicker.close());
matchRetry.addEventListener('click', loadMatchOptions);
matchPicker.addEventListener('cancel', event => { if (savingMatch) event.preventDefault(); });
matchPicker.addEventListener('close', () => {
  ++matchGeneration;
  matchBook = null;
  showNextMatch();
});
picker.addEventListener('close', showNextMatch);
document.querySelectorAll('[data-match-choice]').forEach(button => {
  button.addEventListener('click', () => queueMatchChoice(button.dataset.matchChoice, true));
  queueMatchChoice(button.dataset.matchChoice);
});
