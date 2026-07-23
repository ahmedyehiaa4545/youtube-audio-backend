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

# Ensure public and temp directories exist
PUBLIC_DIR = os.path.abspath("public")
TEMP_DIR = os.path.abspath("temp")
os.makedirs(PUBLIC_DIR, exist_ok=True)
os.makedirs(TEMP_DIR, exist_ok=True)

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

def cleanup_old_temp_files(max_age_seconds: int = 172800):
    """Clean up temp folders and rendered videos older than 48 hours."""
    try:
        now = time.time()
        for root_dir in [TEMP_DIR, PUBLIC_DIR]:
            if not os.path.exists(root_dir):
                continue
            for item in os.listdir(root_dir):
                item_path = os.path.join(root_dir, item)
                try:
                    if os.stat(item_path).st_mtime < (now - max_age_seconds):
                        if os.path.isdir(item_path):
                            shutil.rmtree(item_path, ignore_errors=True)
                        else:
                            os.remove(item_path)
                except Exception:
                    pass
    except Exception as e:
        print(f"⚠️ Periodic 48h cleanup warning: {e}", flush=True)

init_cookies()
cleanup_old_temp_files()

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
    geminiApiKey: str | None = None
    openrouterApiKey: str | None = None
    openrouterModel: str | None = "google/gemini-3.1-flash-lite"
    customPrompt: str | None = None
    titleStyle: str | None = "auto"
    numShorts: int = 3

class CutRequest(BaseModel):
    url: str
    start_time: str
    end_time: str
    quality: int = 720

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

def download_audio_via_rapidapi(youtube_url: str, output_path: str, task_id: str = None) -> str:
    video_id = extract_video_id(youtube_url)
    if not video_id:
        raise Exception("رابط الفيديو غير صالح!")

    def update_task(msg):
        if task_id and task_id in TASKS:
            TASKS[task_id]["progress"] = msg

    RAPID_API_KEY = os.environ.get("RAPID_API_KEY", "78aaeed1d3mshdc777f49020e221p1803c4jsn35138c026a86")

    headers = {
        'x-rapidapi-host': 'youtube-mp4-mp3-downloader.p.rapidapi.com',
        'x-rapidapi-key': RAPID_API_KEY,
        'Content-Type': 'application/json'
    }

    update_task("🌐 جارٍ طلب تحويل الصوت من خادم RapidAPI...")
    print(f"[*] Sending download request to RapidAPI for video ID: {video_id}...", flush=True)
    api_url = "https://youtube-mp4-mp3-downloader.p.rapidapi.com/api/v1/download"
    params = {
        'format': 'mp3',
        'id': video_id,
        'audioQuality': '128',
        'addInfo': 'false',
        'allowExtendedDuration': 'true'
    }

    response = requests.get(api_url, headers=headers, params=params, timeout=15)
    if response.status_code != 200:
        raise Exception(f"Failed to start conversion. Status code: {response.status_code}. Detail: {response.text}")
    
    res_data = response.json()
    rapid_task_id = res_data.get('progressId') or res_data.get('id')
    if not rapid_task_id:
        raise Exception(f"Task ID not found in response: {res_data}")

    print(f"[+] RapidAPI Conversion started. Task ID: {rapid_task_id}", flush=True)

    progress_url = "https://youtube-mp4-mp3-downloader.p.rapidapi.com/api/v1/progress"
    download_url = None
    max_retries = 25  # 25 * 3s = 75 seconds max timeout
    
    for attempt in range(max_retries):
        prog_percent = min(90, int(((attempt + 1) / max_retries) * 100))
        update_task(f"🌐 جارٍ تجهيز وتحميل ملف الصوت عبر السيرفر ({prog_percent}%)...")
        print(f"[*] Checking conversion progress... (attempt {attempt + 1}/{max_retries})", flush=True)
        
        try:
            progress_res = requests.get(progress_url, headers=headers, params={'id': rapid_task_id}, timeout=10)
            if progress_res.status_code == 200:
                progress_data = progress_res.json()
                if progress_data.get('finished') is True or progress_data.get('status') == 'Finished':
                    download_url = progress_data.get('downloadUrl')
                    print(f"🎉 Conversion finished on RapidAPI server!", flush=True)
                    break
                elif progress_data.get('status') in ['Failed', 'Error']:
                    raise Exception(f"Conversion failed on RapidAPI server: {progress_data}")
        except Exception as pe:
            print(f"⚠️ RapidAPI progress check warning: {pe}", flush=True)

        time.sleep(3)

    if not download_url:
        raise Exception("استغرق خادم التحويل وقتاً طويلاً. يرجى المحاولة مرة أخرى أو استخدام فيديو أقصر.")

    update_task("📥 جارٍ تنزيل ملف MP3 الأصلي...")
    print("[*] Downloading MP3 file from RapidAPI...", flush=True)
    audio_res = requests.get(download_url, stream=True, timeout=60)
    if audio_res.status_code != 200:
        raise Exception(f"Failed to download audio file. Status: {audio_res.status_code}")
        
    with open(output_path, 'wb') as f:
        for chunk in audio_res.iter_content(chunk_size=1024*1024):
            if chunk:
                f.write(chunk)
                
    print("[+] Audio downloaded and saved successfully.", flush=True)
    return output_path

