from pydantic import BaseModel
from typing import List, Optional

class SongRequest(BaseModel):
    video_id: str
    
class PlaylistRequest(BaseModel):
    playlist_id: str

class EditPlaylistRequest(BaseModel):
    playlist_id: str
    new_title: str

class DeletePlaylistRequest(BaseModel):
    playlist_id: str
    
class CreatePlaylistRequest(BaseModel):
    title: str
    description: Optional[str] = None
    private: bool = True
    video_ids: Optional[List[str]] = []
    source_playlist: Optional[str] = None
    
class SearchSuggestionsRequest(BaseModel):
    query: str
    
class SearchAlbumRequest(BaseModel):
    query: str
    
class GetAlbumRequest(BaseModel):
    browse_id: str