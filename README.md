# betterlyrics-picard

A [MusicBrainz Picard](https://picard.musicbrainz.org/) plugin that fetches synchronized **TTML** (Timed Text Markup Language) and **LRC** lyrics from the [Better Lyrics](https://github.com/better-lyrics/better-lyrics) ecosystem ([LRC.red](https://lrc.red) and [Unison](https://unison.betterlyrics.org)).

Designed for **MusicBrainz Picard 3.0+** using the modern **Plugin v3** architecture and **PyQt6**.

---

## Features

- **Native TTML Support**: Fetches and embeds Apple Music / Better Lyrics-compliant TTML XML directly into audio file metadata (`©lyr` for MP4/M4A, `USLT` for MP3, `LYRICS` and `ttml` tags for FLAC/Vorbis), preserving word-by-word timing, roles, background vocals, translations and romanization when supplied.
- **Sidecar File Export**: Saves `.ttml` and/or `.lrc` files directly alongside your audio tracks (`Track.ttml`, `Track.lrc`).
- **LRC Conversion & Word-Sync Cleaning**: Converts TTML to LRC on the fly when needed. Strips inline word timestamps (`<mm:ss.xx>`) into standard line timestamps (`[mm:ss.xx]`) for broad player compatibility (Plex, Jellyfin, Foobar2000, VLC, mobile players).
- **Lyrics Providers**:
  - **LRC.red API**: Primary synced lyrics catalog used by Better Lyrics.
  - **Unison API**: Official crowdsourced community database of Better Lyrics.
  - **LyricsPlus KPOE v2**: Apple-compatible word / syllable-timed lyrics.
  - **Better Lyrics API / Boidu**: Public Boidu cache-backed TTML fallback, with the protected Better Lyrics API available when a key is supplied.
  - **LRCLIB**: Line-synced fallback.
- **Smart Match Scoring**: Ranks results with explicit identity weights (ISRC 35%, title 30%, artist 15%, album 5%, duration 15%), strict duration tolerance (default ±5s), and exact MusicBrainz ISRC matching. Empty metadata never receives a false bonus, and a failed lyrics endpoint falls back to the next compatible candidate.
- **Background Fetch Queue**: Non-blocking worker pool fetches lyrics off the UI thread, so loading large albums never freezes Picard. Identical in-flight jobs are de-duplicated, transient failures (HTTP 429/5xx, network errors) are retried with exponential backoff, and successful responses are cached to reduce repeat lookups.
- **Rate Limiting / Anti-Abuse**: A configurable minimum delay between outbound requests paces the whole queue, preventing burst traffic and API rate-limit (`429`) responses.
- **Loss-aware lyrics model**: Keeps native TTML, enhanced word-timed LRC, line LRC and plain text as separate representations; missing word end times are never fabricated.
- **Persistent matching cache**: Stores successful bundles and manual provider choices in Picard's user-data directory, never inside the installed plugin or source repository.
- **Interactive Search Dialog**: Right-click any track or album to search manually, view results in a detailed table (title, artist, album, duration, sync type, source), and inspect lyrics with side-by-side TTML/LRC preview before applying.
- **Automation Hooks**: Automatic search and retrieval when loading tracks into Picard or when saving files.
- **Library Maintenance**: Built-in tool to scan your music directory and clean up orphaned `.ttml` and `.lrc` files whose audio counterparts have been removed.

---

## Installation

### Option 1: Install from Git URL (Recommended)

1. In MusicBrainz Picard 3.0, open **Options -> Plugins**.
2. Click **Install from URL...**
3. Enter the repository URL:
   ```
   https://github.com/zoltrons/betterlyrics-picard
   ```
4. Click **Install**. Picard will clone the repository, read `MANIFEST.toml`, and enable the plugin.

### Option 2: Install from a local checkout (developers)

Use Picard's Plugin v3 installer so the repository and `MANIFEST.toml` are
registered through the supported lifecycle:

```bash
picard-cli plugins install /path/to/betterlyrics-picard
```

For a non-git local copy, use the Plugin v3 local-install option supported by
your Picard version rather than copying files into the legacy `plugins/`
directory. Restart or reload Picard, open **Options -> Plugins**, and ensure
**Better Lyrics** is enabled.

---

## Configuration

Settings are available under **Options -> Plugins -> Better Lyrics**:

| Setting | Default | Description |
|---|---|---|
| **Primary Lyrics Format** | `TTML` | Choose between `TTML (Apple Music XML)`, `LRC (Standard Synced)`, or `Both TTML and LRC (Dual Export)`. |
| **Embed into 'lyrics' tag** | `Enabled` | Writes primary lyrics format into standard metadata tags (`©lyr`, `USLT`, `LYRICS`). |
| **Embed into 'ttml' tag** | `Enabled` | Writes raw TTML XML into a dedicated `ttml` metadata tag. |
| **Embed into 'syncedlyrics' tag** | `Disabled` | Writes synchronized lyrics to the `syncedlyrics` tag for compatible players. |
| **Save external .ttml sidecar file** | `Enabled` | Saves a `.ttml` file alongside the audio file. |
| **Save external .lrc sidecar file** | `Disabled` | Saves a `.lrc` file alongside the audio file. |
| **Clean word timestamps in LRC** | `Enabled` | Strips `<mm:ss.xx>` word-level tags from LRC output for wider player compatibility. |
| **Prefer synchronized lyrics** | `Enabled` | Prefers time-synced lyrics over plain text when available. |
| **Ignore instrumental tracks** | `Enabled` | Skips queries for tracks labeled as instrumental. |
| **Search when loading tracks** | `Enabled` | Automatically queries for lyrics when a track is added to Picard. |
| **Search when saving files** | `Disabled` | Automatically queries for lyrics when saving files. |
| **Auto overwrite existing lyrics** | `Disabled` | Overwrites existing tags and sidecars without prompting. |
| **Primary Source** | `All Sources` | Select `All Sources (Smart Hybrid)`, `LRC.red`, or `Unison`. |
| **LyricsPlus word/syllable fallback** | `Enabled` | Enables the KPOE v2 LyricsPlus adapter. |
| **Better Lyrics API / Boidu fallback** | `Enabled` | Uses Boidu's public cache endpoint without a key; add only your own authorized key when using the protected Better Lyrics API. |
| **LRCLIB fallback** | `Enabled` | Enables the line-synced LRCLIB fallback. |
| **Duration tolerance (seconds)** | `5` | Maximum allowable difference between audio track length and lyric duration. |
| **Max parallel requests** | `3` | Number of background worker threads used to fetch lyrics concurrently (higher = faster, more bandwidth). |
| **Minimum delay between requests (ms)** | `200` | Minimum spacing between outbound HTTP requests across all workers. Prevents API abuse and `429` rate-limiting (`0` disables pacing). |
| **Better Lyrics API Key** | *(empty)* | Optional authorized API key for `api.betterlyrics.org`; the keyless default uses `lyrics-api.boidu.dev`. |

---

## Usage

### Automatic Tagging
When you load albums or tracks into Picard, Better Lyrics automatically matches the track against available databases, retrieves the best match based on duration and ISRC, and stages the configured lyrics tags in Picard. Save the file in Picard to embed the staged metadata. To fetch and persist lyrics automatically after each file save, enable **Search when saving files**; that path calls Picard's file save after embedding the new metadata.

Automatic matching retains ISRC and recording-version checks. Without a target
ISRC, otherwise ambiguous releases can select one deterministic candidate only
when title, artist (including featured credits), album, and known duration agree.
Featured credits can move between the title and artist fields without rejecting
the recording. Distinct ISRCs are not merged into one lyrics document.

Existing content is protected **per destination**: a `.ttml` sidecar no longer
blocks filling an empty `lyrics` tag, and an existing `lyrics` tag does not block
an empty enabled `ttml` tag. To replace existing tags or sidecars, explicitly
enable **Auto overwrite existing lyrics**. The sidecar checkboxes control file
creation independently of the preferred embedded format. Logs distinguish
rejected matches, protected/unchanged destinations, staged tags, and requested
audio saves; a requested save is not reported as completed before its callback.

### Manual Actions
Right-click any track or album in Picard's selection view:
- **Plugins -> Get lyrics (TTML / LRC) automatically with Better Lyrics**: Executes the scoring and matching pipeline for the selected item(s).
- **Plugins -> Search lyrics (TTML / LRC) manually with Better Lyrics**: Opens the search dialog. You can refine your search query, browse candidates, preview both TTML (XML) and LRC versions, and double-click or press OK to apply.

### LRC.red lookup

- Uses the [documented API](https://lrc.red/docs) resource `/s/{ISRC}.json`
  for exact recording lookup, mapping `title`, `artist`, `album`, `duration`,
  `sync`, and `files` from the response.
- If the ISRC is absent or not found, searches by artist and title. An edited
  manual query takes precedence over the original track's ISRC; the track's
  metadata is still used to rank the returned candidates.
- Text search uses the site's current `/search.json?q=...` endpoint and its
  `hits` / `next` response (verified on 2026-10-11). This endpoint is used by
  the website but is not described on `/docs`, so it may change independently.
  Searches read at most three cursor pages and deduplicate by ISRC.
- Downloads TTML first, then LRC if unavailable, using the returned `files`
  URLs or the documented `/s/{ISRC}.ttml` and `/s/{ISRC}.lrc` resources.
- Permanent HTTP failures such as 404 are not retried; transient network,
  rate-limit and server failures retain the existing retry policy.

### Developer checks

```bash
python3.11 -m unittest discover -s tests -v
python3.11 -m py_compile __init__.py tests/test_matching.py tests/test_lrcred.py
```

The regression suite stubs Picard/Qt and HTTP responses. It does not replace
an installation and UI test inside Picard.

### Picard 3.0 album-menu crash compatibility

On the reproduced Python 3.14 / PyQt 6.11 / Qt 6.11 runtime, Picard's
album **Other versions** menu can crash during destruction when Python collects
a cycle containing its request-cancellation callback. This was reproduced with
the installed Picard helper without loading Better Lyrics or accessing music.

`lyrics_qt_compat.py` keeps the destruction handler alive independently of that
cycle. At plugin enable it replaces only the item-view menu's reference to the
recognized vulnerable helper, and only on the affected runtime family. It does
not modify Picard's application bundle, global Qt signals, or garbage collection.
Disable restores the original reference; already-open menus retain their guards
until destruction. Startup logs report whether protection was installed.

Real-Qt checks run separately from the stub suite (PyQt6 required):

```bash
QT_QPA_PLATFORM=offscreen python tests/qt_menu_smoke.py
QT_QPA_PLATFORM=offscreen python tests/qt_dispatcher_thread_smoke.py
# Requires PyInstaller and a local frozen Picard executable:
QT_QPA_PLATFORM=offscreen python tests/qt_menu_lifetime_smoke.py \
  --picard-binary "/path/to/MusicBrainz Picard.app/Contents/MacOS/picard-run"
```

The dispatcher check enables and disables the plugin from a worker thread, the
same way Picard's asynchronous install and update flows do, and verifies that
queued results are applied on the GUI thread.

The lifetime check exercises 300 menu/request cycles, cleanup and disable while
a menu is alive. The optional `--baseline` flag **deliberately crashes an isolated
child process** using the original helper, then checks the protected path. It
does not open the user's Picard session or write audio files. Verification used
Python 3.14.7, PyQt 6.11.0 and Qt 6.11.2; a full application click-through remains
a separate check.

### Orphaned File Cleaner
Under **Options -> Plugins -> Better Lyrics**, click **Clean Orphaned .ttml / .lrc Files in Library...** and select your music root directory. The plugin will recursively delete any `.ttml` or `.lrc` sidecars that no longer correspond to an audio file.

### Reliability & Performance
Automatic lyric lookups run on a background worker queue instead of the UI thread, so Picard stays responsive while large libraries are processed. The queue:

- **De-duplicates** identical in-flight jobs (adding the same track twice only fetches once).
- **Paces requests** with a configurable minimum delay, avoiding burst traffic and API rate limits.
- **Retries** transient failures (network errors, HTTP 408/425/429/5xx) with jittered exponential backoff.
- **Caches** successful responses (and briefly caches failures) to reduce repeat lookups.
- **Stops immediately on disable**: queued jobs are discarded, an already-running request is abandoned at the next cancellation point, and no result from the previous session can be applied after a re-enable. Shutdown no longer waits for retries or a drained queue.
- **Works immediately after install or update**: Picard runs plugin installation and updates on a worker thread, so `enable()` may not run on the GUI thread. The result dispatcher is moved to the Qt application thread before its timer is created; previously the timer belonged to a worker thread without an event loop and never fired, which made automatic lyrics appear broken until the plugin was toggled off and on.
- **Isolates providers**: one failing or malformed source (LRC.red, Unison, LyricsPlus, Boidu, LRCLIB) is logged and skipped, so it cannot suppress the remaining fallback chain.
- **Snapshots settings** on the GUI thread before handing work to a worker, so worker threads never read Picard's configuration object while it is being edited.
- **Bounds GUI work per tick**: completed lookups are applied in small batches, preventing a burst of hundreds of results from freezing the window.

Malformed TTML cues are skipped individually instead of discarding the whole document, and stored numeric options that are corrupt or out of range are clamped when the options page loads.

While work is outstanding, the number of tracks still waiting is shown in Picard's status bar (e.g. *Better Lyrics: 7 track(s) in lyrics queue...*), so you can see at a glance how much is left.

Tune these under **Options -> Plugins -> Better Lyrics -> Sources & Advanced Settings -> Performance & Stability**. The request delay applies immediately; the worker count takes effect after reloading the plugin or restarting Picard.

---

## License

This project is licensed under the [MIT License](LICENSE).