def download_audio_smart(youtube_url: str, output_path: str, task_id: str = None) -> str:
    """
    Downloads YouTube audio fast via direct stream extraction (takes 3-5 seconds for long videos).
    If yt-dlp fails or stalls, falls back smoothly to RapidAPI with real-time status.
    """
    def update_task(msg):
        if task_id and task_id in TASKS:
            TASKS[task_id]["progress"] = msg

    update_task("⚡ جاري استخراج وتنزيل الصوت مباشرة بـ yt-dlp...")
    print(f"⚡ Attempting fast direct audio extraction via yt-dlp for {youtube_url}...", flush=True)

    try:
        raw_temp_audio = output_path + ".raw"
        ytdl_cmd = [
            'yt-dlp',
            '--quiet', '--no-warnings',
            '--no-playlist',
            '--socket-timeout', '15',
            '--concurrent-fragments', '4',
            '-f', 'bestaudio[ext=m4a]/bestaudio/best',
            '-o', raw_temp_audio
        ]
        if os.path.exists(COOKIE_FILE_PATH) and os.path.getsize(COOKIE_FILE_PATH) > 0:
            ytdl_cmd.extend(['--cookies', COOKIE_FILE_PATH])
        ytdl_cmd.append(youtube_url)

        res = subprocess.run(ytdl_cmd, capture_output=True, text=True, timeout=75)

        downloaded_file = None
        if os.path.exists(raw_temp_audio):
            downloaded_file = raw_temp_audio
        else:
            parent_dir = os.path.dirname(raw_temp_audio)
            base_name = os.path.basename(raw_temp_audio)
            for f in os.listdir(parent_dir):
                if f.startswith(base_name):
                    downloaded_file = os.path.join(parent_dir, f)
                    break

        if downloaded_file and os.path.exists(downloaded_file) and os.path.getsize(downloaded_file) > 0:
            update_task("⚡ جارٍ تحويل كودك الصوت إلى MP3 بسرعة...")
            print(f"🚀 Audio stream downloaded ({os.path.getsize(downloaded_file)} bytes). Fast converting to MP3 via ffmpeg...", flush=True)
            
            ffmpeg_cmd = [
                'ffmpeg', '-y',
                '-i', downloaded_file,
                '-ac', '1',
                '-ar', '16000',
                '-b:a', '64k',
                output_path
            ]
            ff_res = subprocess.run(ffmpeg_cmd, capture_output=True, text=True)
            
            try: os.remove(downloaded_file)
            except: pass

            if ff_res.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 0:
                print("🎉 Direct audio extraction succeeded!", flush=True)
                return output_path

        err_msg = res.stderr.strip() if res.stderr else "yt-dlp returned non-zero code or empty file"
        print(f"⚠️ Direct yt-dlp extraction failed ({err_msg}). Falling back to RapidAPI...", flush=True)

    except Exception as e:
        print(f"⚠️ Direct yt-dlp extraction error ({e}). Falling back to RapidAPI...", flush=True)

    update_task("🌐 جارٍ استخراج الصوت عبر خادم التنزيل السريع (RapidAPI)...")
    return download_audio_via_rapidapi(youtube_url, output_path, task_id)

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
    import concurrent.futures

    genai.configure(api_key=api_key)
    selected_model = "gemini-3.1-flash-lite"

    print(f"🟢 النموذج المستخدم: {selected_model}", flush=True)
    if task_id and task_id in TASKS:
        TASKS[task_id]["progress"] = "تقسيم ملف الصوت لتفادي استهلاك الذاكرة..."

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
    
    def get_chunk_idx(filepath):
        try:
            basename = os.path.basename(filepath)
            num_part = basename.split('_')[1].split('.')[0]
            return int(num_part)
        except:
            return 9999
            
    chunk_files.sort(key=get_chunk_idx)
    total_chunks = len(chunk_files)

    print(f"[+] تم تقسيم الصوت إلى {total_chunks} أجزاء.", flush=True)
    if task_id and task_id in TASKS:
        TASKS[task_id]["progress"] = f"تم تقسيم الصوت إلى {total_chunks} أجزاء. جاري التفريغ الموازي..."

    completed_count = 0

    def process_single_chunk(args):
        nonlocal completed_count
        idx, chunk_path = args
        uploaded_file = None
        try:
            print(f"[*] [Chunk {idx+1}/{total_chunks}] Uploading to Gemini...", flush=True)
            uploaded_file = genai.upload_file(path=chunk_path)

            while uploaded_file.state.name == "PROCESSING":
                time.sleep(2)
                uploaded_file = genai.get_file(uploaded_file.name)

            if uploaded_file.state.name == "FAILED":
                raise Exception(f"Gemini file upload state FAILED for chunk {idx+1}")

            print(f"[*] [Chunk {idx+1}/{total_chunks}] Transcribing with Gemini...", flush=True)
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

            adjusted_text = adjust_timestamps(response.text, idx * chunk_minutes)
            adjusted_text = convert_single_timestamps_to_ranges(adjusted_text)

            completed_count += 1
            msg = f"تم تفريغ الجزء {completed_count}/{total_chunks} بالذكاء الاصطناعي..."
            print(f"✅ [Chunk {idx+1}/{total_chunks}] {msg}", flush=True)
            if task_id and task_id in TASKS:
                TASKS[task_id]["progress"] = msg

            return idx, adjusted_text

        finally:
            if uploaded_file:
                try: genai.delete_file(uploaded_file.name)
                except: pass
            if os.path.exists(chunk_path):
                try: os.remove(chunk_path)
                except: pass

    max_workers = min(5, total_chunks) if total_chunks > 0 else 1
    chunks_results = ["" for _ in range(total_chunks)]

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(process_single_chunk, (idx, cp)) for idx, cp in enumerate(chunk_files)]
        for future in concurrent.futures.as_completed(futures):
            idx, text = future.result()
            chunks_results[idx] = text

    return "\n".join(chunks_results).strip()

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
        
        print(f"[{task_id}] Background: Downloading YouTube audio smartly...", flush=True)
        TASKS[task_id]["progress"] = "📥 جاري تحميل صوت اليوتيوب..."
        download_audio_smart(youtube_url, audio_path, task_id=task_id)
        
        if not os.path.exists(audio_path) or os.path.getsize(audio_path) == 0:
            raise Exception("فشل تحميل ملف الصوت من السيرفر.")
            
        print(f"[{task_id}] Background: Transcribing audio with Gemini...", flush=True)
        TASKS[task_id]["progress"] = "✨ جاري تفريغ الصوت وتقسيمه بالذكاء الاصطناعي..."
        transcription_text = transcribe_audio_with_gemini(
            audio_path=audio_path,
            api_key=gemini_api_key,
            task_id=task_id
        )
        
        # Success
        TASKS[task_id].update({
            "status": "success",
            "progress": "اكتمل بنجاح! 🎉",
            "audioUrl": f"public/temp_{task_id}/audio.mp3",
            "transcription": transcription_text
        })
        
    except Exception as e:
        print(f"[{task_id}] Background process failed: {e}", flush=True)
        clean_temp_dir(task_dir)
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

