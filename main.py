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

app = FastAPI(title="YouTube Audio Downloader API", description="Standalone API for downloading audio from YouTube using Deno + Cookies + FFmpeg")

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

COOKIE_FILE_PATH = "/tmp/cookies.txt"

def init_cookies():
    """Write cookies from env variable or copy local cookies.txt to /tmp"""
    cookies_env = os.environ.get("YOUTUBE_COOKIES")
    if cookies_env:
        try:
            with open(COOKIE_FILE_PATH, "w", encoding="utf-8") as f:
                f.write(cookies_env.strip())
            print("🔑 cookies.txt written from YOUTUBE_COOKIES env variable.", flush=True)
        except Exception as e:
            print(f"⚠️ Failed to write cookies from env: {e}", flush=True)
    else:
        # Fallback to local cookies.txt in project root
        if os.path.exists("cookies.txt"):
            try:
                shutil.copy("cookies.txt", COOKIE_FILE_PATH)
                print("🔑 cookies.txt copied from project root to /tmp.", flush=True)
            except Exception as e:
                print(f"⚠️ Failed to copy local cookies.txt: {e}", flush=True)
        else:
            print("⚠️ Warning: No cookies.txt found in project root or YOUTUBE_COOKIES env variable!", flush=True)

init_cookies()

class DownloadRequest(BaseModel):
    youtubeUrl: str
    geminiApiKey: str | None = None

def extract_info_executor(youtube_url: str, opts: dict):
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(youtube_url, download=False)

def clean_temp_dir(path: str):
    """Clean up the temporary directory after some delay or on request"""
    if os.path.exists(path):
        try:
            shutil.rmtree(path)
            print(f"Cleaned up directory: {path}", flush=True)
        except Exception as e:
            print(f"Error cleaning up {path}: {e}", flush=True)

async def schedule_dir_cleanup(path: str, delay_seconds: int = 600):
    """Wait for some time then delete the temp folder (e.g. 10 minutes)"""
    await asyncio.sleep(delay_seconds)
    clean_temp_dir(path)

@app.get("/")
def read_root():
    exists = os.path.exists(COOKIE_FILE_PATH)
    size = os.path.getsize(COOKIE_FILE_PATH) if exists else 0
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
        
        # Configure yt_dlp options identical to the working method5
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'source_address': '0.0.0.0',
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36',
            }
        }
        
        # Inject cookie file path if it exists
        if os.path.exists(COOKIE_FILE_PATH) and os.path.getsize(COOKIE_FILE_PATH) > 0:
            ydl_opts["cookiefile"] = COOKIE_FILE_PATH
            print(f"[{task_id}] Using cookies from {COOKIE_FILE_PATH} for yt-dlp authentication.", flush=True)
        
        # Extract stream URL
        print(f"[{task_id}] Extracting stream URL for {req.youtubeUrl}...", flush=True)
        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(None, lambda: extract_info_executor(req.youtubeUrl, ydl_opts))
        
        # Select best audio stream
        audio_formats = [
            f for f in info.get('formats', [])
            if f.get('acodec') != 'none' and f.get('vcodec') == 'none'
        ]
        audio_formats.sort(key=lambda x: x.get('abr', 0), reverse=True)
        
        if not audio_formats:
            # Fallback to any format with audio
            audio_formats = [
                f for f in info.get('formats', [])
                if f.get('acodec') != 'none'
            ]
            
        if not audio_formats:
            raise Exception("No suitable audio stream found")
            
        audio_url = audio_formats[0]['url']
        audio_path = output_filename + ".mp3"
        
        # Download and transcode direct stream URL to 64k mp3 using FFmpeg
        print(f"[{task_id}] Downloading and transcoding stream to MP3...", flush=True)
        ffmpeg_cmd = [
            'ffmpeg', '-y',
            '-i', audio_url,
            '-vn',
            '-c:a', 'libmp3lame',
            '-b:a', '64k',
            audio_path
        ]
        
        process = await asyncio.create_subprocess_exec(
            *ffmpeg_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        
        if process.returncode != 0:
            raise Exception(f"FFmpeg transcoding failed: {stderr.decode('utf-8', errors='ignore')}")
            
        if not os.path.exists(audio_path):
            raise Exception("Audio file was not created by FFmpeg.")
            
        print(f"[{task_id}] Successfully downloaded and transcoded audio: {audio_path}", flush=True)
        
        # Schedule cleanup in the background after 10 minutes to save disk space
        background_tasks.add_task(schedule_dir_cleanup, task_dir, 600)
        
        return {
            "status": "success",
            "audioUrl": f"public/temp_{task_id}/audio.mp3"
        }
        
    except Exception as e:
        clean_temp_dir(task_dir)
        print(f"[{task_id}] Failed to process audio: {e}", flush=True)
        raise HTTPException(status_code=500, detail=str(e))
