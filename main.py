import os
import uuid
import shutil
import asyncio
import time
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import yt_dlp

app = FastAPI(title="YouTube Audio Downloader API", description="Standalone API for downloading audio from YouTube")

@app.on_event("startup")
def startup_event():
    print(f"Startup: cookies.txt exists = {os.path.exists('cookies.txt')}", flush=True)

# Enable CORS for all origins so that Netlify/React frontends can consume the API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Ensure public directory exists
PUBLIC_DIR = os.path.abspath("public")
os.makedirs(PUBLIC_DIR, exist_ok=True)

# Mount public folder to serve downloaded audio files statically
app.mount("/public", StaticFiles(directory=PUBLIC_DIR), name="public")

class DownloadRequest(BaseModel):
    youtubeUrl: str
    geminiApiKey: str | None = None

def download_audio_executor(youtube_url: str, opts: dict):
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([youtube_url])

def clean_temp_dir(path: str):
    """Clean up the temporary directory after some delay or on request"""
    if os.path.exists(path):
        try:
            shutil.rmtree(path)
            print(f"Cleaned up directory: {path}")
        except Exception as e:
            print(f"Error cleaning up {path}: {e}")

async def schedule_dir_cleanup(path: str, delay_seconds: int = 600):
    """Wait for some time then delete the temp folder (e.g. 10 minutes)"""
    await asyncio.sleep(delay_seconds)
    clean_temp_dir(path)

@app.get("/")
def read_root():
    exists = os.path.exists("cookies.txt")
    size = os.path.getsize("cookies.txt") if exists else 0
    return {
        "status": "running",
        "service": "YouTube Audio Downloader",
        "cookies_detected": exists,
        "cookies_size_bytes": size,
        "files_in_dir": os.listdir(".")
    }

@app.post("/api/transcribe-gemini")
async def transcribe_gemini(req: DownloadRequest, background_tasks: BackgroundTasks):
    task_id = str(uuid.uuid4())
    task_dir = os.path.join(PUBLIC_DIR, f"temp_{task_id}")
    os.makedirs(task_dir, exist_ok=True)
    
    try:
        output_filename = os.path.join(task_dir, "audio")
        
        # Exact yt_dlp options requested by user (no cookies, no extra proxy settings)
        ydl_opts = {
            "format": "bestaudio/best",
            "outtmpl": output_filename,
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "64",
            }],
            "quiet": True,
            "noprogress": True,
        }
        
        # Check if cookies.txt exists in the current directory (uploaded by user)
        cookie_file = "cookies.txt"
        if os.path.exists(cookie_file):
            ydl_opts["cookiefile"] = cookie_file
            print(f"[{task_id}] Using cookies.txt for yt-dlp authentication.", flush=True)
        
        # Run downloader in separate thread to prevent blocking FastAPI's event loop
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: download_audio_executor(req.youtubeUrl, ydl_opts))
        
        audio_path = output_filename + ".mp3"
        if not os.path.exists(audio_path):
            raise Exception("Audio file was not created by yt-dlp postprocessor.")
            
        print(f"[{task_id}] Successfully downloaded YouTube audio: {audio_path}", flush=True)
        
        # Schedule cleanup in the background after 10 minutes to save disk space
        background_tasks.add_task(schedule_dir_cleanup, task_dir, 600)
        
        return {
            "status": "success",
            "audioUrl": f"public/temp_{task_id}/audio.mp3"
        }
        
    except Exception as e:
        clean_temp_dir(task_dir)
        print(f"[{task_id}] Failed to download audio: {e}")
        raise HTTPException(status_code=500, detail=str(e))