def enforce_title_style(title: str, style: str) -> str:
    if not title or not isinstance(title, str):
        return title
    
    clean_title = title.strip()
    
    if style == "short":
        # 1. If title contains colons, dashes, or question marks, cut at first segment
        for sep in [':', ' - ', ' – ', ' | ', '؟', '?']:
            if sep in clean_title:
                parts = clean_title.split(sep)
                if parts[0].strip():
                    clean_title = parts[0].strip()
                    break
        
        # 2. Strict word count limit (max 5 words)
        words = clean_title.split()
        if len(words) > 5:
            clean_title = " ".join(words[:4]) + "!"
        elif not clean_title.endswith(('!', '؟', '?')):
            clean_title += "!"
            
    elif style == "medium":
        words = clean_title.split()
        if len(words) > 10:
            for sep in [':', ' - ', ' – ', ' | ']:
                if sep in clean_title:
                    clean_title = clean_title.split(sep)[0].strip()
                    break
            words = clean_title.split()
            if len(words) > 9:
                clean_title = " ".join(words[:8]) + "..."
                
    return clean_title

def call_openrouter_shorts(transcription: str, num_shorts: int, api_key: str, model_name: str = "google/gemini-3.1-flash-lite", custom_prompt: str = None, title_style: str = "auto"):
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://rekaption.com",
        "X-Title": "ReKaption"
    }
    system_prompt = (
        "أنت خبير محترف في صناعة المحتوى الفيروسي ومقاطع الفيديو القصيرة (Shorts/Reels/TikTok).\n"
        "يجب أن تكون إجابتك بصيغة JSON فقط بالتنسيق التالي بدون أي نصوص إضافية خارج الـ JSON:\n"
        "{\n"
        '  "shorts": [\n'
        '    {\n'
        '      "title": "عنوان المقطع",\n'
        '      "start_time": "05:47",\n'
        '      "end_time": "06:20",\n'
        '      "script": "النص الكامل للمقطع القصير كما ورد في التفريغ",\n'
        '      "hook": "الجملة الافتتاحية في أول 3 ثواني"\n'
        '    }\n'
        '  ]\n'
        "}\n"
    )
    
    title_instruction = "5. صياغة عنوان جذاب ومثير للاهتمام لكل مقطع."
    if title_style == "short":
        title_instruction = "5. صياغة عنوان قصير ومختصر جداً يتكون من 2 إلى 4 كلمات فقط (حد أقصى 5 كلمات كحد أقصى مطلق!). يمنع منعاً باتاً كتابة عناوين طويلة أو تفصيلية."
    elif title_style == "medium":
        title_instruction = "5. صياغة عنوان متوسط ومفصل من 5 إلى 9 كلمات يوضح فكرة المقطع بوضوح وجاذبية."

    user_prompt = (
        f"قم بتحليل النص المفرغ التالي واستخرج أفضل {num_shorts} مقاطع قصيرة (Shorts) مميزة ومثيرة للاهتمام وتصلح لتكون مقاطع مستقلة ناجحة.\n\n"
        "شروط استخراج كل مقطع:\n"
        "1. يجب أن تكون البداية والنهاية مستندة بدقة إلى التوقيتات الموجودة في النص المرفق (مثال: 05:47 أو 12:30).\n"
        "2. مدة المقطع واكتمال الحكاية/القصة: تتراوح مدة المقاطع العادية بين 30 ثانية و 150 ثانية (دقيقتين ونصف). أما إذا كان المقطع يتضمن قصة أو موقفاً أو حكاية أو مقلباً (Story / Narrative): يمنع منعاً باتاً قطع القصة في منتصفها، ويجب استمرار المقطع حتى اكتمال الخاتمة وقفلة الموقف بالكامل، ويُسمح بالامتداد خصيصاً في حالات القصص والمواقف حتى 210 ثانية (3 دقائق ونصف كحد أقصى) لضمان اكتمال الحكاية ونهايتها السعيدة/المفاجئة دون بتر.\n"
        "3. يجب تحديد 'الخطاف' (Hook) وهو أول جملة نطقها المتحدث في أول 3 ثوانٍ بنفس المقطع تماماً.\n"
        "4. كتابة السكريبت (script) الخاص بالمقطع بدقة كما ورد في النص المفرغ دون تغيير الكلمات.\n"
        f"{title_instruction}\n"
    )

    if custom_prompt and custom_prompt.strip():
        user_prompt += f"\n⚠️ توجيهات إضافية مخصصة من المستخدم (يجب الالتزام بها بصرامة عند تحديد المقاطع):\n{custom_prompt.strip()}\n"

    user_prompt += f"\nالنص المفرغ المراد تحليله:\n{transcription}"

    payload = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "response_format": {"type": "json_object"}
    }
    
    res = requests.post(url, headers=headers, json=payload, timeout=90)
    if res.status_code != 200:
        raise Exception(f"OpenRouter API error (status {res.status_code}): {res.text}")
    
    data = res.json()
    content = data['choices'][0]['message']['content']
    import json
    return json.loads(content)

