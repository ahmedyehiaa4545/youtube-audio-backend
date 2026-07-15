import os
import re
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
import google.generativeai as genai
from pydub import AudioSegment

app = FastAPI(title="YouTube Audio Downloader API", description="Standalone API for downloading and transcribing audio from YouTube using Gemini + Deno + Cookies + yt-dlp")

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

def download_audio_executor(youtube_url: str, opts: dict):
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([youtube_url])

# دالة لضبط التوقيتات برمجياً (رياضياً)
def adjust_timestamps(text: str, offset_minutes: int) -> str:
    if offset_minutes == 0:
        return text

    offset_seconds = offset_minutes * 60

    def shift_time(time_str):
        parts = list(map(int, time_str.split(':')))
        if len(parts) == 2: # MM:SS
            total_sec = parts[0] * 60 + parts[1] + offset_seconds
        elif len(parts) == 3: # HH:MM:SS
            total_sec = parts[0] * 3600 + parts[1] * 60 + parts[2] + offset_seconds
        else:
            return time_str

        h = total_sec // 3600
        m = (total_sec % 3600) // 60
        s = total_sec % 60

        if h > 0:
            return f"{h:02d}:{m:02d}:{s:02d}"
        else:
            return f"{m:02d}:{s:02d}"

    def repl(match):
        start = shift_time(match.group(1))
        end = shift_time(match.group(2))
        return f"[{start} -> {end}]"

    pattern = r'\[\s*(\d{1,2}:\d{2}(?::\d{2})?)\s*->\s*(\d{1,2}:\d{2}(?::\d{2})?)\s*\]'
    return re.sub(pattern, repl, text)

def transcribe_audio_with_gemini(audio_path: str, api_key: str, chunk_minutes: int = 7) -> str:
    genai.configure(api_key=api_key)
    # Use the model requested by the user
    selected_model = "gemini-3.1-flash-lite"
    full_transcription = ""

    print(f"🟢 النموذج المستخدم: {selected_model}", flush=True)

    # Load audio file using pydub
    audio = AudioSegment.from_mp3(audio_path)
    chunk_length_ms = chunk_minutes * 60 * 1000
    
    # Split audio into chunks
    chunks = []
    for i in range(0, len(audio), chunk_length_ms):
        chunks.append(audio[i:i + chunk_length_ms])

    print(f"[+] تم تقسيم الصوت إلى {len(chunks)} أجزاء.", flush=True)

    for idx, chunk in enumerate(chunks):
        print(f"\n[*] معالجة الجزء {idx + 1}/{len(chunks)}", flush=True)

        chunk_filename = f"chunk_{idx}_{uuid.uuid4().hex[:6]}.mp3"
        chunk_path = os.path.join(os.path.dirname(audio_path), chunk_filename)
        chunk.export(chunk_path, format="mp3")

        uploaded_file = None

        try:
            print("   - جاري رفع الملف...", flush=True)
            uploaded_file = genai.upload_file(path=chunk_path)

            while uploaded_file.state.name == "PROCESSING":
                time.sleep(3)
                uploaded_file = genai.get_file(uploaded_file.name)

            if uploaded_file.state.name == "FAILED":
                print("❌ فشل رفع الملف.", flush=True)
                continue

            print("   - جاري إرسال الطلب إلى Gemini...", flush=True)

            model = genai.GenerativeModel(selected_model)

            prompt = (
                "اسمع الملف الصوتي المرفق بتركيز. "
                "قم بتفريغ المحتوى كاملاً باللغة العربية مع توقيتات دقيقة تبدأ من [00:00]. "
                "لا تلخص واكتب كل ما تسمعه.\n\n"
                "تنسيق المخرجات المطلوب حصراً:\n[00:05 -> 00:10] النص العربي هنا"
            )

            response = model.generate_content([prompt, uploaded_file])

            print("✅ تم تفريغ الجزء.", flush=True)

            # تعديل التوقيتات برمجياً
            adjusted_text = adjust_timestamps(response.text, idx * chunk_minutes)
            full_transcription += "\n" + adjusted_text

        except Exception as e:
            print(f"❌ خطأ في تفريغ الجزء {idx + 1}: {e}", flush=True)
            raise e

        finally:
            if uploaded_file:
                try:
                    genai.delete_file(uploaded_file.name)
                except:
                    pass

            if os.path.exists(chunk_path):
                try:
                    os.remove(chunk_path)
                except:
                    pass

    return full_transcription.strip()

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
        
        # Configure yt_dlp options to download and extract audio directly
        ydl_opts = {
            'format': 'bestaudio/best',
            'outtmpl': output_filename,
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '64',
            }],
            'quiet': False,
            'no_warnings': False,
            'verbose': True,
            'source_address': '0.0.0.0',
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36',
            }
        }
        
        # Inject cookie file path if it exists
        if os.path.exists(COOKIE_FILE_PATH) and os.path.getsize(COOKIE_FILE_PATH) > 0:
            ydl_opts["cookiefile"] = COOKIE_FILE_PATH
            print(f"[{task_id}] Using cookies from {COOKIE_FILE_PATH} for yt-dlp authentication.", flush=True)
            
        print(f"[{task_id}] Downloading and transcoding YouTube audio via yt-dlp...", flush=True)
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: download_audio_executor(req.youtubeUrl, ydl_opts))
        
        audio_path = output_filename + ".mp3"
        if not os.path.exists(audio_path):
            raise Exception("Audio file was not created by yt-dlp postprocessor.")
            
        print(f"[{task_id}] Successfully downloaded and transcoded audio: {audio_path}", flush=True)
        
        # Verify Gemini API Key exists
        if not req.geminiApiKey or req.geminiApiKey.strip() in ["", "none", "null"]:
            raise Exception("Gemini API key is missing or invalid.")
            
        print(f"[{task_id}] Transcribing audio with Gemini...", flush=True)
        # Call the transcription function
        loop = asyncio.get_event_loop()
        transcription_text = await loop.run_in_executor(
            None, 
            lambda: transcribe_audio_with_gemini(audio_path, req.geminiApiKey)
        )
        
        # Schedule cleanup in the background after 10 minutes to save disk space
        background_tasks.add_task(schedule_dir_cleanup, task_dir, 600)
        
        return {
            "status": "success",
            "audioUrl": f"public/temp_{task_id}/audio.mp3",
            "transcription": transcription_text
        }
        
    except Exception as e:
        clean_temp_dir(task_dir)
        print(f"[{task_id}] Failed to process audio: {e}", flush=True)
        raise HTTPException(status_code=500, detail=str(e))
