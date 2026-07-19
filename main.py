import os
import re
import uuid
import shutil
import asyncio
import time
import requests
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from typing import List
import yt_dlp
import google.generativeai as genai
from pydub import AudioSegment
import subprocess

TASKS = {}

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

class ShortSuggestion(BaseModel):
    title: str = Field(description="عنوان جذاب ومثير للمقطع القصير")
    start_time: str = Field(description="توقيت بداية المقطع كما ورد في النص المفرغ تماماً (مثال: 05:47)")
    end_time: str = Field(description="توقيت نهاية المقطع كما ورد في النص المفرغ تماماً (مثال: 06:02)")
    script: str = Field(description="النص الكامل للمقطع القصير كما ورد في التفريغ")
    hook: str = Field(description="الجملة أو الفكرة الافتتاحية الجذابة (الخطاف) في أول 3 ثوانٍ")

class ShortsResponse(BaseModel):
    shorts: List[ShortSuggestion]

class SuggestShortsRequest(BaseModel):
    transcription: str
    geminiApiKey: str
    numShorts: int = 3

class CutRequest(BaseModel):
    url: str
    start_time: str
    end_time: str
    quality: int = 720
    convert_vertical: bool = False

def parse_time_to_seconds(time_str: str) -> float:
    """Convert HH:MM:SS or MM:SS or raw seconds to float seconds"""
    try:
        return float(time_str)
    except ValueError:
        pass

    parts = time_str.split(':')
    if len(parts) == 3:
        h, m, s = parts
        return float(h) * 3600 + float(m) * 60 + float(s)
    elif len(parts) == 2:
        m, s = parts
        return float(m) * 60 + float(s)
    else:
        raise ValueError(f"Invalid time format: {time_str}")

def get_max_transcription_seconds(transcription: str) -> float:
    """Scan transcription to find the maximum timestamp in it"""
    pattern = r'\[\s*(\d{1,2}:\d{2}(?::\d{2})?)\s*->\s*(\d{1,2}:\d{2}(?::\d{2})?)\s*\]'
    matches = re.findall(pattern, transcription)
    max_secs = 0.0
    for start, end in matches:
        try:
            max_secs = max(max_secs, parse_time_to_seconds(start), parse_time_to_seconds(end))
        except:
            pass
    return max_secs

