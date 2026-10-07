# betterlyrics-picard

A [MusicBrainz Picard](https://picard.musicbrainz.org/) plugin that fetches synchronized **TTML** (Timed Text Markup Language) and **LRC** lyrics from the [Better Lyrics](https://github.com/better-lyrics/better-lyrics) ecosystem ([LRC.red](https://lrc.red) and [Unison](https://unison.betterlyrics.org)).

Designed for **MusicBrainz Picard 3.0+** using the modern **Plugin v3** architecture and **PyQt6**.

---

## Features

- **Native TTML Support**: Fetches and embeds Apple Music / Better Lyrics-compliant TTML XML directly into audio file metadata (`©lyr` for MP4/M4A, `USLT` for MP3, `LYRICS` and `ttml` tags for FLAC/Vorbis), preserving word-by-word timing, roles, and background vocals.
- **Sidecar File Export**: Saves `.ttml` and/or `.lrc` files directly alongside your audio tracks (`Track.ttml`, `Track.lrc`).
- **LRC Conversion & Word-Sync Cleaning**: Converts TTML to LRC on the fly when needed. Strips inline word timestamps (`<mm:ss.xx>`) into standard line timestamps (`[mm:ss.xx]`) for broad player compatibility (Plex, Jellyfin, Foobar2000, VLC, mobile players).
- **Dual Backend Providers**:
  - **LRC.red API**: Primary synced lyrics catalog used by Better Lyrics.
  - **Unison API**: Official crowdsourced community database of Better Lyrics.
  - **Better Lyrics API**: Optional support for custom API keys.
- **Smart Match Scoring**: Ranks results using title and artist fuzzy matching (`SequenceMatcher`), album identity, duration tolerance (default ±4s), and exact MusicBrainz ISRC matching.
- **Background Fetch Queue**: Non-blocking worker pool fetches lyrics off the UI thread, so loading large albums never freezes Picard. Identical in-flight jobs are de-duplicated, transient failures (HTTP 429/5xx, network errors) are retried with exponential backoff, and successful responses are cached to reduce repeat lookups.
- **Rate Limiting / Anti-Abuse**: A configurable minimum delay between outbound requests paces the whole queue, preventing burst traffic and API rate-limit (`429`) responses.
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

### Option 2: Clone directly into plugins directory

Clone this repository into Picard's plugin directory:

#### macOS
```bash
git clone https://github.com/zoltrons/betterlyrics-picard.git ~/Library/Preferences/MusicBrainz/Picard/plugins/betterlyrics-picard
```

#### Windows (PowerShell)
```powershell
git clone https://github.com/zoltrons/betterlyrics-picard.git "$env:APPDATA\MusicBrainz\Picard\plugins\betterlyrics-picard"
```

#### Linux
```bash
git clone https://github.com/zoltrons/betterlyrics-picard.git ~/.config/MusicBrainz/Picard/plugins/betterlyrics-picard
```

Restart or reload Picard, open **Options -> Plugins**, and ensure **Better Lyrics** is enabled.

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
| **Duration tolerance (seconds)** | `4` | Maximum allowable difference between audio track length and lyric duration. |
| **Max parallel requests** | `3` | Number of background worker threads used to fetch lyrics concurrently (higher = faster, more bandwidth). |
| **Minimum delay between requests (ms)** | `200` | Minimum spacing between outbound HTTP requests across all workers. Prevents API abuse and `429` rate-limiting (`0` disables pacing). |
| **Better Lyrics API Key** | *(empty)* | Optional API key for `api.betterlyrics.org`. |

---

## Usage

### Automatic Tagging
When you load albums or tracks into Picard, Better Lyrics automatically matches the track against available databases, retrieves the best match based on duration and ISRC, and writes the lyrics according to your configuration.

### Manual Actions
Right-click any track or album in Picard's selection view:
- **Plugins -> Get lyrics (TTML / LRC) automatically with Better Lyrics**: Executes the scoring and matching pipeline for the selected item(s).
- **Plugins -> Search lyrics (TTML / LRC) manually with Better Lyrics**: Opens the search dialog. You can refine your search query, browse candidates, preview both TTML (XML) and LRC versions, and double-click or press OK to apply.

### Orphaned File Cleaner
Under **Options -> Plugins -> Better Lyrics**, click **Clean Orphaned .ttml / .lrc Files in Library...** and select your music root directory. The plugin will recursively delete any `.ttml` or `.lrc` sidecars that no longer correspond to an audio file.

### Reliability & Performance
Automatic lyric lookups run on a background worker queue instead of the UI thread, so Picard stays responsive while large libraries are processed. The queue:

- **De-duplicates** identical in-flight jobs (adding the same track twice only fetches once).
- **Paces requests** with a configurable minimum delay, avoiding burst traffic and API rate limits.
- **Retries** transient failures (network errors, HTTP 408/425/429/5xx) with jittered exponential backoff.
- **Caches** successful responses (and briefly caches failures) to reduce repeat lookups.

Tune these under **Options -> Plugins -> Better Lyrics -> Sources & Advanced Settings -> Performance & Stability**. The request delay applies immediately; the worker count takes effect after reloading the plugin or restarting Picard.

---

## License

This project is licensed under the [MIT License](LICENSE).
