import os
import uuid
import shutil
import asyncio
import time
import re
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import yt_dlp
from pydub import AudioSegment
import google.generativeai as genai

app = FastAPI(title="YouTube Audio Downloader & Transcriber API", description="Standalone API for downloading and transcribing audio from YouTube")

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

# دالة لضبط التوقيتات برمجياً (رياضياً)
def adjust_timestamps(text: str, offset_minutes: int) -> str:
    if offset_minutes == 0:
        return text

    offset_seconds = offset_minutes * 60

    def shift_time(time_str):
        try:
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
        except Exception:
            return time_str

    # Match format [MM:SS -> MM:SS] or similar
    def repl_func(m_obj):
        start = shift_time(m_obj.group(1))
        end = shift_time(m_obj.group(2))
        return f"[{start} -> {end}]"

    pattern = r'\[\s*(\d{1,2}:\d{2}(?::\d{2})?)\s*->\s*(\d{1,2}:\d{2}(?::\d{2})?)\s*\]'
    return re.sub(pattern, repl_func, text)

# دالة لاستخراج ID الفيديو من الرابط
def extract_video_id(url: str) -> str:
    pattern = r'(?:https?:\/\/)?(?:www\.)?(?:youtube\.com\/(?:[^\/\n\s]+\/\S+\/|(?:v|e(?:mbed)?)\/|\S*?[?&]v=)|youtu\.be\/)([a-zA-Z0-9_-]{11})'
    match = re.search(pattern, url)
    return match.group(1) if match else "temp"

@app.get("/")
def read_root():
    return {"status": "running", "service": "YouTube Audio Downloader & Transcriber"}

@app.post("/api/transcribe-gemini")
async def transcribe_gemini(req: DownloadRequest, background_tasks: BackgroundTasks):
    task_id = str(uuid.uuid4())
    task_dir = os.path.join(PUBLIC_DIR, f"temp_{task_id}")
    os.makedirs(task_dir, exist_ok=True)
    
    video_id = extract_video_id(req.youtubeUrl)
    
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
        
        # Run downloader in separate thread to prevent blocking FastAPI's event loop
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: download_audio_executor(req.youtubeUrl, ydl_opts))
        
        audio_path = output_filename + ".mp3"
        if not os.path.exists(audio_path):
            raise Exception("Audio file was not created by yt-dlp postprocessor.")
            
        print(f"[{task_id}] Successfully downloaded YouTube audio: {audio_path}")
        
        has_api_key = req.geminiApiKey and req.geminiApiKey.strip() != "" and req.geminiApiKey.lower() != "none"
        full_transcription = ""
        transcription_url = None
        
        if has_api_key:
            print(f"[{task_id}] API key detected. Starting chunking & Gemini transcription...")
            # Configure GenAI
            genai.configure(api_key=req.geminiApiKey.strip())
            
            # Load and chunk audio
            audio = AudioSegment.from_file(audio_path)
            chunk_minutes = 7
            chunk_length_ms = chunk_minutes * 60 * 1000
            chunks = [audio[i:i + chunk_length_ms] for i in range(0, len(audio), chunk_length_ms)]
            
            print(f"[{task_id}] Split audio into {len(chunks)} chunks.")
            
            selected_model = "gemini-3.1-flash-lite"
            
            for idx, chunk in enumerate(chunks):
                start_minute = idx * chunk_minutes
                chunk_filename = os.path.join(task_dir, f"chunk_{video_id}_{idx}.mp3")
                chunk.export(chunk_filename, format="mp3", bitrate="64k")
                
                uploaded_file = None
                try:
                    uploaded_file = genai.upload_file(path=chunk_filename)
                    
                    while uploaded_file.state.name == "PROCESSING":
                        await asyncio.sleep(3)
                        uploaded_file = genai.get_file(uploaded_file.name)
                        
                    if uploaded_file.state.name == "FAILED":
                        print(f"[{task_id}] Chunk {idx+1} processing failed on Gemini.")
                        continue
                        
                    model = genai.GenerativeModel(selected_model)
                    prompt = (
                        "اسمع الملف الصوتي المرفق بتركيز. "
                        "قم بتفريغ المحتوى كاملاً باللغة العربية مع توقيتات دقيقة تبدأ من [00:00]. "
                        "لا تلخص واكتب كل ما تسمعه.\n\n"
                        "تنسيق المخرجات المطلوب حصراً:\n[00:05 -> 00:10] النص العربي هنا"
                    )
                    
                    response = await loop.run_in_executor(None, lambda: model.generate_content([prompt, uploaded_file]))
                    
                    adjusted_text = adjust_timestamps(response.text, start_minute)
                    full_transcription += "\n" + adjusted_text
                    
                except Exception as chunk_err:
                    print(f"[{task_id}] Error transcribing chunk {idx+1}: {chunk_err}")
                    full_transcription += f"\n[خطأ في معالجة الجزء {idx+1}]"
                finally:
                    if uploaded_file:
                        try:
                            genai.delete_file(uploaded_file.name)
                        except Exception as e:
                            print(f"[{task_id}] Failed to delete remote file: {e}")
                    if os.path.exists(chunk_filename):
                        try:
                            os.remove(chunk_filename)
                        except Exception as e:
                            print(f"[{task_id}] Failed to remove local chunk file: {e}")
            
            # Write full transcription to file
            transcription_path = os.path.join(task_dir, "transcription.txt")
            with open(transcription_path, "w", encoding="utf-8") as f:
                f.write(full_transcription.strip())
                
            transcription_url = f"public/temp_{task_id}/transcription.txt"
            print(f"[{task_id}] Transcription complete and saved.")
            
        # Schedule cleanup in the background after 10 minutes to save disk space
        background_tasks.add_task(schedule_dir_cleanup, task_dir, 600)
        
        response_payload = {
            "status": "success",
            "audioUrl": f"public/temp_{task_id}/audio.mp3"
        }
        if has_api_key:
            response_payload["transcription"] = full_transcription.strip()
            response_payload["transcriptionUrl"] = transcription_url
            
        return response_payload
        
    except Exception as e:
        clean_temp_dir(task_dir)
        print(f"[{task_id}] Failed to process request: {e}")
        raise HTTPException(status_code=500, detail=str(e))
