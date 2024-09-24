from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import httpx
from PIL import Image
from io import BytesIO
import yt_dlp
import base64
from ytmusicapi import YTMusic
from fastapi.middleware.gzip import GZipMiddleware
from models import *
from databases import Database
import sqlalchemy
from urllib.parse import urlparse, parse_qs
import time

# 定義 lifespan 事件處理器
async def lifespan(app: FastAPI):
    await database.connect()
    yield
    await database.disconnect()
    
app = FastAPI(lifespan = lifespan)
app.add_middleware(GZipMiddleware, minimum_size=1000)

# 初始化 YTMusic API
ytmusic = YTMusic("oauth.json")

# 定義數據庫 URL
DATABASE_URL = "sqlite:///./cache.db"

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
)

# 創建數據庫引擎
engine = sqlalchemy.create_engine(
    DATABASE_URL, connect_args={"check_same_thread": False}
)

# 創建表
metadata.create_all(engine)

def make_ytmusic_url(video_id):
    return f"https://music.youtube.com/watch?v={video_id}"

def find_best_audio_format(formats):
    # 定義最佳音訊編碼
    kBestAudioCodec = "mp4"

    # 過濾出符合條件的格式：沒有視頻編碼，音訊編碼存在且包含指定的編碼
    valid_formats = [format for format in formats if format['vcodec'] == 'none' and 'acodec' in format and kBestAudioCodec in format['acodec']]

    # 如果沒有找到符合條件的格式，回傳 None
    if not valid_formats:
        return None

    # 按照音訊比特率排序，並返回比特率最高的格式
    best_format = sorted(valid_formats, key=lambda f: f['abr'], reverse=True)[0]
    print(f"{best_format}")
    return best_format

def extract_video_info(video_id):
    ydl_opts = {
        'cookiesfrombrowser': ('firefox', None, None, None),
        'format': 'bestaudio/best',  # 取得最好的音訊格式
        'noplaylist': True,          # 不要下載播放清單中的其他內容
        'extract_flat': False,       # 完整提取格式資訊
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(video_id, download=False)

    # 找到最佳音訊格式
    best_format = find_best_audio_format(info['formats'])
    if best_format is None:
        raise HTTPException(status_code=404, detail="No suitable audio format found.")
    
    return best_format

async def fetch_song_info_from_api(video_id):
    # 使用 ytmusicapi 取得歌曲詳細資訊
    song_info = ytmusic.get_song(video_id)
    if not song_info or 'videoDetails' not in song_info:
        raise HTTPException(status_code=404, detail="Song information not found.")
    
    thumbnails = song_info['videoDetails'].get('thumbnail', {}).get('thumbnails', [])
    if not thumbnails:
        raise HTTPException(status_code=404, detail="Song thumbnail not found.")
    
    return thumbnails

@app.post("/fetch_song_info")
async def fetch_song_info_endpoint(request: SongRequest):
    video_id = request.video_id

    # 检查是否存在于缓存中
    query = cache_table.select().where(cache_table.c.video_id == video_id)
    cached_result = await database.fetch_one(query)

    if cached_result:
        # 检查下载链接是否过期
        expire_timestamp = cached_result["expire"]
        current_timestamp = int(time.time())
        if expire_timestamp > current_timestamp:
            # 链接仍然有效，返回缓存结果
            return {
                "download_url": cached_result["download_url"],
                "thumbnail_base64": cached_result["thumbnail_base64"]
            }
        else:
            print("Cached URL has expired, fetching new URL...")

    ytmusic_url = make_ytmusic_url(video_id)
    print("Extracting video information...")

    # 使用 yt_dlp 获取视频信息并找出最佳音频格式
    best_format = extract_video_info(ytmusic_url)
    download_url = best_format["url"]
    print(f"Download URL: {download_url}")

    # 从 download_url 中提取 'expire' 参数
    parsed_url = urlparse(download_url)
    query_params = parse_qs(parsed_url.query)
    expire_param = query_params.get('expire', [None])[0]
    if expire_param is not None:
        expire_timestamp = int(expire_param)
    else:
        # 如果没有 'expire' 参数，设置一个默认的过期时间（例如1小时后）
        expire_timestamp = int(time.time()) + 3600  # 1小时后过期

    # 使用 ytmusicapi 获取歌曲信息
    thumbnails = await fetch_song_info_from_api(video_id)
    thumbnail_url = thumbnails[-1]["url"]
    print(f"Extracting song thumbnail from: {thumbnail_url}")

    # 下载并处理图片
    async with httpx.AsyncClient() as client:
        response = await client.get(thumbnail_url)
        if response.status_code != 200:
            raise HTTPException(status_code=500, detail="Failed to download song thumbnail.")
        
        image = Image.open(BytesIO(response.content))
        resized_image = image.resize((200, 200))  # 假设缩放到 200x200

        # 将图片编码为 Base64
        buffered = BytesIO()
        resized_image.save(buffered, format="JPEG")
        base64_image = base64.b64encode(buffered.getvalue()).decode("utf-8")

    # 将结果存入缓存，使用 OR REPLACE 进行更新或插入
    query = cache_table.insert().prefix_with('OR REPLACE').values(
        video_id=video_id,
        download_url=download_url,
        thumbnail_base64=base64_image,
        expire=expire_timestamp
    )
    await database.execute(query)

    return {
        "download_url": download_url,
        "thumbnail_base64": base64_image
    }
    
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