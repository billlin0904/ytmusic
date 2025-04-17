from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel
import httpx
import os
from PIL import Image
from io import BytesIO
import yt_dlp
import base64
from ytmusicapi import YTMusic, OAuthCredentials
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.middleware.gzip import GZipMiddleware
from models import *
from databases import Database
import sqlalchemy
from urllib.parse import urlparse, parse_qs
import time

# 定義數據庫 URL
DATABASE_URL = "sqlite:///./cache.db"
HOST_ADDR = "127.0.0.1:8090"
PO_TOKEN_VALUE = os.getenv("PO_TOKEN_VALUE")

# 定義 lifespan 事件處理器
async def lifespan(app: FastAPI):
    await database.connect()
    yield
    await database.disconnect()
    
app = FastAPI(lifespan = lifespan)
app.add_middleware(GZipMiddleware, minimum_size=1000)

# 初始化 YTMusic API
ytmusic = YTMusic("browser.json")
# ytmusic = YTMusic()

# 創建數據庫實例
database = Database(DATABASE_URL)

# 定義元數據
metadata = sqlalchemy.MetaData()

# 定義快取表
cache_table = sqlalchemy.Table(
    "cache",
    metadata,
    sqlalchemy.Column("video_id", sqlalchemy.String, primary_key=True),
    sqlalchemy.Column("download_url", sqlalchemy.String),
    sqlalchemy.Column("thumbnail_base64", sqlalchemy.String),
    sqlalchemy.Column("expire", sqlalchemy.Integer),  # Unix 时间戳
    sqlalchemy.Column("lyrics", sqlalchemy.Text),  # 新增 lyrics 欄位來存放歌詞
)

DOWNLOAD_FOLDER = "downloaded_songs"
if not os.path.exists(DOWNLOAD_FOLDER):
    os.makedirs(DOWNLOAD_FOLDER)
    
# 創建數據庫引擎
engine = sqlalchemy.create_engine(
    DATABASE_URL, connect_args={"check_same_thread": False}
)

# 創建表
metadata.create_all(engine)

def make_ytmusic_url(video_id):
    return f"https://music.youtube.com/watch?v={video_id}"