@app.post("/api/suggest-shorts")
async def suggest_shorts(req: SuggestShortsRequest):
    if not req.transcription or req.transcription.strip() == "":
        raise HTTPException(status_code=400, detail="Transcription content is empty.")
    
    openrouter_key = req.openrouterApiKey or os.environ.get("OPENROUTER_API_KEY")
    openrouter_model = req.openrouterModel if (req.openrouterModel and req.openrouterModel.strip()) else "google/gemini-3.1-flash-lite"
    shorts_list = []

    if openrouter_key and openrouter_key.strip():
        print(f"🌐 Using OpenRouter ({openrouter_model}) for suggest_shorts...", flush=True)
        try:
            shorts_data = call_openrouter_shorts(
                transcription=req.transcription,
                num_shorts=req.numShorts,
                api_key=openrouter_key,
                model_name=openrouter_model,
                custom_prompt=req.customPrompt,
                title_style=req.titleStyle
            )
            shorts_list = shorts_data.get("shorts", [])
        except Exception as or_err:
            print(f"⚠️ OpenRouter failed: {or_err}. Falling back to direct Gemini API...", flush=True)

    if not shorts_list:
        if not req.geminiApiKey or req.geminiApiKey.strip() in ["", "none", "null"]:
            raise HTTPException(status_code=400, detail="Gemini / OpenRouter API key is missing or invalid.")
        try:
            genai.configure(api_key=req.geminiApiKey)
            model = genai.GenerativeModel(
                model_name="gemini-3-flash-preview",
                generation_config={
                    "response_mime_type": "application/json",
                    "response_schema": ShortsResponse
                }
            )
            prompt = (
                "أنت خبير محترف في صناعة المحتوى الفيروسي ومقاطع الفيديو القصيرة.\n"
                f"قم بتحليل النص المفرغ التالي واستخرج منه أفضل {req.numShorts} مقاطع قصيرة مميزة تتراوح مدتها بين 30 ثانية ودقيقتين ونصف (150 ثانية كحد أقصى):\n\n"
            )
            if req.customPrompt and req.customPrompt.strip():
                prompt += f"توجيهات إضافية: {req.customPrompt.strip()}\n\n"
            prompt += req.transcription
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(None, lambda: model.generate_content(prompt))
            import json
            shorts_data = json.loads(response.text)
            shorts_list = shorts_data.get("shorts", [])
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    max_secs = get_max_transcription_seconds(req.transcription)
    for s in shorts_list:
        s["start_time"] = normalize_time_str(s.get("start_time", "00:00:00"), max_secs)
        s["end_time"] = normalize_time_str(s.get("end_time", "00:00:00"), max_secs)
        s["title"] = enforce_title_style(s.get("title", ""), req.titleStyle)
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

    return {
        "status": "success",
        "shorts": shorts_list
    }