def normalize_time_str(time_str: str, max_secs: float = 0.0) -> str:
    """Normalize any time format (HH:MM:SS, MM:SS, raw seconds) to standard HH:MM:SS format.
    If the parsed seconds exceed max_secs, corrects common AI mapping errors (e.g. HH:MM:00 -> 00:HH:MM).
    """
    try:
        # Detect and fix mapping error if max_secs is provided
        parts = time_str.split(':')
        if max_secs > 0 and len(parts) == 3:
            try:
                h, m, s = int(parts[0]), int(parts[1]), int(parts[2])
                parsed_secs = h * 3600 + m * 60 + s
                # If parsed duration is way too long, try shifting right
                if parsed_secs > max_secs * 1.2:
                    shifted_secs = h * 60 + m
                    if shifted_secs <= max_secs:
                        seconds = shifted_secs
                        h_new = int(seconds // 3600)
                        m_new = int((seconds % 3600) // 60)
                        s_new = int(seconds % 60)
                        return f"{h_new:02d}:{m_new:02d}:{s_new:02d}"
            except Exception:
                pass

        seconds = parse_time_to_seconds(time_str)
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = int(seconds % 60)
        return f"{h:02d}:{m:02d}:{s:02d}"
    except Exception:
        return time_str

def convert_single_timestamps_to_ranges(text: str) -> str:
    """
    Scans the transcription text line by line.
    If a line has a single timestamp [MM:SS] Text, it converts it to [MM:SS -> next_MM:SS] Text
    based on the start time of the next segment.
    """
    lines = text.split('\n')
    
    parsed_segments = []
    for line in lines:
        trimmed = line.strip()
        if not trimmed:
            parsed_segments.append({"type": "empty", "content": line})
            continue
            
        range_match = re.match(r'^\[\s*(\d{1,2}:\d{2}(?::\d{2})?)\s*->\s*(\d{1,2}:\d{2}(?::\d{2})?)\s*\]\s*(.*)$', trimmed)
        if range_match:
            parsed_segments.append({
                "type": "range",
                "start_str": range_match.group(1),
                "end_str": range_match.group(2),
                "text": range_match.group(3),
                "raw_line": line
            })
            continue
            
        single_match = re.match(r'^\[\s*(\d{1,2}:\d{2}(?::\d{2})?)\s*\]\s*(.*)$', trimmed)
        if single_match:
            parsed_segments.append({
                "type": "single",
                "start_str": single_match.group(1),
                "text": single_match.group(2),
                "raw_line": line
            })
            continue
            
        parsed_segments.append({"type": "text", "content": line})
        
    for idx, seg in enumerate(parsed_segments):
        if seg["type"] == "single":
            next_start_str = None
            for lookahead_idx in range(idx + 1, len(parsed_segments)):
                lookahead_seg = parsed_segments[lookahead_idx]
                if lookahead_seg["type"] in ["range", "single"]:
                    next_start_str = lookahead_seg["start_str"]
                    break
            
            if next_start_str:
                seg["type"] = "range"
                seg["end_str"] = next_start_str
            else:
                try:
                    start_sec = parse_time_to_seconds(seg["start_str"])
                    end_sec = start_sec + 4.0
                    h = int(end_sec // 3600)
                    m = int((end_sec % 3600) // 60)
                    s = int(end_sec % 60)
                    if h > 0:
                        seg["end_str"] = f"{h:02d}:{m:02d}:{s:02d}"
                    else:
                        seg["end_str"] = f"{m:02d}:{s:02d}"
                    seg["type"] = "range"
                except:
                    pass
                    
    rebuilt_lines = []
    for seg in parsed_segments:
        if seg["type"] == "empty":
            rebuilt_lines.append(seg["content"])
        elif seg["type"] == "text":
            rebuilt_lines.append(seg["content"])
        elif seg["type"] == "range":
            rebuilt_lines.append(f"[{seg['start_str']} -> {seg['end_str']}] {seg['text']}")
        elif seg["type"] == "single":
            rebuilt_lines.append(f"[{seg['start_str']}] {seg['text']}")
            
    return "\n".join(rebuilt_lines)

def parse_transcription_segments(transcription: str):
    """
    Parses transcription text into list of dicts:
    [{"start": float, "end": float, "text": str}]
    Supports multiple inline timestamps and text before/after them.
    """
    segments = []
    pattern = r'\[\s*(\d{1,2}:\d{2}(?::\d{2})?)(?:\s*->\s*(\d{1,2}:\d{2}(?::\d{2})?))?\s*\]'
    
    matches = list(re.finditer(pattern, transcription))
    
    if not matches:
        return []
        
    first_match = matches[0]
    first_text = transcription[0:first_match.start()].strip()
    if first_text:
        segments.append({
            "start": 0.0,
            "end": parse_time_to_seconds(first_match.group(1)),
            "text": first_text
        })
        
    for i, match in enumerate(matches):
        start_str = match.group(1)
        end_str = match.group(2)
        
        start_sec = parse_time_to_seconds(start_str)
        end_sec = parse_time_to_seconds(end_str) if end_str else None
        
        start_pos = match.end()
        end_pos = matches[i+1].start() if i + 1 < len(matches) else len(transcription)
        text = transcription[start_pos:end_pos].strip()
        text = re.sub(r'\s+', ' ', text)
        
        segments.append({
            "start": start_sec,
            "end": end_sec,
            "text": text
        })
        
    for i in range(len(segments)):
        if segments[i]["end"] is None:
            if i + 1 < len(segments):
                segments[i]["end"] = segments[i+1]["start"]
            else:
                segments[i]["end"] = segments[i]["start"] + 5.0
                
    return segments

def rebuild_script_for_short(transcription: str, start_time: str, end_time: str, fallback_script: str) -> str:
    try:
        start_sec = parse_time_to_seconds(start_time)
        end_sec = parse_time_to_seconds(end_time)
    except Exception:
        return fallback_script

    segments = parse_transcription_segments(transcription)
    matching_texts = []
    
    for seg in segments:
        if seg["start"] < (end_sec - 0.01) and seg["end"] > (start_sec + 0.01):
            matching_texts.append(seg["text"])
            
    rebuilt = " ".join(matching_texts).strip()
    return rebuilt if rebuilt else fallback_script

def extract_video_id(url: str) -> str | None:
    pattern = r'(?:https?:\/\/)?(?:www\.)?(?:youtube\.com\/(?:[^\/\n\s]+\/\S+\/|(?:v|e(?:mbed)?)\/|\S*?[?&]v=)|youtu\.be\/)([a-zA-Z0-9_-]{11})'
    match = re.search(pattern, url)
    return match.group(1) if match else None

def download_audio_via_rapidapi(youtube_url: str, output_path: str) -> str:
    video_id = extract_video_id(youtube_url)
    if not video_id:
        raise Exception("رابط الفيديو غير صالح!")

    RAPID_API_KEY = os.environ.get("RAPID_API_KEY", "78aaeed1d3mshdc777f49020e221p1803c4jsn35138c026a86")

    headers = {
        'x-rapidapi-host': 'youtube-mp4-mp3-downloader.p.rapidapi.com',
        'x-rapidapi-key': RAPID_API_KEY,
        'Content-Type': 'application/json'
    }

    # 1. إرسال طلب البدء بالتحويل والحصول على الـ Task ID
    print(f"[*] Sending download request to RapidAPI for video ID: {video_id}...", flush=True)
    api_url = "https://youtube-mp4-mp3-downloader.p.rapidapi.com/api/v1/download"
    params = {
        'format': 'mp3',
        'id': video_id,
        'audioQuality': '251',
        'addInfo': 'false',
        'allowExtendedDuration': 'false'
    }

    response = requests.get(api_url, headers=headers, params=params)
    if response.status_code != 200:
        raise Exception(f"Failed to start conversion. Status code: {response.status_code}. Detail: {response.text}")
    
    res_data = response.json()
    task_id = res_data.get('progressId') or res_data.get('id')
    if not task_id:
        raise Exception(f"Task ID not found in response: {res_data}")

    print(f"[+] Conversion started. Task ID: {task_id}", flush=True)

    # 2. Polling loop
    progress_url = "https://youtube-mp4-mp3-downloader.p.rapidapi.com/api/v1/progress"
    download_url = None
    max_retries = 60  # 5 minutes max
    
    for attempt in range(max_retries):
        print(f"[*] Checking conversion progress... (attempt {attempt + 1})", flush=True)
        
        progress_res = requests.get(progress_url, headers=headers, params={'id': task_id})
        if progress_res.status_code != 200:
            print(f"⚠️ Progress request failed: {progress_res.status_code}. Retrying...", flush=True)
            time.sleep(5)
            continue
            
        progress_data = progress_res.json()
        
        if progress_data.get('finished') is True or progress_data.get('status') == 'Finished':
            download_url = progress_data.get('downloadUrl')
            print(f"🎉 Conversion finished on RapidAPI server!", flush=True)
            break
        elif progress_data.get('status') in ['Failed', 'Error']:
            raise Exception(f"Conversion failed on RapidAPI server: {progress_data}")
            
        time.sleep(5)

    if not download_url:
        raise Exception("Timeout waiting for conversion to finish on RapidAPI.")

    # 3. Download the converted MP3 file
    print("[*] Downloading MP3 file from RapidAPI...", flush=True)
    audio_res = requests.get(download_url, stream=True)
    if audio_res.status_code != 200:
        raise Exception(f"Failed to download audio file. Status: {audio_res.status_code}")
        
    with open(output_path, 'wb') as f:
        for chunk in audio_res.iter_content(chunk_size=1024*1024):
            if chunk:
                f.write(chunk)
                
    print("[+] Audio downloaded and saved successfully.", flush=True)
    return output_path

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

    # أولاً نقوم بضبط التوقيتات ذات المدى (مثل [00:05 -> 00:10])
    pattern_range = r'\[\s*(\d{1,2}:\d{2}(?::\d{2})?)\s*->\s*(\d{1,2}:\d{2}(?::\d{2})?)\s*\]'
    text = re.sub(pattern_range, lambda m: f"[{shift_time(m.group(1))} -> {shift_time(m.group(2))}]", text)
    
    # ثانياً نقوم بضبط التوقيتات الفردية (مثل [00:00] أو [00:03]) التي قد تخرج أحياناً من الذكاء الاصطناعي
    pattern_single = r'\[\s*(\d{1,2}:\d{2}(?::\d{2})?)\s*\]'
    text = re.sub(pattern_single, lambda m: f"[{shift_time(m.group(1))}]", text)
    
    return text

def transcribe_audio_with_gemini(audio_path: str, api_key: str, chunk_minutes: int = 7, task_id: str = None) -> str:
    genai.configure(api_key=api_key)
    # Use the model requested by the user
    selected_model = "gemini-3.1-flash-lite"
    full_transcription = ""

    print(f"🟢 النموذج المستخدم: {selected_model}", flush=True)
    if task_id and task_id in TASKS:
        TASKS[task_id]["progress"] = "تقسيم ملف الصوت لتفادي استهلاك الذاكرة..."

    # Split audio into chunks using ffmpeg to avoid loading the entire file into memory (OOM crash)
    dir_name = os.path.dirname(audio_path)
    chunk_pattern = os.path.join(dir_name, "chunk_%d.mp3")
    segment_time_sec = chunk_minutes * 60
    
    split_cmd = [
        'ffmpeg', '-y',
        '-i', audio_path,
        '-f', 'segment',
        '-segment_time', str(segment_time_sec),
        '-c', 'copy',
        chunk_pattern
    ]
    
    print("[*] Splitting audio using ffmpeg to prevent OOM...", flush=True)
    subprocess.run(split_cmd, capture_output=True)
    
    import glob
    chunk_files = glob.glob(os.path.join(dir_name, "chunk_*.mp3"))
    
    # Sort chunks numerically based on their index (e.g. chunk_0.mp3, chunk_1.mp3)
    def get_chunk_idx(filepath):
        try:
            basename = os.path.basename(filepath)
            num_part = basename.split('_')[1].split('.')[0]
            return int(num_part)
        except:
            return 9999
            
    chunk_files.sort(key=get_chunk_idx)

    print(f"[+] تم تقسيم الصوت إلى {len(chunk_files)} أجزاء.", flush=True)
    if task_id and task_id in TASKS:
        TASKS[task_id]["progress"] = f"تم تقسيم الصوت إلى {len(chunk_files)} أجزاء. جاري بدء تفريغها..."

    for idx, chunk_path in enumerate(chunk_files):
        msg = f"معالجة الجزء {idx + 1}/{len(chunk_files)}: جاري الرفع إلى Gemini..."
        print(f"\n[*] {msg}", flush=True)
        if task_id and task_id in TASKS:
            TASKS[task_id]["progress"] = msg

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

            msg = f"معالجة الجزء {idx + 1}/{len(chunk_files)}: جاري التحليل والتفريغ..."
            print(f"   - {msg}", flush=True)
            if task_id and task_id in TASKS:
                TASKS[task_id]["progress"] = msg

            model = genai.GenerativeModel(selected_model)

            prompt = (
                "أنت خبير تفريغ نصوص صوتية محترف. "
                "قم بالاستماع للملف الصوتي المرفق بتركيز شديد وتفريغ كل كلمة بدقة باللغة العربية دون تلخيص أو إغفال لأي جملة.\n\n"
                "⚠️ شروط التوقيت الحاسمة والمطلوبة حصراً:\n"
                "1. يجب كتابة كل جملة أو فكرة في سطر مستقل يبدأ بنطاق زمني بصيغة: `[البداية -> النهاية] النص العربي`.\n"
                "2. يمنع منعاً باتاً استخدام توقيت فردي مثل `[00:05]`، بل يجب تحديد وقت البداية ووقت النهاية للجملة بدقة (مثال: `[00:05 -> 00:10]`).\n"
                "3. احرص على أن تكون الفترات الزمنية قصيرة ومحددة (تتراوح بين ثانيتين إلى 7 ثوانٍ كحد أقصى لكل سطر) لضمان أعلى دقة مزامنة ممكنة.\n"
                "4. ابدأ التوقيت من [00:00] بالنسبة للملف المرفق.\n\n"
                "أمثلة للتنسيق المطلوب:\n"
                "[00:00 -> 00:04] أهلاً بكم في هذه الحلقة الجديدة.\n"
                "[00:04 -> 00:09] اليوم سنتحدث عن أسرار البحار والمحيطات.\n"
                "[00:09 -> 00:13] البحر مليء بالمفاجآت العجيبة."
            )

            response = model.generate_content([prompt, uploaded_file])

            print("✅ تم تفريغ الجزء.", flush=True)

            # تعديل التوقيتات برمجياً
            adjusted_text = adjust_timestamps(response.text, idx * chunk_minutes)
            # تحويل أي توقيتات فردية متبقية إلى نطاقات زمنية لضمان دقة القص والسكريبت 100%
            adjusted_text = convert_single_timestamps_to_ranges(adjusted_text)
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

def run_transcription_background(task_id: str, youtube_url: str, gemini_api_key: str, task_dir: str):
    # To prevent memory leak, keep TASKS size under control
    if len(TASKS) > 200:
        keys_to_remove = list(TASKS.keys())[:50]
        for k in keys_to_remove:
            TASKS.pop(k, None)

    try:
        audio_path = os.path.join(task_dir, "audio.mp3")
        
        print(f"[{task_id}] Background: Downloading YouTube audio via RapidAPI...", flush=True)
        download_audio_via_rapidapi(youtube_url, audio_path)
        
        if not os.path.exists(audio_path):
            raise Exception("فشل تحميل ملف الصوت من السيرفر.")
            
        print(f"[{task_id}] Background: Transcribing audio with Gemini...", flush=True)
        transcription_text = transcribe_audio_with_gemini(
            audio_path=audio_path,
            api_key=gemini_api_key,
            task_id=task_id
        )
        
        # Success
        TASKS[task_id].update({
            "status": "success",
            "progress": "اكتمل بنجاح!",
            "audioUrl": f"public/temp_{task_id}/audio.mp3",
            "transcription": transcription_text
        })
        
    except Exception as e:
        clean_temp_dir(task_dir)
        print(f"[{task_id}] Background process failed: {e}", flush=True)
        TASKS[task_id].update({
            "status": "failed",
            "progress": f"فشل: {str(e)}",
            "error": str(e)
        })

@app.post("/api/transcribe-gemini")
async def transcribe_gemini(req: DownloadRequest, background_tasks: BackgroundTasks):
    if not req.geminiApiKey or req.geminiApiKey.strip() in ["", "none", "null"]:
        raise HTTPException(status_code=400, detail="Gemini API key is missing or invalid.")
        
    task_id = str(uuid.uuid4())
    task_dir = os.path.join(PUBLIC_DIR, f"temp_{task_id}")
    os.makedirs(task_dir, exist_ok=True)
    
    # Initialize task status
    TASKS[task_id] = {
        "status": "processing",
        "progress": "جاري بدء المهمة...",
        "audioUrl": None,
        "transcription": None,
        "error": None
    }
    
    # Run task in background
    background_tasks.add_task(
        run_transcription_background, 
        task_id, 
        req.youtubeUrl, 
        req.geminiApiKey, 
        task_dir
    )
    
    # Schedule cleanup in the background after 20 minutes to save disk space
    background_tasks.add_task(schedule_dir_cleanup, task_dir, 1200)
    
    return {
        "status": "queued",
        "taskId": task_id
    }

@app.get("/api/task-status/{task_id}")
async def get_task_status(task_id: str):
    if task_id not in TASKS:
        raise HTTPException(status_code=404, detail="Task not found")
    return TASKS[task_id]

@app.post("/api/suggest-shorts")
async def suggest_shorts(req: SuggestShortsRequest):
    if not req.geminiApiKey or req.geminiApiKey.strip() in ["", "none", "null"]:
        raise HTTPException(status_code=400, detail="Gemini API key is missing or invalid.")
    
    if not req.transcription or req.transcription.strip() == "":
        raise HTTPException(status_code=400, detail="Transcription content is empty.")
    
    try:
        genai.configure(api_key=req.geminiApiKey)
        
        # Use structured JSON outputs with Gemini to guarantee perfect schema match
        model = genai.GenerativeModel(
            model_name="gemini-3-flash-preview",
            generation_config={
                "response_mime_type": "application/json",
                "response_schema": ShortsResponse
            }
        )
        
        prompt = (
            "أنت خبير محترف في صناعة المحتوى الفيروسي (Viral Content Creator) ومقاطع الفيديو القصيرة (Shorts/Reels/TikTok).\n"
            f"قم بتحليل النص المفرغ التالي (الذي يحتوي على توقيتات دقيقة)، واستخرج منه أفضل {req.numShorts} مقاطع قصيرة (Shorts) مميزة ومثيرة للاهتمام وتصلح لتكون مقاطع مستقلة ناجحة.\n\n"
            "شروط استخراج كل مقطع:\n"
            "1. يجب أن تكون البداية والنهاية مستندة بدقة إلى التوقيتات الموجودة في النص المرفق. لا تخترع توقيتات جديدة.\n"
            "2. اكتب أوقات البداية والنهاية كما هي مكتوبة في النص المفرغ تماماً دون أي تعديل أو تحويل (مثال: 05:47 أو 12:30).\n"
            "3. يجب أن تتراوح مدة كل مقطع بين 30 ثانية إلى 120 ثانية (دقيقتين كحد أقصى) إذا احتاج المقطع ذلك لاكتمال المعنى والمعالجة.\n"
            "4. يجب تحديد 'الخطاف' (Hook) وهو أول جملة نطقها المتحدث في أول 3 ثوانٍ بنفس المقطع تماماً وبنفس الكلمات المكتوبة في السكريبت دون أي تلخيص أو تغيير.\n"
            "5. يجب كتابة السكريبت (script) الخاص بالمقطع بدقة كما ورد في النص المفرغ دون تغيير الكلمات.\n"
            "6. صياغة عنوان جذاب جداً ومثير للاهتمام (Catchy Title) لكل مقطع.\n"
            "7. تنبيه هام جداً للمزامنة وقص الفيديوهات: عند تحديد وقت النهاية (end_time) لمقطع الشورت، احذر أن تدرج في السكريبت (script) أي جملة تبدأ في وقت النهاية أو بعده. فمثلاً، إذا كان وقت النهاية هو 03:06، فإن الجملة التي تبدأ بـ `[03:06]` أو `[03:06 -> 03:12]` تقع بعد وقت النهاية الفعلي للمقطع ولا يجب أن تظهر في السكريبت الخاص بهذا الشورت نهائياً، لأن الفيديو ينتهي عند 03:06 تماماً قبل نطق هذه الجملة.\n\n"
            "النص المفرغ المراد تحليله:\n"
            f"{req.transcription}"
        )
        
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(None, lambda: model.generate_content(prompt))
        
        import json
        try:
            shorts_data = json.loads(response.text)
            shorts_list = shorts_data.get("shorts", [])
            max_secs = get_max_transcription_seconds(req.transcription)
            for s in shorts_list:
                s["start_time"] = normalize_time_str(s.get("start_time", "00:00:00"), max_secs)
                s["end_time"] = normalize_time_str(s.get("end_time", "00:00:00"), max_secs)
                
                # Rebuild/trim script using the normalized times and transcription to guarantee perfect boundary matching!
                s["script"] = rebuild_script_for_short(
                    transcription=req.transcription,
                    start_time=s["start_time"],
                    end_time=s["end_time"],
                    fallback_script=s.get("script", "")
                )

                # Post-process hook to guarantee it strictly matches the exact opening sentence of the short clip
                script_text = s.get("script", "").strip()
                if script_text:
                    first_clause = re.split(r'[.!\?\n]', script_text)[0].strip()
                    if first_clause:
                        s["hook"] = first_clause

                
            return {
                "status": "success",
                "shorts": shorts_list
            }
        except Exception as parse_err:
            print(f"Failed to parse Gemini JSON: {parse_err}. Raw response: {response.text}", flush=True)
            raise Exception("Failed to parse AI response into structured shorts format.")
            
    except Exception as e:
        print(f"Failed to suggest shorts: {e}", flush=True)
        raise HTTPException(status_code=500, detail=str(e))


def run_suggest_shorts_background(task_id: str, req: SuggestShortsRequest):
    if len(TASKS) > 200:
        keys_to_remove = list(TASKS.keys())[:50]
        for k in keys_to_remove:
            TASKS.pop(k, None)

    try:
        TASKS[task_id] = {"status": "processing", "progress": f"✨ جاري تحليل النص بالذكاء الاصطناعي واقتراح {req.numShorts} مقاطع Shorts..."}

        genai.configure(api_key=req.geminiApiKey)
        model = genai.GenerativeModel(
            model_name="gemini-3-flash-preview",
            generation_config={
                "response_mime_type": "application/json",
                "response_schema": ShortsResponse
            }
        )

        prompt = (
            "أنت خبير محترف في صناعة المحتوى الفيروسي (Viral Content Creator) ومقاطع الفيديو القصيرة (Shorts/Reels/TikTok).\n"
            f"قم بتحليل النص المفرغ التالي (الذي يحتوي على توقيتات دقيقة)، واستخرج منه أفضل {req.numShorts} مقاطع قصيرة (Shorts) مميزة ومثيرة للاهتمام وتصلح لتكون مقاطع مستقلة ناجحة.\n\n"
            "شروط استخراج كل مقطع:\n"
            "1. يجب أن تكون البداية والنهاية مستندة بدقة إلى التوقيتات الموجودة في النص المرفق. لا تخترع توقيتات جديدة.\n"
            "2. اكتب أوقات البداية والنهاية كما هي مكتوبة في النص المفرغ تماماً دون أي تعديل أو تحويل (مثال: 05:47 أو 12:30).\n"
            "3. يجب أن تتراوح مدة كل مقطع بين 30 ثانية إلى 120 ثانية (دقيقتين كحد أقصى) إذا احتاج المقطع ذلك لاكتمال المعنى والمعالجة.\n"
            "4. يجب تحديد 'الخطاف' (Hook) وهو أول جملة نطقها المتحدث في أول 3 ثوانٍ بنفس المقطع تماماً وبنفس الكلمات المكتوبة في السكريبت دون أي تلخيص أو تغيير.\n"
            "5. يجب كتابة السكريبت (script) الخاص بالمقطع بدقة كما ورد في النص المفرغ دون تغيير الكلمات.\n"
            "6. صياغة عنوان جذاب جداً ومثير للاهتمام (Catchy Title) لكل مقطع.\n"
            "7. تنبيه هام جداً للمزامنة وقص الفيديوهات: عند تحديد وقت النهاية (end_time) لمقطع الشورت، احذر أن تدرج في السكريبت (script) أي جملة تبدأ في وقت النهاية أو بعده. فمثلاً، إذا كان وقت النهاية هو 03:06، فإن الجملة التي تبدأ بـ `[03:06]` أو `[03:06 -> 03:12]` تقع بعد وقت النهاية الفعلي للمقطع ولا يجب أن تظهر في السكريبت الخاص بهذا الشورت نهائياً، لأن الفيديو ينتهي عند 03:06 تماماً قبل نطق هذه الجملة.\n\n"
            "النص المفرغ المراد تحليله:\n"
            f"{req.transcription}"
        )

        response = model.generate_content(prompt)

        import json
        shorts_data = json.loads(response.text)
        shorts_list = shorts_data.get("shorts", [])
        max_secs = get_max_transcription_seconds(req.transcription)
        for s in shorts_list:
            s["start_time"] = normalize_time_str(s.get("start_time", "00:00:00"), max_secs)
            s["end_time"] = normalize_time_str(s.get("end_time", "00:00:00"), max_secs)

            s["script"] = rebuild_script_for_short(
                transcription=req.transcription,
                start_time=s["start_time"],
                end_time=s["end_time"],
                fallback_script=s.get("script", "")
            )

            script_text = s.get("script", "").strip()
            if script_text:
                first_clause = re.split(r'[.!\?\n]', script_text)[0].strip()
                if first_clause:
                    s["hook"] = first_clause

        TASKS[task_id] = {
            "status": "success",
            "progress": "✅ تم اقتراح المقاطع بنجاح!",
            "shorts": shorts_list
        }

    except Exception as e:
        print(f"[{task_id}] suggest_shorts_async failed: {e}", flush=True)
        TASKS[task_id] = {
            "status": "failed",
            "error": str(e)
        }


@app.post("/api/suggest-shorts-async")
def suggest_shorts_async(req: SuggestShortsRequest, background_tasks: BackgroundTasks):
    if not req.geminiApiKey or req.geminiApiKey.strip() in ["", "none", "null"]:
        raise HTTPException(status_code=400, detail="Gemini API key is missing or invalid.")
    
    if not req.transcription or req.transcription.strip() == "":
        raise HTTPException(status_code=400, detail="Transcription content is empty.")

    task_id = str(uuid.uuid4())
    TASKS[task_id] = {"status": "processing", "progress": "جاري تحضير طلب اقتراح المقاطع..."}
    background_tasks.add_task(run_suggest_shorts_background, task_id, req)
    
    return {"status": "processing", "taskId": task_id}


import subprocess
import threading

TEMP_DIR = "/tmp/yt_segments"
os.makedirs(TEMP_DIR, exist_ok=True)

def cleanup_old_files():
    while True:
        now = time.time()
        for f in os.listdir(TEMP_DIR):
            filepath = os.path.join(TEMP_DIR, f)
            if os.path.isfile(filepath) and now - os.path.getmtime(filepath) > 600:
                try:
                    os.remove(filepath)
                except:
                    pass
        time.sleep(60)

threading.Thread(target=cleanup_old_files, daemon=True).start()

def get_cookie_header_from_file(cookie_file_path: str) -> str:
    if not os.path.exists(cookie_file_path) or os.path.getsize(cookie_file_path) == 0:
        return ""
    cookies = []
    try:
        with open(cookie_file_path, 'r', encoding='utf-8') as f:
            for line in f:
                if line.startswith('#') or not line.strip():
                    continue
                parts = line.strip().split('\t')
                if len(parts) >= 7:
                    name = parts[5]
                    value = parts[6]
                    cookies.append(f"{name}={value}")
    except Exception as e:
        print(f"Error parsing cookies file: {e}", flush=True)
    return "; ".join(cookies)

def get_ffmpeg_headers(format_dict) -> str:
    headers = format_dict.get('http_headers', {})
    header_str = ""
    for k, v in headers.items():
        if k.lower() == 'referer':
            continue
        header_str += f"{k}: {v}\r\n"
    
    # Enforce Referer header for googlevideo streams to bypass 403 Forbidden
    header_str += "Referer: https://www.youtube.com/\r\n"
    
    # Ensure User-Agent is present
    if "User-Agent" not in header_str and "user-agent" not in header_str.lower():
        header_str += "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36\r\n"
    
    # Append Cookie header manually from file if present in the tmp folder
    cookie_str = get_cookie_header_from_file(COOKIE_FILE_PATH)
    if cookie_str and "Cookie:" not in header_str:
        header_str += f"Cookie: {cookie_str}\r\n"
        
    return header_str

def format_seconds_to_time_str(seconds: float) -> str:
    """Format float seconds into HH:MM:SS.mmm format for precise clipping"""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int(round((seconds % 1) * 1000))
    if ms == 1000:
        s += 1
        ms = 0
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"

@app.post("/api/cut")
def cut_video(req: CutRequest):
    if req.quality not in [360, 480, 720, 1080, 1440, 2160]:
        raise HTTPException(400, "quality must be 360, 480, 720, 1080, 1440, or 2160")

    try:
        start_sec = parse_time_to_seconds(req.start_time)
        end_sec = parse_time_to_seconds(req.end_time)
    except Exception as e:
        raise HTTPException(400, f"Invalid start_time or end_time: {str(e)}")

    if start_sec >= end_sec:
        raise HTTPException(400, "start_time must be less than end_time")

    # تمديد مدة التحميل والقطع بمقدار 0.75 ثانية من النهاية لعمل تأثير التلاشي عليها
    # وتقليص تأثير التلاشي في البداية ليكون خفيفاً جداً لكي لا يضيع أول الكلام
    end_extension = 0.75
    fade_in_duration = 0.2
    fade_out_duration = 0.75
    
    extended_end_sec = end_sec + end_extension
    start_time_str = format_seconds_to_time_str(start_sec)
    extended_end_time_str = format_seconds_to_time_str(extended_end_sec)
    
    original_duration_sec = end_sec - start_sec

    file_id = str(uuid.uuid4())[:8]
    temp_raw_path = os.path.join(TEMP_DIR, f"{file_id}_raw.mp4")
    output_path = os.path.join(TEMP_DIR, f"{file_id}.mp4")

    # تشغيل أمر yt-dlp للتحميل والقص مباشرة لتفادي مشاكل الـ 403 وحظر يوتيوب
    print(f"🎬 Downloading and cutting segment: {start_time_str} to {extended_end_time_str} using direct YouTube link...", flush=True)
    start_time_proc = time.time()
    
    ytdl_cmd = [
        'yt-dlp',
        '--quiet', '--no-warnings',
        '--no-playlist',
        '--download-sections', f"*{start_time_str}-{extended_end_time_str}",
        '--force-keyframes-at-cuts',
        '-f', f"best[height<={req.quality}]/bestvideo[height<={req.quality}]+bestaudio/best",
        '--merge-output-format', 'mp4',
        '-o', temp_raw_path
    ]

    if os.path.exists(COOKIE_FILE_PATH) and os.path.getsize(COOKIE_FILE_PATH) > 0:
        ytdl_cmd.extend(['--cookies', COOKIE_FILE_PATH])

    ytdl_cmd.append(req.url)

    result = subprocess.run(ytdl_cmd, capture_output=True, text=True)
    elapsed = time.time() - start_time_proc

    if result.returncode != 0:
        if os.path.exists(temp_raw_path):
            try: os.remove(temp_raw_path)
            except: pass
        err_lines = result.stderr.strip().split('\n')
        last_err_lines = "\n".join(err_lines[-5:]) if len(err_lines) > 5 else result.stderr
        raise HTTPException(500, f"Cutting failed: {last_err_lines}")

    if not os.path.exists(temp_raw_path):
        raise HTTPException(500, "Output MP4 file was not generated by yt-dlp")

    # تطبيق الفلاتر الصوتية (تخفيت الصوت في البداية والنهاية وزيادة الصوت بنسبة 50%)
    fade_applied = False
    # يبدأ التلاشي النهائي (Fade Out) عند نهاية المقطع المحدد أصلياً (ثانية 0 إلى original_duration_sec لا يتأثران، والتلاشي يتم في الـ 0.75 ثانية الإضافية)
    start_fade_out = original_duration_sec
    
    ffmpeg_cmd = [
        'ffmpeg', '-y',
        '-i', temp_raw_path,
        '-filter_complex', f"[0:a]volume=1.5,afade=t=in:st=0:d={fade_in_duration},afade=t=out:st={start_fade_out}:d={fade_out_duration}[a]",
        '-map', '0:v', '-map', '[a]',
        '-c:v', 'copy',
        '-c:a', 'aac', '-b:a', '192k',
        output_path
    ]
    
    filter_result = subprocess.run(ffmpeg_cmd, capture_output=True, text=True)
    if filter_result.returncode == 0 and os.path.exists(output_path):
        fade_applied = True
        
    # تنظيف الملف المؤقت الخام
    try: os.remove(temp_raw_path)
    except: pass

    # في حال فشل الفلتر لأي سبب (مثل عدم وجود مسار صوتي)، نستخدم الملف الأصلي
    if not fade_applied:
        if os.path.exists(temp_raw_path):
            try: os.rename(temp_raw_path, output_path)
            except: pass

    if not os.path.exists(output_path):
        raise HTTPException(500, "Final output MP4 file was not generated")

    size_mb = os.path.getsize(output_path) / (1024 * 1024)
    print(f"✅ Success! {size_mb:.2f}MB | {elapsed:.1f}s | {req.quality}p MP4 (Fade/Volume applied: {fade_applied})", flush=True)

    return FileResponse(
        output_path,
        media_type="video/mp4",
        filename=f"cut_{file_id}.mp4"
    )


def smart_vertical_crop(video_path: str, output_path: str) -> bool:
    """
    Analyzes scene cuts using PySceneDetect and active faces using MediaPipe/OpenCV,
    crops each scene to a 9:16 target ratio centered on the active speaker,
    and writes out a high-quality vertical MP4 video file.
    """
    try:
        import cv2
        import numpy as np

        mp_face = None
        face_detector = None
        try:
            import mediapipe as mp
            mp_face = mp.solutions.face_detection
            face_detector = mp_face.FaceDetection(model_selection=1, min_detection_confidence=0.5)
        except Exception as mpe:
            print(f"MediaPipe face detection not available: {mpe}. Fallback to center crop.", flush=True)

        scene_cuts = [0.0]
        try:
            from scenedetect import detect, ContentDetector
            scene_list = detect(video_path, ContentDetector(threshold=27.0))
            for scene in scene_list:
                scene_cuts.append(scene[1].get_seconds())
        except Exception as sde:
            print(f"PySceneDetect failed: {sde}. Fallback to single scene.", flush=True)

        from moviepy.editor import VideoFileClip, concatenate_videoclips

        clip = VideoFileClip(video_path)
        if len(scene_cuts) == 1 or scene_cuts[-1] < clip.duration:
            scene_cuts.append(clip.duration)

        def get_center_x(frame):
            if face_detector:
                try:
                    rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB) if frame.shape[2] == 3 else frame
                    results = face_detector.process(rgb_frame)
                    h, w, _ = frame.shape
                    if results and results.detections:
                        bbox = results.detections[0].location_data.relative_bounding_box
                        x_center = int((bbox.xmin + bbox.width / 2) * w)
                        return x_center
                except Exception:
                    pass
            h, w, _ = frame.shape
            return w // 2

        sub_clips = []
        for i in range(len(scene_cuts) - 1):
            start_t = scene_cuts[i]
            end_t = scene_cuts[i+1]
            
            if end_t - start_t < 0.4:
                continue
                
            sub = clip.subclip(start_t, end_t)
            first_frame = sub.get_frame(0)
            
            target_w = int(sub.h * 9 / 16)
            if target_w > sub.w:
                target_w = sub.w

            x_center = get_center_x(first_frame)
            
            x1 = max(0, x_center - target_w // 2)
            x2 = min(sub.w, x_center + target_w // 2)
            
            if x1 <= 0:
                x1 = 0
                x2 = target_w
            elif x2 >= sub.w:
                x2 = sub.w
                x1 = sub.w - target_w
                
            cropped_sub = sub.crop(x1=x1, y1=0, x2=x2, y2=sub.h)
            sub_clips.append(cropped_sub)

        if sub_clips:
            final_video = concatenate_videoclips(sub_clips)
            final_video.write_videofile(
                output_path,
                codec="libx264",
                audio_codec="aac",
                preset="fast",
                threads=4,
                logger=None
            )
            clip.close()
            final_video.close()
            return True
        else:
            clip.close()
            return False

    except Exception as e:
        print(f"Smart vertical crop failed: {e}", flush=True)
        return False


def run_cut_background(task_id: str, req: CutRequest, task_dir: str):
    if len(TASKS) > 200:
        keys_to_remove = list(TASKS.keys())[:50]
        for k in keys_to_remove:
            TASKS.pop(k, None)

    try:
        TASKS[task_id] = {"status": "processing", "progress": "🎬 جاري بدء قص المقطع..."}
        
        start_sec = parse_time_to_seconds(req.start_time)
        end_sec = parse_time_to_seconds(req.end_time)
        
        if start_sec >= end_sec:
            raise Exception("start_time must be less than end_time")
            
        end_extension = 0.75
        fade_in_duration = 0.2
        fade_out_duration = 0.75
        
        extended_end_sec = end_sec + end_extension
        start_time_str = format_seconds_to_time_str(start_sec)
        extended_end_time_str = format_seconds_to_time_str(extended_end_sec)
        original_duration_sec = end_sec - start_sec

        temp_raw_path = os.path.join(task_dir, "raw.mp4")
        output_path = os.path.join(task_dir, "short_clip.mp4")

        TASKS[task_id]["progress"] = "🎬 جاري استخراج وقص الفيديو من يوتيوب..."
        print(f"[{task_id}] Async Cutting: {start_time_str} to {extended_end_time_str}...", flush=True)

        ytdl_cmd = [
            'yt-dlp',
            '--quiet', '--no-warnings',
            '--no-playlist',
            '--download-sections', f"*{start_time_str}-{extended_end_time_str}",
            '--force-keyframes-at-cuts',
            '-f', f"best[height<={req.quality}]/bestvideo[height<={req.quality}]+bestaudio/best",
            '--merge-output-format', 'mp4',
            '-o', temp_raw_path
        ]

        if os.path.exists(COOKIE_FILE_PATH) and os.path.getsize(COOKIE_FILE_PATH) > 0:
            ytdl_cmd.extend(['--cookies', COOKIE_FILE_PATH])

        ytdl_cmd.append(req.url)

        result = subprocess.run(ytdl_cmd, capture_output=True, text=True)

        if result.returncode != 0 or not os.path.exists(temp_raw_path):
            err_msg = result.stderr.strip() if result.stderr else "Output MP4 file was not generated by yt-dlp"
            raise Exception(f"Cutting failed: {err_msg}")

        TASKS[task_id]["progress"] = "✨ جاري تطبيق الفلاتر الصوتية والتلاشي..."
        start_fade_out = original_duration_sec
        ffmpeg_cmd = [
            'ffmpeg', '-y',
            '-i', temp_raw_path,
            '-filter_complex', f"[0:a]volume=1.5,afade=t=in:st=0:d={fade_in_duration},afade=t=out:st={start_fade_out}:d={fade_out_duration}[a]",
            '-map', '0:v', '-map', '[a]',
            '-c:v', 'copy',
            '-c:a', 'aac', '-b:a', '192k',
            output_path
        ]
        
        filter_result = subprocess.run(ffmpeg_cmd, capture_output=True, text=True)
        if filter_result.returncode != 0 or not os.path.exists(output_path):
            if os.path.exists(temp_raw_path):
                try: os.rename(temp_raw_path, output_path)
                except: pass

        if os.path.exists(temp_raw_path):
            try: os.remove(temp_raw_path)
            except: pass

        if req.convert_vertical:
            TASKS[task_id]["progress"] = "📱 جاري تحويل الفيديو إلى طولي (9:16) وتتبع الوجوه بالذكاء الاصطناعي..."
            vert_output_path = os.path.join(task_dir, "vert_clip.mp4")
            success = smart_vertical_crop(output_path, vert_output_path)
            if success and os.path.exists(vert_output_path):
                try:
                    os.replace(vert_output_path, output_path)
                except Exception as ex:
                    print(f"Failed to replace vertical output file: {ex}", flush=True)

        if not os.path.exists(output_path):
            raise Exception("Final clip output file was not found")

        video_url = f"public/temp_{task_id}/short_clip.mp4"
        TASKS[task_id] = {
            "status": "success",
            "progress": "✅ تم قص المقطع بنجاح!",
            "videoUrl": video_url
        }
        print(f"[{task_id}] Async Cut completed successfully: {video_url}", flush=True)

    except Exception as e:
        print(f"[{task_id}] Async Cut failed: {e}", flush=True)
        TASKS[task_id] = {
            "status": "failed",
            "error": str(e)
        }


@app.post("/api/cut-async")
def cut_video_async(req: CutRequest, background_tasks: BackgroundTasks):
    if req.quality not in [360, 480, 720, 1080, 1440, 2160]:
        raise HTTPException(400, "quality must be 360, 480, 720, 1080, 1440, or 2160")

    try:
        parse_time_to_seconds(req.start_time)
        parse_time_to_seconds(req.end_time)
    except Exception as e:
        raise HTTPException(400, f"Invalid start_time or end_time: {str(e)}")

    task_id = str(uuid.uuid4())
    task_dir = os.path.join(PUBLIC_DIR, f"temp_{task_id}")
    os.makedirs(task_dir, exist_ok=True)
    
    TASKS[task_id] = {"status": "processing", "progress": "جاري بدء قص المقطع..."}
    background_tasks.add_task(run_cut_background, task_id, req, task_dir)
    
    return {"status": "processing", "taskId": task_id}

