# YouTube Music FastAPI API

This project provides a FastAPI-based API for interacting with YouTube Music, allowing you to fetch song information, playlists, and lyrics. It also extracts audio download links and resizes song thumbnails.

## Features

- Fetch song information, including best audio download URL and base64-encoded song thumbnail.
- Retrieve playlists and library playlists with track details.
- Fetch lyrics for a given song.
- Automatically selects the best available audio format using `yt-dlp`.
- Supports song thumbnails resizing and conversion to base64.

## Endpoints

### 1. Fetch Song Information

**POST** `/fetch_song_info`

Request:
```json
{
  "video_id": "song_video_id"
}
```

Response:
```json
{
  "download_url": "audio_download_url",
  "thumbnail_base64": "base64_encoded_thumbnail"
}
```

### 2. Fetch Playlist Information

**POST** `/fetch_playlist`

Request:
```json
{
  "playlist_id": "playlist_id"
}
```

Response:
```json
{
  "title": "Playlist Title",
  "playlistId": "playlist_id",
  "tracks": [ ... ]  // Track details
}
```

### 3. Fetch Library Playlists

**GET** `/fetch_library_playlists`

Response:
```json
[
  {
    "title": "Playlist Title",
    "playlistId": "playlist_id",
    "tracks": [ ... ]  // Track details
  },
  ...
]
```

### 4. Fetch Song Lyrics

**POST** `/fetch_lyrics`

Request:
```json
{
  "video_id": "song_video_id"
}
```

Response:
```json
{
  "video_id": "song_video_id",
  "lyrics": "Lyrics text"
}
```

## Setup

### 1. Clone the repository

```bash
git clone https://github.com/your-username/ytmusic-fastapi-api.git
cd ytmusic-fastapi-api
```

### 2. Create and activate a virtual environment (optional but recommended)

```bash
python3 -m venv env
source env/bin/activate  # On Windows use `env\Scripts\activate`
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Run the FastAPI server

```bash
uvicorn main:app --reload
```

### 5. Open the FastAPI docs

Open your browser and navigate to `http://127.0.0.1:8000/docs` to access the API documentation.

## Requirements

- FastAPI
- yt-dlp
- YTMusicAPI
- Pillow
- httpx
- pydantic

## Configuration

- Ensure you have an OAuth file for YouTube Music API (`oauth.json`), which you can generate through `ytmusicapi`.
- This project uses yt-dlp for extracting audio URLs.

## License

MIT License