def run_suggest_shorts_background(task_id: str, req: SuggestShortsRequest):
    if len(TASKS) > 200:
        keys_to_remove = list(TASKS.keys())[:50]
        for k in keys_to_remove:
            TASKS.pop(k, None)

    try:
        TASKS[task_id] = {"status": "processing", "progress": f"✨ جاري تحليل النص بالذكاء الاصطناعي واقتراح {req.numShorts} مقاطع Shorts..."}

        openrouter_key = req.openrouterApiKey or os.environ.get("OPENROUTER_API_KEY")
        openrouter_model = req.openrouterModel if (req.openrouterModel and req.openrouterModel.strip()) else "google/gemini-3.1-flash-lite"
        shorts_list = []

        if openrouter_key and openrouter_key.strip():
            print(f"[{task_id}] 🌐 Using OpenRouter ({openrouter_model})...", flush=True)
            try:
                shorts_data = call_openrouter_shorts(
                    transcription=req.transcription,
                    num_shorts=req.numShorts,
                    api_key=openrouter_key,
                    model_name=openrouter_model,
                    custom_prompt=req.customPrompt,
                    title_style=req.titleStyle
                )
                shorts_list = shorts_data.get("shorts", [])
            except Exception as or_err:
                print(f"[{task_id}] ⚠️ OpenRouter failed: {or_err}. Falling back to direct Gemini API...", flush=True)

        if not shorts_list:
            print(f"[{task_id}] Using direct Gemini API...", flush=True)
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
                f"قم بتحليل النص المفرغ التالي واستخرج منه أفضل {req.numShorts} مقاطع قصيرة مميزة. "
                "شروط المدة واكتمال الحكاية: تتراوح مدة المقاطع العادية بين 30 ثانية و 150 ثانية (دقيقتين ونصف). أما للمواقف والقصص والمقالب (Story / Narrative): يمنع قطع القصة في منتصفها ويجب استمرار المقطع حتى اكتمال القفلة والخاتمة بالكامل، ويُسمح بالامتداد خصيصاً للقصص والمواقف حتى 210 ثانية (3 دقائق ونصف كحد أقصى) لضمان اكتمال الحكاية دون بتر.\n\n"
            )
            if req.customPrompt and req.customPrompt.strip():
                prompt += f"⚠️ توجيهات إضافية مخصصة من المستخدم (يجب الالتزام بها بصرامة):\n{req.customPrompt.strip()}\n\n"

            prompt += f"النص المفرغ المراد تحليله:\n{req.transcription}"

            response = model.generate_content(prompt)

            import json
            shorts_data = json.loads(response.text)
            shorts_list = shorts_data.get("shorts", [])

        max_secs = get_max_transcription_seconds(req.transcription)
        for s in shorts_list:
            s["start_time"] = normalize_time_str(s.get("start_time", "00:00:00"), max_secs)
            s["end_time"] = normalize_time_str(s.get("end_time", "00:00:00"), max_secs)
            s["title"] = enforce_title_style(s.get("title", ""), req.titleStyle)

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