@app.post("/fetch_song")
async def fetch_song(request: SongRequest):
    """
    與 /fetch_song_info 類似，但下載最佳音質檔案並轉成 .m4a 存到本地端。
    回傳的 download_url 是本地端可直接下載的連結，例如：
      http://127.0.0.1:8090/download_song/{video_id}.m4a
    """
    video_id = request.video_id

    # --------------------------
    # 1) 先檢查快取資料(可自行複用 /fetch_song_info 的邏輯)
    # --------------------------
    query = cache_table.select().where(cache_table.c.video_id == video_id)
    cached_result = await database.fetch_one(query)

    # 若快取已存在且未過期（檢查 expire 時間），就直接用
    if cached_result and cached_result["expire"] > int(time.time()):
        # 同時也檢查本地是否已經有下載好的檔案
        file_path = os.path.join(DOWNLOAD_FOLDER, f"{video_id}.opus")
        if os.path.exists(file_path):
            return {
                "download_url": f"http://127.0.0.1:8090/download_song/{video_id}.opus",
                "thumbnail_base64": cached_result["thumbnail_base64"],
                "lyrics": cached_result["lyrics"]
            }
        else:
            print("快取資訊存在，但檔案不在本地端，需重新下載。")

    # --------------------------
    # 2) 抓取歌曲資訊 (與原本的 fetch_song_info 類似)
    # --------------------------
    # 先組成可下載的 YouTube Music URL
    def make_ytmusic_url(vid):
        return f"https://music.youtube.com/watch?v={vid}"

    ytmusic_url = make_ytmusic_url(video_id)
    
    # 透過 yt_dlp 抓取格式資訊
    ydl_extract_opts = {
        #'cookiesfrombrowser': ('firefox', None, None, None),
        'extractor_args': {
            'youtube': {
                'po_token': [PO_TOKEN_VALUE]
            }
        },
        'cookiefile': 'cookies.txt',
        'format': 'bestaudio/best',
        'noplaylist': True,
    }

    with yt_dlp.YoutubeDL(ydl_extract_opts) as ydl:
        info = ydl.extract_info(ytmusic_url, download=False)

    # 從 formats 中找最佳音訊
    best_format = None
    for f in info['formats']:
        if f.get('vcodec') == 'none' and 'acodec' in f and 'mp4' in f['acodec']:
            # 這裡簡單取碼率最高即可
            if (best_format is None) or (f.get('abr', 0) > best_format.get('abr', 0)):
                best_format = f

    if not best_format:
        raise HTTPException(status_code=404, detail="No suitable audio format found.")

    # 取得下載連結及到期時間
    download_url = best_format["url"]
    parsed_url = urlparse(download_url)
    query_params = parse_qs(parsed_url.query)
    expire_param = query_params.get('expire', [None])[0]
    if expire_param is not None:
        expire_timestamp = int(expire_param)
    else:
        # 如果沒有 expire 參數，就預設 1 小時後
        expire_timestamp = int(time.time()) + 3600

    # 抓取歌曲詳細資訊(縮圖、歌詞)
    song_info = ytmusic.get_song(video_id)
    if not song_info or 'videoDetails' not in song_info:
        raise HTTPException(status_code=404, detail="Song information not found.")

    thumbnails = song_info['videoDetails'].get('thumbnail', {}).get('thumbnails', [])
    if not thumbnails:
        raise HTTPException(status_code=404, detail="Song thumbnail not found.")

    thumbnail_url = thumbnails[-1]["url"]

    # 抓取歌詞 (若有 lyrics_id)
    watch_playlist = ytmusic.get_watch_playlist(video_id)
    lyrics = ""
    if "lyrics" in watch_playlist and watch_playlist["lyrics"]:
        lyrics_id = watch_playlist["lyrics"]
        lyrics_data = ytmusic.get_lyrics(lyrics_id)
        lyrics = lyrics_data.get("lyrics", "Lyrics not available")

    # 下載縮圖並轉成 base64
    async with httpx.AsyncClient() as client:
        response = await client.get(thumbnail_url)
        if response.status_code != 200:
            raise HTTPException(status_code=500, detail="Failed to download thumbnail.")
        image = Image.open(BytesIO(response.content))
        resized_image = image.resize((200, 200))
        buffered = BytesIO()
        resized_image.save(buffered, format="JPEG")
        base64_image = base64.b64encode(buffered.getvalue()).decode("utf-8")

    # --------------------------
    # 3) 下載音訊檔並轉成 .m4a 檔
    # --------------------------
    file_path = os.path.join(DOWNLOAD_FOLDER, f"{video_id}.opus")

    # 若本地已有同名檔案，可以視需求判斷是否要覆蓋或跳過下載
    if not os.path.exists(file_path):
    #if True:
        ydl_opts = {
            'extractor_args': {
                'youtube': {
                    'po_token': [PO_TOKEN_VALUE]
                }
            },
            'cookiefile': 'cookies.txt',          
            # 將檔案直接輸出到 file_path
            'outtmpl': file_path.replace('.opus', '.%(ext)s'),
            'format': 'bestaudio/best',
            'postprocessors': [
                {
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'best' # 最高音質就是opus格式
                }
            ]
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([ytmusic_url])

        # 下載完成後，您就會在 downloaded_songs/ 下看到 {video_id}.opus

    # --------------------------
    # 4) 寫入/更新快取
    # --------------------------
    insert_query = cache_table.insert().prefix_with('OR REPLACE').values(
        video_id=video_id,
        download_url=download_url,
        thumbnail_base64=base64_image,
        expire=expire_timestamp,
        lyrics=lyrics
    )
    await database.execute(insert_query)

    # --------------------------
    # 5) 回傳結果
    # --------------------------
    return {
        "download_url": f"http://{HOST_ADDR}/download_song/{video_id}.opus",
        "thumbnail_base64": base64_image,
        "lyrics": lyrics
    }

@app.get("/download_song/{video_id}.opus")
async def download_song(video_id: str):
    file_path = os.path.join(DOWNLOAD_FOLDER, f"{video_id}.opus")
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File not found.")

    return FileResponse(
        path=file_path,
        media_type="audio/opus",
        filename=f"{video_id}.opus"
    )
    
@app.post("/fetch_playlist")
async def fetch_playlist(request: PlaylistRequest):
    playlist_id = request.playlist_id
    
    try:
        # 使用 get_playlist 獲取播放清單的詳細資訊
        playlist_details = ytmusic.get_playlist(playlist_id, 255)

        # 提取 playlist 的標題、ID 和曲目資訊
        title = playlist_details.get("title", "Unknown Title")
        tracks = playlist_details.get("tracks", [])

        # 直接返回 tracks 不進行處理
        return {
            "title": title,
            "playlistId": playlist_id,
            "tracks": tracks  # 直接返回完整的 tracks 資訊
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/fetch_library_playlists")
async def fetch_library_playlists():
    try:
        # 取得使用者的播放清單
        playlists = ytmusic.get_library_playlists()

        library_playlists = []
        for playlist in playlists:
            playlist_id = playlist["playlistId"]
            playlist_title = playlist["title"]

            # 取得每個播放清單的詳細資訊，包括 tracks
            playlist_details = ytmusic.get_playlist(playlist_id, limit=10)  # 可以根據需要調整 limit

            # 提取 tracks 的完整資訊
            tracks_info = playlist_details["tracks"]

            # 添加到結果中
            library_playlists.append({
                "title": playlist_title,
                "playlistId": playlist_id,
                "tracks": tracks_info  # 返回所有 track 的資訊
            })

        return library_playlists

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    
@app.post("/fetch_lyrics")
async def fetch_lyrics(request: SongRequest):
    video_id = request.video_id
    try:
        # 使用 get_watch_playlist 獲取播放清單
        watch_playlist = ytmusic.get_watch_playlist(video_id)
        
        # 檢查是否有 lyrics，通常會在 "lyrics" 字段
        if "lyrics" not in watch_playlist or not watch_playlist["lyrics"]:
            raise HTTPException(status_code=404, detail="No lyrics available for this video")

        # 獲取 lyrics 的 ID
        lyrics_id = watch_playlist["lyrics"]
        
        # 使用 get_lyrics 取得歌詞
        lyrics_data = ytmusic.get_lyrics(lyrics_id)
        
        # 提取歌詞
        lyrics = lyrics_data.get("lyrics", "Lyrics not available")

        return {"video_id": video_id, "lyrics": lyrics}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    
@app.post("/edit_playlist")
async def edit_playlist(request: EditPlaylistRequest):
    playlist_id = request.playlist_id
    new_title = request.new_title
    try:
        # 使用 edit_playlist 方法更改播放清單的名稱
        response = ytmusic.edit_playlist(playlist_id, title=new_title)

        # 檢查回應，確認是否更新成功
        if response is None:
            raise HTTPException(status_code=500, detail="Failed to edit playlist title.")

        return {
            "message": f"Playlist title updated successfully to {new_title}",
            "playlistId": playlist_id
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    
@app.post("/delete_playlist")
async def delete_playlist(request: DeletePlaylistRequest):
    playlist_id = request.playlist_id
    try:
        # 使用 delete_playlist 刪除指定播放清單
        response = ytmusic.delete_playlist(playlist_id)

        # 檢查回應，確認是否刪除成功
        if response is None:
            raise HTTPException(status_code=500, detail="Failed to delete playlist.")

        return {
            "message": f"Playlist with ID {playlist_id} deleted successfully."
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    
    
@app.post("/create_playlist")
async def create_playlist(request: CreatePlaylistRequest):
    try:
        # 使用 create_playlist 方法建立新的播放清單
        response = ytmusic.create_playlist(
            title=request.title,
            description=request.description,
            privacy_status="PRIVATE" if request.private else "PUBLIC",
            video_ids=request.video_ids,
            source_playlist=request.source_playlist
        )

        # 檢查回應，確認是否建立成功
        if not response:
            raise HTTPException(status_code=500, detail="Failed to create playlist.")

        return {
            "message": "Playlist created successfully.",
            "playlistId": response
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    
@app.post("/search_suggestions")
async def search_suggestions(request: SearchSuggestionsRequest):
    try:
        # 使用 get_search_suggestions 獲取搜索建議
        suggestions = ytmusic.get_search_suggestions(request.query)

        # 檢查是否有返回結果
        if not suggestions:
            raise HTTPException(status_code=404, detail="No search suggestions found.")

        return {
            "query": request.query,
            "suggestions": suggestions
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    
@app.post("/search_album")
async def search_album(request: SearchAlbumRequest):
    try:
        # 使用 search 並將 filter 設置為 albums 來搜索專輯
        search_results = ytmusic.search(request.query, filter="albums")

        # 檢查是否有返回結果
        if not search_results:
            raise HTTPException(status_code=404, detail="No albums found.")

        return {
            "query": request.query,
            "albums": search_results
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))    

@app.post("/get_album")
async def get_album(request: GetAlbumRequest):
    try:
        # 使用 get_album 獲取專輯的詳細資訊
        album_details = ytmusic.get_album(request.browse_id)

        # 檢查是否有返回結果
        if not album_details:
            raise HTTPException(status_code=404, detail="No album details found.")

        return album_details

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    
@app.post("/remove_cache")
async def remove_cache(request: RemoveCacheRequest):
    video_id = request.video_id

    # 檢查是否存在
    query = cache_table.select().where(cache_table.c.video_id == video_id)
    cached_result = await database.fetch_one(query)

    if not cached_result:
        return {"message": f"No cache found for video_id {video_id}."}

    # 刪除資料庫紀錄
    delete_query = cache_table.delete().where(cache_table.c.video_id == video_id)
    await database.execute(delete_query)

    # 刪除本地實體檔案
    file_path = os.path.join(DOWNLOAD_FOLDER, f"{video_id}.m4a")
    if os.path.exists(file_path):
        os.remove(file_path)

    return {"message": f"Cache and file for video_id {video_id} removed successfully."}

@app.post("/remove_cache_all")
async def remove_cache_all():
    # 刪除所有資料庫快取
    delete_query = cache_table.delete()
    await database.execute(delete_query)

    # 刪除所有下載的 .m4a 檔案
    deleted_files = []
    for file_name in os.listdir(DOWNLOAD_FOLDER):
        if file_name.endswith(".opus"):
            file_path = os.path.join(DOWNLOAD_FOLDER, file_name)
            os.remove(file_path)
            deleted_files.append(file_name)

    return {
        "message": "All cache records and downloaded audio files removed.",
        "files_deleted": deleted_files
    }