# ==================== Horizontal to Vertical (9:16) Conversion (KIM Algorithm) ====================

def convert_video_to_vertical(video_path: str, output_path: str, progress_callback=None):
    import cv2
    import numpy as np
    import mediapipe as mp
    import subprocess
    from scenedetect import detect, ContentDetector

    if progress_callback:
        progress_callback("🎬 جاري فتح وتحليل بيانات الفيديو الأصلي...")

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise Exception("فشل فتح ملف الفيديو.")

    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    W = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    H = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    if W <= 0 or H <= 0 or total_frames <= 0:
        cap.release()
        raise Exception("بيانات أبعاد الفيديو أو عدد الفريمات غير صالحة.")

    target_w = int(H * 9 / 16)
    if target_w % 2 != 0:
        target_w += 1
    if target_w > W:
        target_w = W if W % 2 == 0 else W - 1

    out_h = H if H % 2 == 0 else H - 1

    if progress_callback:
        progress_callback("🔍 جاري تحليل وتحديد المشاهد (Scene Detection)...")

    scene_list = detect(video_path, ContentDetector(threshold=27.0))
    raw_cuts = [0] + [s[1].get_frames() for s in scene_list]
    if not raw_cuts or raw_cuts[-1] < total_frames:
        raw_cuts.append(total_frames)

    min_len = max(int(fps * 0.5), 1)
    segments = []
    start = raw_cuts[0]
    for cut in raw_cuts[1:]:
        if cut - start >= min_len:
            segments.append((start, cut))
            start = cut
    if start < total_frames:
        if segments:
            segments[-1] = (segments[-1][0], total_frames)
        else:
            segments = [(0, total_frames)]

    if progress_callback:
        progress_callback(f"👥 جاري تحليل الوجوه وتحديد الكادر لـ {len(segments)} مشهد...")

    mp_face = mp.solutions.face_detection
    detector = mp_face.FaceDetection(model_selection=1, min_detection_confidence=0.5)

    def sample_scene_faces(start_f, end_f):
        boxes = []
        dur = (end_f - start_f) / fps
        n = int(max(5, min(15, dur)))
        idxs = [start_f + int((end_f - start_f) * t) for t in np.linspace(0.05, 0.95, n)]
        for idx in idxs:
            cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
            ok, frame = cap.read()
            if not ok or frame is None:
                continue
            res = detector.process(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            if res.detections:
                for d in res.detections:
                    bb = d.location_data.relative_bounding_box
                    x1 = max(0, int(bb.xmin * W))
                    x2 = min(W, int((bb.xmin + bb.width) * W))
                    if (x2 - x1) > W * 0.04:
                        boxes.append((x1, x2))
        return boxes

    def compute_x1(boxes):
        if not boxes:
            return (W - target_w) // 2
        leftmost = min(a for a, _ in boxes)
        rightmost = max(b for _, b in boxes)
        centers = np.array([(a + b) / 2 for a, b in boxes])
        anchor = float(np.median(centers))

        if (rightmost - leftmost) <= target_w * 0.98:
            x1 = (leftmost + rightmost) / 2 - target_w / 2
        else:
            x1 = anchor - target_w / 2
        return int(max(0, min(x1, W - target_w)))

    scene_x1 = [compute_x1(sample_scene_faces(a, b)) for a, b in segments]
    detector.close()

    if progress_callback:
        progress_callback("⚡ جاري اقتصاص وقص الفيديو طولي (9:16) ومعالجة الإطارات...")

    cmd = [
        "ffmpeg", "-y",
        "-f", "rawvideo", "-pix_fmt", "bgr24",
        "-s", f"{target_w}x{out_h}", "-r", str(fps),
        "-i", "-",
        "-i", video_path,
        "-map", "0:v", "-map", "1:a?",
        "-c:v", "libx264", "-preset", "fast", "-crf", "20",
        "-pix_fmt", "yuv420p", "-profile:v", "high",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        "-shortest", output_path
    ]

    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
    seg_i, frame_i = 0, 0
    while True:
        ok, frame = cap.read()
        if not ok or frame is None:
            break
        while seg_i + 1 < len(segments) and frame_i >= segments[seg_i][1]:
            seg_i += 1
        x1 = scene_x1[seg_i]
        crop = frame[0:out_h, x1:x1 + target_w]
        proc.stdin.write(crop.tobytes())
        frame_i += 1
        if frame_i % 150 == 0 and progress_callback:
            percent = int((frame_i / total_frames) * 100)
            progress_callback(f"🎬 جاري معالجة الإطارات: {frame_i}/{total_frames} ({percent}%)...")

    proc.stdin.close()
    proc.wait()
    cap.release()


def run_convert_vertical_background(task_id: str, video_path: str, youtube_url: str, task_dir: str):
    if len(TASKS) > 200:
        keys_to_remove = list(TASKS.keys())[:50]
        for k in keys_to_remove:
            TASKS.pop(k, None)

    try:
        TASKS[task_id] = {"status": "processing", "progress": "🎬 جاري بدء معالجة الفيديو..."}

        # Download from YouTube if URL provided
        if youtube_url and not video_path:
            video_path = os.path.join(task_dir, "input_yt.mp4")
            TASKS[task_id]["progress"] = "📥 جاري تنزيل فيديو يوتيوب الأصلي..."
            ytdl_cmd = [
                'yt-dlp',
                '--quiet', '--no-warnings',
                '--no-playlist',
                '-f', 'best[height<=720]/best',
                '--merge-output-format', 'mp4',
                '-o', video_path
            ]
            if os.path.exists(COOKIE_FILE_PATH) and os.path.getsize(COOKIE_FILE_PATH) > 0:
                ytdl_cmd.extend(['--cookies', COOKIE_FILE_PATH])
            ytdl_cmd.append(youtube_url)
            res = subprocess.run(ytdl_cmd, capture_output=True, text=True)
            if res.returncode != 0 or not os.path.exists(video_path):
                raise Exception(f"فشل تنزيل فيديو يوتيوب: {res.stderr.strip() if res.stderr else 'Unknown error'}")

        output_path = os.path.join(task_dir, "vertical_tiktok.mp4")

        def update_progress(msg: str):
            if task_id in TASKS:
                TASKS[task_id]["progress"] = msg

        convert_video_to_vertical(video_path, output_path, update_progress)

        if not os.path.exists(output_path):
            raise Exception("لم يتم توليد ملف الفيديو الطولي الناتج.")

        video_url = f"public/temp_{task_id}/vertical_tiktok.mp4"
        TASKS[task_id] = {
            "status": "success",
            "progress": "✅ تم تحويل الفيديو إلى طولي بنجاح!",
            "videoUrl": video_url
        }
        print(f"[{task_id}] Vertical conversion completed: {video_url}", flush=True)

    except Exception as e:
        print(f"[{task_id}] Vertical conversion failed: {e}", flush=True)
        TASKS[task_id] = {
            "status": "failed",
            "error": str(e)
        }


from fastapi import File, UploadFile, Form

@app.post("/api/convert-vertical-async")
async def convert_vertical_async(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(None),
    youtubeUrl: str = Form(None)
):
    if not file and not youtubeUrl:
        raise HTTPException(400, "يجب تحديد ملف فيديو للرفع أو إدخال رابط يوتيوب.")

    task_id = str(uuid.uuid4())
    task_dir = os.path.join(PUBLIC_DIR, f"temp_{task_id}")
    os.makedirs(task_dir, exist_ok=True)

    video_path = None
    if file:
        video_path = os.path.join(task_dir, f"input_{file.filename}")
        with open(video_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

    TASKS[task_id] = {"status": "processing", "progress": "جاري التحضير لتحويل الفيديو إلى طولي..."}
    background_tasks.add_task(run_convert_vertical_background, task_id, video_path, youtubeUrl, task_dir)

    return {"status": "processing", "taskId": task_id}


