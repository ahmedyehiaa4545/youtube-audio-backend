import os
import re
import uuid
import shutil
import asyncio
import time
import requests
import concurrent.futures
from fastapi import FastAPI, HTTPException, BackgroundTasks, UploadFile, File
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
            return
        except Exception as e:
            print(f"⚠️ Failed to write cookies from env: {e}", flush=True)

    # Check local cookies.txt from repo in current dir, script dir, or parent dir
    possible_cookie_paths = [
        "cookies.txt",
        os.path.join(os.path.dirname(__file__), "cookies.txt"),
        os.path.join(os.path.dirname(__file__), "..", "cookies.txt"),
        "/app/cookies.txt",
        "/app/youtube-audio-backend/cookies.txt"
    ]
    for p in possible_cookie_paths:
        if os.path.exists(p) and os.path.getsize(p) > 0:
            try:
                shutil.copy(p, COOKIE_FILE_PATH)
                print(f"🔑 cookies.txt copied from {p} to {COOKIE_FILE_PATH}.", flush=True)
                return
            except Exception as e:
                print(f"⚠️ Failed to copy {p}: {e}", flush=True)

    print("⚠️ Warning: No cookies.txt found in repository or env variable!", flush=True)

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
    groqApiKey: str | None = None


class ShortSuggestion(BaseModel):
    title: str = Field(description="عنوان جذاب ومثير للمقطع القصير")
    category: str | None = Field(default="🎯 قصة وخلاصة مكتملة", description="تصنيف نوع المقطع: '🎯 قصة وخلاصة مكتملة' أو '💡 سر ونصيحة ذهبية' أو '🔥 موقف درامي / صدمة' أو '⚡ مقطع تشويقي'")
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
    openrouterModel: str | None = "google/gemini-3.5-flash-lite"
    customPrompt: str | None = None
    titleStyle: str | None = "auto"
    numShorts: int = 3

class CutRequest(BaseModel):
    url: str
    start_time: str
    end_time: str
    quality: int = 1080

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

def snap_short_timestamps_to_sentences(transcription: str, start_time: str, end_time: str) -> tuple[str, str]:
    """
    محاذاة توقيت البداية والنهاية تلقائياً لأقرب حدود جملة طبيعية ومكتملة،
    لمنع قطع الكلام في منتصف الجملة أو قبل اكتمال قفلة الفكرة.
    """
    try:
        start_sec = parse_time_to_seconds(start_time)
        end_sec = parse_time_to_seconds(end_time)
    except Exception:
        return start_time, end_time

    segments = parse_transcription_segments(transcription)
    if not segments:
        return start_time, end_time

    # 1. محاذاة البداية لأول جملة تبدأ عند أو قبل start_sec بقليل
    snapped_start = start_sec
    for seg in segments:
        if seg["start"] <= start_sec <= seg["end"] or (abs(seg["start"] - start_sec) <= 2.0):
            snapped_start = seg["start"]
            break

    # 2. محاذاة النهاية لآخر جملة كاملة حتى لا يتم قطع المتحدث قبل أن يختم كلمته
    snapped_end = end_sec
    matching_segs = [s for s in segments if s["start"] >= (snapped_start - 0.5) and s["start"] < end_sec]
    if matching_segs:
        last_seg = matching_segs[-1]
        # إذا كانت الجملة الأخيرة تمتد قليلاً بعد end_sec (حتى 5 ثوانٍ)، نمد النهاية حتى تكتمل الجملة بالكامل
        if last_seg["end"] >= end_sec:
            snapped_end = last_seg["end"]
        elif (end_sec - last_seg["end"]) < 3.0:
            snapped_end = last_seg["end"]
        else:
            snapped_end = max(end_sec, last_seg["end"])

    def format_secs(s: float) -> str:
        s = max(0, s)
        h = int(s // 3600)
        m = int((s % 3600) // 60)
        sec = int(s % 60)
        if h > 0:
            return f"{h:02d}:{m:02d}:{sec:02d}"
        else:
            return f"{m:02d}:{sec:02d}"

    return format_secs(snapped_start), format_secs(snapped_end)

def rebuild_script_for_short(transcription: str, start_time: str, end_time: str, fallback_script: str) -> str:
    try:
        start_sec = parse_time_to_seconds(start_time)
        end_sec = parse_time_to_seconds(end_time)
    except Exception:
        return fallback_script

    segments = parse_transcription_segments(transcription)
    matching_texts = []
    
    for seg in segments:
        if seg["start"] < (end_sec + 0.1) and seg["end"] > (start_sec - 0.1):
            matching_texts.append(seg["text"])
            
    rebuilt = " ".join(matching_texts).strip()
    return rebuilt if rebuilt else fallback_script

def extract_video_id(url: str) -> str | None:
    pattern = r'(?:https?:\/\/)?(?:www\.)?(?:youtube\.com\/(?:[^\/\n\s]+\/\S+\/|(?:v|e(?:mbed)?)\/|\S*?[?&]v=)|youtu\.be\/)([a-zA-Z0-9_-]{11})'
    match = re.search(pattern, url)
    return match.group(1) if match else None

def download_audio_smart(youtube_url: str, output_path: str, task_id: str = None) -> str:
    """
    تحميل الصوت مباشرة وسريعاً من يوتيوب عبر مكتبة yt_dlp الأصلية مع عملاء البث المعتمدين والكوكيز
    """
    import yt_dlp

    def update_task(msg):
        if task_id and task_id in TASKS:
            TASKS[task_id]["progress"] = msg

    update_task("⚡ جاري استخراج وتحميل الصوت من يوتيوب...")
    print(f"⚡ Downloading audio directly via yt-dlp for {youtube_url}...", flush=True)

    output_base = output_path.replace(".mp3", "")

    # إعدادات السحب الأصلية المعززة بعملاء البث والكوكيز
    opts = {
        "format": "bestaudio/best",
        "outtmpl": output_base,
        "extractor_args": {"youtube": {"player_client": ["android_vr", "ios", "mweb", "android"]}},
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
        "quiet": True,
        "noprogress": True,
        "socket_timeout": 30,
        "concurrent_fragment_downloads": 5,
    }

    if os.path.exists(COOKIE_FILE_PATH) and os.path.getsize(COOKIE_FILE_PATH) > 0:
        opts["cookiefile"] = COOKIE_FILE_PATH

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([youtube_url])
    except Exception as ydl_err:
        print(f"⚠️ Primary yt_dlp download notice: {ydl_err}", flush=True)

    # التحقق من وجود ملف الـ MP3 الناتج
    if os.path.exists(output_path) and os.path.getsize(output_path) > 1000:
        print(f"🎉 Audio extracted successfully: {output_path} ({os.path.getsize(output_path)/(1024*1024):.2f}MB)", flush=True)
        return output_path

    # البحث عن أي ملف صوتي تم إنشاؤه في المجلد
    parent_dir = os.path.dirname(output_path)
    base_name = os.path.basename(output_base)
    for f in os.listdir(parent_dir):
        if f.startswith(base_name) and f.endswith(".mp3") and os.path.getsize(os.path.join(parent_dir, f)) > 1000:
            found_path = os.path.join(parent_dir, f)
            if found_path != output_path:
                try: os.rename(found_path, output_path)
                except: pass
            print(f"🎉 Audio extracted successfully: {output_path}", flush=True)
            return output_path

    raise Exception("فشل استخراج ملف الصوت من يوتيوب. يرجى التأكد من الرابط أو المحاولة مرة أخرى.")

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
            
    final_text = "\n".join(chunks_results).strip()
    if not final_text:
        raise Exception("فشلت عملية التفريغ (لم يتم استخراج أي نص).")
    return final_text

def transcribe_audio_with_groq(audio_path: str, groq_api_key: str, chunk_minutes: int = 10, task_id: str = None) -> str:
    """
    Transcribes YouTube audio using Groq's whisper-large-v3-turbo API ultra-fast,
    splitting audio into chunks and processing them IN PARALLEL, then concatenates results.
    """
    total_start = time.time()
    print(f"⚡ Transcribing full YouTube audio with Groq whisper-large-v3-turbo (PARALLEL)...", flush=True)
    if task_id and task_id in TASKS:
        TASKS[task_id]["progress"] = "⚡ جاري التفريغ النصي الذكي والسريع..."

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
    subprocess.run(split_cmd, capture_output=True)

    import glob
    chunk_files = glob.glob(os.path.join(dir_name, "chunk_*.mp3"))

    def get_chunk_idx(filepath):
        try:
            return int(os.path.basename(filepath).split('_')[1].split('.')[0])
        except:
            return 9999

    chunk_files.sort(key=get_chunk_idx)
    if not chunk_files:
        chunk_files = [audio_path]

    total_chunks = len(chunk_files)
    chunks_results = [""] * total_chunks
    completed_count = 0

    def process_groq_chunk(args):
        nonlocal completed_count
        idx, c_path = args
        offset_seconds = idx * segment_time_sec
        chunk_start = time.time()
        lines = []
        try:
            print(f"[*] Chunk {idx+1}/{total_chunks}: sending to Groq...", flush=True)
            headers = {"Authorization": f"Bearer {groq_api_key.strip()}"}
            with open(c_path, "rb") as f:
                files = {"file": (os.path.basename(c_path), f, "audio/mpeg")}
                data = {
                    "model": "whisper-large-v3-turbo",
                    "response_format": "verbose_json"
                }
                resp = requests.post(
                    "https://api.groq.com/openai/v1/audio/transcriptions",
                    headers=headers, files=files, data=data, timeout=120
                )
                resp.raise_for_status()
                res_dict = resp.json()

            segments = res_dict.get("segments", [])
            for seg in segments:
                s_start = float(seg.get("start", 0.0)) + offset_seconds
                s_end   = float(seg.get("end",   0.0)) + offset_seconds
                text    = seg.get("text", "").strip()
                if text:
                    h1, m1, s1 = int(s_start // 3600), int((s_start % 3600) // 60), int(s_start % 60)
                    h2, m2, s2 = int(s_end   // 3600), int((s_end   % 3600) // 60), int(s_end   % 60)
                    t1 = f"{h1:02d}:{m1:02d}:{s1:02d}" if h1 > 0 else f"{m1:02d}:{s1:02d}"
                    t2 = f"{h2:02d}:{m2:02d}:{s2:02d}" if h2 > 0 else f"{m2:02d}:{s2:02d}"
                    lines.append(f"[{t1} -> {t2}] {text}")

            chunk_elapsed = round(time.time() - chunk_start, 1)
            completed_count += 1
            msg = f"تم تفريغ الجزء {completed_count}/{total_chunks} ({chunk_elapsed}s)"
            print(f"✅ Chunk {idx+1}/{total_chunks} done in {chunk_elapsed}s", flush=True)
            if task_id and task_id in TASKS:
                TASKS[task_id]["progress"] = msg

        except Exception as c_err:
            chunk_elapsed = round(time.time() - chunk_start, 1)
            print(f"⚠️ Groq chunk {idx+1} error after {chunk_elapsed}s: {c_err}", flush=True)
        finally:
            try:
                if c_path != audio_path and os.path.exists(c_path):
                    os.remove(c_path)
            except:
                pass

        return idx, lines

    # Run all chunks in parallel
    max_workers = min(6, total_chunks)
    print(f"🔀 Running {total_chunks} chunks in parallel (max_workers={max_workers})...", flush=True)

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(process_groq_chunk, (idx, cp)) for idx, cp in enumerate(chunk_files)]
        for future in concurrent.futures.as_completed(futures):
            idx, lines = future.result()
            chunks_results[idx] = lines

    full_transcription_lines = []
    for lines in chunks_results:
        full_transcription_lines.extend(lines)

    total_elapsed = round(time.time() - total_start, 1)
    print(f"🏁 Groq full transcription done in {total_elapsed}s total ({total_chunks} chunks parallel)", flush=True)
    if task_id and task_id in TASKS:
        TASKS[task_id]["progress"] = f"✅ اكتمل التفريغ في {total_elapsed} ثانية!"

    result_text = "\n".join(full_transcription_lines).strip()
    if not result_text:
        raise Exception("فشلت عملية التفريغ بـ Groq (قد يكون المفتاح غير صالح أو انتهت الحصة).")
    return result_text


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

def run_transcription_background(task_id: str, youtube_url: str, gemini_api_key: str, groq_api_key: str, task_dir: str):
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
            
        if groq_api_key and groq_api_key.strip():
            print(f"[{task_id}] Background: Transcribing audio with Groq Whisper Large V3 Turbo...", flush=True)
            TASKS[task_id]["progress"] = "⚡ جاري التفريغ النصي الذكي والسريع..."
            transcription_text = transcribe_audio_with_groq(
                audio_path=audio_path,
                groq_api_key=groq_api_key.strip(),
                task_id=task_id
            )
        else:
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
            "progress": "اكتمل بنجاح!",
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
    has_groq = req.groqApiKey and req.groqApiKey.strip() not in ["", "none", "null"]
    has_gemini = req.geminiApiKey and req.geminiApiKey.strip() not in ["", "none", "null"]
    
    if not has_groq and not has_gemini:
        raise HTTPException(status_code=400, detail="يرجى إدخال مفتاح الذكاء الاصطناعي لتفريغ الصوت.")
        
    task_id = str(uuid.uuid4())
    task_dir = os.path.join(PUBLIC_DIR, f"temp_{task_id}")
    os.makedirs(task_dir, exist_ok=True)
    
    TASKS[task_id] = {
        "status": "processing",
        "progress": "جاري بدء المهمة...",
        "audioUrl": None,
        "transcription": None,
        "error": None
    }
    
    background_tasks.add_task(
        run_transcription_background, 
        task_id, 
        req.youtubeUrl, 
        req.geminiApiKey,
        req.groqApiKey,
        task_dir
    )
    
    background_tasks.add_task(schedule_dir_cleanup, task_dir, 1200)
    
    return {
        "status": "queued",
        "taskId": task_id
    }


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

def fix_arabic_spelling(text: str) -> str:
    if not text:
        return ""
    import re
    t = text.strip().strip('"\'`')
    
    # Correction for common ASR elongation mistakes on verbs and words
    asr_verb_fixes = {
        r"\bترضاع(\w*)\b": r"ترضع\1",
        r"\bيرضاع(\w*)\b": r"يرضع\1",
        r"\bتولاد(\w*)\b": r"تلد\1",
        r"\bيولاد(\w*)\b": r"يلد\1",
        r"\bتتعلام(\w*)\b": r"تتعلم\1",
        r"\bيتعلام(\w*)\b": r"يتعلم\1",
        r"\bتتكلموا\b": "تتكلمون",
    }
    for pat, rep in asr_verb_fixes.items():
        t = re.sub(pat, rep, t)
    
    # Common Tanween Adverbs
    tanween_map = {
        r"\bشكرا\b": "شكراً",
        r"\bجدا\b": "جداً",
        r"\bفعلا\b": "فعلاً",
        r"\bحقا\b": "حقاً",
        r"\bمثلا\b": "مثلاً",
        r"\bطبعا\b": "طبعاً",
        r"\bدائما\b": "دائماً",
        r"\bغالبا\b": "غالباً",
        r"\bتقريبا\b": "تقريباً",
        r"\bتماما\b": "تماماً",
        r"\bفورا\b": "فوراً",
        r"\bأهلا\b": "أهلاً",
        r"\bاهلا\b": "أهلاً",
        r"\bمرحبا\b": "مرحباً",
        r"\bأبدا\b": "أبداً",
        r"\bابدا\b": "أبداً",
        r"\bمجددا\b": "مجدداً",
        r"\bمسبقا\b": "مسبقاً",
        r"\bخصوصا\b": "خصوصاً",
        r"\bعموما\b": "عموماً",
        r"\bأحيانا\b": "أحياناً",
        r"\bاحيانا\b": "أحياناً",
        r"\bأولا\b": "أولاً",
        r"\bاولا\b": "أولاً",
        r"\bثانيا\b": "ثانياً",
        r"\bثالثا\b": "ثالثاً",
        r"\bعاما\b": "عاماً",
        r"\bيوما\b": "يوماً",
        r"\bشهرا\b": "شهراً",
        r"\bوقتا\b": "وقتاً",
        r"\bشيئا\b": "شيئاً",
        r"\bريالا\b": "ريالاً",
        r"\bجزءا\b": "جزءاً",
    }
    for pat, rep in tanween_map.items():
        t = re.sub(pat, rep, t)
    return t

def enforce_title_style(title: str, style: str) -> str:
    if not title or not isinstance(title, str):
        return title
    
    clean_title = fix_arabic_spelling(title.strip())
    
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

def call_openrouter_shorts(transcription: str, num_shorts: int, api_key: str, model_name: str = "google/gemini-3.5-flash-lite", custom_prompt: str = None, title_style: str = "auto"):
    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://rekaption.com",
        "X-Title": "ReKaption"
    }
    system_prompt = (
        "أنت خبير ومخرج ومونتير محترف في صناعة واقتطاع المحتوى الفيروسي الأكثر انتشاراً وتأثيراً على TikTok و Shorts و Reels (Viral Content Creator & Master Storyteller).\n"
        "مهمتك الأساسية والجوهرية: تحليل النص المفرغ واستخراج مقاطع قصيرة (Shorts) مميزة ومكتملة البنية تماماً (بداية مشوقة + سياق قوي + كشف السر/الإجابة + قفلة ختامية منطقية ومقنعة 100%).\n"
        "\n"
        "⚠️ قواعد حاسمة لمنع المقاطع المبتورة والقفلات الخائبة:\n"
        "1. ممنوع منعاً باتاً استخراج 'مقدمة الفيديو التشويقية' فقط (No Intro-Only Teasers): يجب أن يشمل المقطع الشرح وكشف السر والخاتمة بالكامل.\n"
        "2. اكتمال الوعد والسر المطروح في العنوان (Payoff Guarantee): يجب أن يحتوي سكربت المقطع المقصوص على شرح السر والنتيجة كاملة.\n"
        "3. اكتمال المعنى والنتيجة: يجب أن ينتهي المقطع دائماً عند نهاية جملة تامة مكتملة الأركان.\n"
        "4. تصنيف نوع المقطع (category): صنف كل مقطع (🎯 قصة وخلاصة مكتملة / 💡 سر ونصيحة ذهبية / 🔥 موقف درامي / صدمة / ⚡ مقطع تشويقي).\n"
        "\n"
        "⚠️ شروط العنوان وتصحيح الأخطاء اللغوية الإلزامية:\n"
        "1. التصحيح اللغوي والنحوي الإجباري (Mandatory Grammar & Spelling Correction): يُمنع منعاً باتاً نقل الأخطاء الإملائية أو اللغوية الناتجة عن التفريغ الصوتي (مثل: مد الحروف كـ 'ترضاعها' التي يجب تصحيحها حتماً إلى 'تُرضِعها'). يجب صياغة العنوان بلغة عربية فصيحة وسليمة نحوياً وإملائياً 100%.\n"
        "2. الارتباط العميق بسكربت هذا المقطع تحديداً وإثارة الفضول القاتل (Curiosity Hook).\n"
        "3. السلامة الإملائية التامة للهمزات والتاء المربوطة وتنوين الفتح (شكراً، جداً، عاماً).\n"
        "يجب أن تكون إجابتك بصيغة JSON فقط بالتنسيق التالي:\n"
        "{\n"
        '  "shorts": [\n'
        '    {\n'
        '      "title": "عنوان جريء ومثير ومصحح نحوياً وإملائياً 100%",\n'
        '      "category": "🎯 قصة وخلاصة مكتملة",\n'
        '      "start_time": "05:47",\n'
        '      "end_time": "06:50",\n'
        '      "script": "النص الكامل للمقطع القصير من البداية ومروراً بشرح السر وحتى القفلة والنتيجة الكاملة",\n'
        '      "hook": "الجملة الافتتاحية في أول 3 ثواني"\n'
        '    }\n'
        '  ]\n'
        "}\n"
    )
    
    title_instruction = (
        "5. صياغة عنوان فيروسي خاص ومحدد بدقة ومصحح لغوياً 100%:\n"
        "   - افهم الحوار والمفارقة التي حدثت في هذا المقطع تحديداً واجعله جذاباً.\n"
        "   - التدقيق النحوي والإملائي الإلزامي: صحح أي أخطاء لغوية أو نطقية واردة في التفريغ وتأكد من سلامة تصريف الأفعال والهمزات والتنوين تماماً."
    )
    if title_style == "short":
        title_instruction = (
            "5. صياغة عنوان فيروسي جريء ومحدد بدقة لهذا المقطع وقصير من 2 إلى 4 كلمات فقط (حد أقصى 5 كلمات) يثير الفضول والتشويق القاتل، وخالٍ 100% من الأخطاء الإملائية."
        )
    elif title_style == "medium":
        title_instruction = (
            "5. صياغة عنوان فيروسي حماسي وجريء من 5 إلى 9 كلمات، يلتقط الجزء الأكثر إثارة من حكاية هذا المقطع تحديداً بدقة إملائية خالية تماماً من الأخطاء."
        )

    user_prompt = (
        f"قم بتحليل النص المفرغ التالي واستخرج أفضل {num_shorts} مقاطع قصيرة (Shorts) مميزة ومكتملة الفكرة تماماً.\n\n"
        "شروط استخراج كل مقطع:\n"
        "1. يجب أن تكون البداية والنهاية مستندة بدقة إلى التوقيتات الموجودة في النص المرفق (مثال: 05:47 أو 12:30).\n"
        "2. مدة المقطع واكتمال الحكاية/السر: تتراوح مدة المقاطع بين 30 ثانية و 180 ثانية (3 دقائق كحد أقصى). ممنوع منعاً باتاً قطع المقطع قبل كشف السر أو شرح الفكرة أو إتمام القصة بالكامل. إذا طرح المقطع سؤالاً أو لغزاً، مدّ التوقيت حتى اكتمال الإجابة والتفسير والنهاية.\n"
        "3. ممنوع استخراج مقدمات الفيديوهات الطويلة التي تحيل المشاهد لإكمال الفيديو أو تنتهي قبل شرح الموضوع.\n"
        "4. تحديد الخطاف (Hook) في أول 3 ثوانٍ فقط (جملة افتتاحية قصيرة من 3 إلى 7 كلمات لشد الانتباه).\n"
        "5. كتابة السكربت كاملاً من بداية المقطع وحتى ختامه التام بدقة كما ورد في النص.\n"
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
    
    max_attempts = 4
    last_err = None
    for attempt in range(1, max_attempts + 1):
        try:
            print(f"⚡ OpenRouter suggest_shorts attempt {attempt}/{max_attempts} with model: {model_name}...", flush=True)
            res = requests.post(url, headers=headers, json=payload, timeout=90)
            if res.status_code != 200:
                raise Exception(f"OpenRouter API error (status {res.status_code}): {res.text}")
            
            data = res.json()
            choices = data.get('choices', [])
            if not choices:
                raise Exception("OpenRouter returned no choices.")
                
            content = choices[0].get('message', {}).get('content', '')
            if not content or not content.strip():
                raise Exception("OpenRouter returned empty content.")
                
            import json
            parsed = json.loads(content.strip())
            if isinstance(parsed, dict) and "shorts" in parsed and len(parsed["shorts"]) > 0:
                print(f"✨ OpenRouter suggest_shorts succeeded on attempt {attempt} with model {model_name}!", flush=True)
                return parsed
            else:
                raise Exception("OpenRouter JSON missing 'shorts' array or empty shorts.")
        except Exception as e:
            last_err = e
            print(f"⚠️ OpenRouter suggest_shorts attempt {attempt}/{max_attempts} failed ({e}). Retrying same model...", flush=True)
            time.sleep(1.5)

    raise Exception(f"OpenRouter failed after {max_attempts} retries with model {model_name}: {last_err}")

def extract_single_sentence_hook(script_text: str) -> str:
    if not script_text:
        return ""
    # Split by Arabic punctuation: periods, exclamation, question marks, commas, semicolons, or newlines
    clauses = [c.strip() for c in re.split(r'[.!\?\n،؛]', script_text) if c.strip()]
    first_clause = clauses[0] if clauses else script_text
    # If the first clause is still too long (more than 7 words), keep only the first 6 words to represent a quick 3-second hook
    words = first_clause.split()
    if len(words) > 7:
        return " ".join(words[:6]) + "..."
    return first_clause

def extract_two_sentence_summary(script_text: str) -> str:
    if not script_text:
        return ""
    clauses = [c.strip() for c in re.split(r'[.!\?\n،؛]', script_text) if c.strip()]
    if len(clauses) >= 2:
        return f"{clauses[0]}.. {clauses[1]}."
    elif len(clauses) == 1:
        return f"{clauses[0]}."
    return script_text[:120]

@app.post("/api/suggest-shorts")
async def suggest_shorts(req: SuggestShortsRequest):
    if not req.transcription or req.transcription.strip() == "":
        raise HTTPException(status_code=400, detail="Transcription content is empty.")
    
    openrouter_key = req.openrouterApiKey or os.environ.get("OPENROUTER_API_KEY")
    openrouter_model = req.openrouterModel if (req.openrouterModel and req.openrouterModel.strip()) else "google/gemini-3.5-flash-lite"
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
        raw_start = normalize_time_str(s.get("start_time", "00:00:00"), max_secs)
        raw_end = normalize_time_str(s.get("end_time", "00:00:00"), max_secs)

        # محاذاة البداية والنهاية لحدود الجمل الطبيعية لمنع أي قطع في منتصف الكلام أو قبل القفلة
        snapped_start, snapped_end = snap_short_timestamps_to_sentences(req.transcription, raw_start, raw_end)
        s["start_time"] = snapped_start
        s["end_time"] = snapped_end
        s["category"] = s.get("category") or "🎯 قصة وخلاصة مكتملة"

        s["title"] = enforce_title_style(s.get("title", ""), req.titleStyle)
        s["script"] = rebuild_script_for_short(
            transcription=req.transcription,
            start_time=s["start_time"],
            end_time=s["end_time"],
            fallback_script=s.get("script", "")
        )
        s["hook"] = extract_single_sentence_hook(s.get("script", ""))

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
        openrouter_model = req.openrouterModel if (req.openrouterModel and req.openrouterModel.strip()) else "google/gemini-3.5-flash-lite"
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
                "أنت خبير ومخرج ومونتير محترف في صناعة واقتطاع المحتوى الفيروسي (Viral Content Creator & Master Storyteller).\n"
                f"قم بتحليل النص المفرغ التالي واستخرج منه أفضل {req.numShorts} مقاطع قصيرة مميزة ومكتملة الفكرة تماماً.\n"
                "⚠️ قواعد حاسمة لاكتمال القصة والسر (ممنوع المقاطع المبتورة نهائياً):\n"
                "1. ممنوع منعاً باتاً استخراج 'مقدمة الفيديو فقط' (No Intro Teasers): يُمنع قص مقطع ينتهي بعبارات مثل 'وده اللي هنتكلم عنه في الفيديو ده' أو 'تعالوا نشوف إيه اللي حصل' دون أن يحتوي المقطع على الشرح والسر نفسه! يجب إطالة التوقيت ليشمل كشف السر والتفسير الفعلي والخاتمة بالكامل.\n"
                "2. اكتمال الوعد المطروح في العنوان: إذا تحدث العنوان عن سر أو خدعة أو صدمة، يجب أن يحتوي سكربت الشورتس على شرح وتفاصيل ذلك السر والنتيجة كاملة حتى لا يشعر المشاهد بالبتر.\n"
                "3. القفلة الختامية: يجب أن ينتهي المقطع دائماً عند نهاية جملة تامة مكتملة الأركان (النتيجة، العبرة، أو خلاصة الفكرة).\n\n"
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
            raw_start = normalize_time_str(s.get("start_time", "00:00:00"), max_secs)
            raw_end = normalize_time_str(s.get("end_time", "00:00:00"), max_secs)

            # محاذاة البداية والنهاية لحدود الجمل الطبيعية
            snapped_start, snapped_end = snap_short_timestamps_to_sentences(req.transcription, raw_start, raw_end)
            s["start_time"] = snapped_start
            s["end_time"] = snapped_end
            s["category"] = s.get("category") or "🎯 قصة وخلاصة مكتملة"

            s["title"] = enforce_title_style(s.get("title", ""), req.titleStyle)

            s["script"] = rebuild_script_for_short(
                transcription=req.transcription,
                start_time=s["start_time"],
                end_time=s["end_time"],
                fallback_script=s.get("script", "")
            )

            s["hook"] = extract_single_sentence_hook(s.get("script", ""))

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

def build_ffmpeg_http_headers(format_dict=None) -> str:
    """Construct HTTP headers string for FFmpeg to access googlevideo streams directly"""
    headers = format_dict.get('http_headers', {}) if format_dict else {}
    header_str = ""
    for k, v in headers.items():
        if k.lower() == 'referer':
            continue
        header_str += f"{k}: {v}\r\n"
    
    # Enforce Referer header for googlevideo streams to bypass 403 Forbidden
    if "Referer:" not in header_str:
        header_str += "Referer: https://www.youtube.com/\r\n"
    
    # Ensure User-Agent is present
    if "User-Agent" not in header_str and "user-agent" not in header_str.lower():
        header_str += "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36\r\n"
    
    # Append Cookie header manually from file if present in the tmp folder
    cookie_str = get_cookie_header_from_file(COOKIE_FILE_PATH)
    if cookie_str and "Cookie:" not in header_str:
        header_str += f"Cookie: {cookie_str}\r\n"
        
    return header_str

def get_ffmpeg_headers(format_dict) -> str:
    return build_ffmpeg_http_headers(format_dict)

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

def cut_segment_fast(url: str, start_sec: float, end_sec: float, quality: int, output_path: str, progress_callback=None) -> str:
    """
    محرك القص الذكي ثلاثي المراحل (3-Tier Robust Cut Engine):
    1. محاولة السحب بأعلى جودة (1080p/720p HD) عبر الكوكيز والاتصال المتوازي مع مهلة ديناميكية.
    2. محرك الإنقاذ فائق السرعة مع الكوكيز والبث المباشر (Single-Stream Fast Direct Extraction).
    3. المحرك النهائي البديل مع الكوكيز.
    """
    if progress_callback:
        progress_callback("💎 جاري استخراج وقص المقطع بأعلى دقة متاحة...")

    clip_len = max(1.0, end_sec - start_sec)
    end_extension = 0.75
    fade_in_duration = 0.2
    fade_out_duration = 0.75
    start_fade_out = clip_len

    start_time_str = format_seconds_to_time_str(start_sec)
    extended_end_time_str = format_seconds_to_time_str(end_sec + end_extension)
    temp_raw = output_path + ".raw.mp4"

    # مهلة سخية تضمن اكتمال تنزيل الـ 1080p بدون قطع الاتصال
    timeout_tier1 = max(360, int(clip_len * 3.5) + 180)
    timeout_rescue = max(240, int(clip_len * 2.5) + 120)

    target_format_hd = f"bestvideo[height<={quality}][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<={quality}]+bestaudio/best[height<={quality}]/best"

    download_success = False

    # 1. المرحلة الأولى: السحب عالي الدقة 1080p الأصلي بالكوكيز
    try:
        ytdl_cmd = [
            'yt-dlp',
            '--no-playlist',
            '--socket-timeout', '30',
            '--concurrent-fragments', '5',
            '--download-sections', f"*{start_time_str}-{extended_end_time_str}",
            '--force-keyframes-at-cuts',
            '-f', target_format_hd,
            '--merge-output-format', 'mp4',
            '-o', temp_raw
        ]
        if os.path.exists(COOKIE_FILE_PATH) and os.path.getsize(COOKIE_FILE_PATH) > 0:
            ytdl_cmd.extend(['--cookies', COOKIE_FILE_PATH])
        ytdl_cmd.append(url)

        res = subprocess.run(ytdl_cmd, capture_output=True, text=True, timeout=timeout_tier1)
        if res.returncode == 0 and os.path.exists(temp_raw) and os.path.getsize(temp_raw) > 1000:
            download_success = True
        else:
            err_msg = res.stderr.strip()[:200] if res.stderr else "Unknown error"
            print(f"⚠️ Tier 1 notice (code {res.returncode}): {err_msg}", flush=True)
    except Exception as e1:
        print(f"⚠️ Tier 1 cut exception ({timeout_tier1}s): {e1}", flush=True)

    # 2. المرحلة الثانية: إعادة المحاولة بنفس دقة 1080p الكاملة
    if not download_success or not os.path.exists(temp_raw) or os.path.getsize(temp_raw) < 1000:
        if progress_callback:
            progress_callback("⚡ جاري إعادة محاولة استخراج المقطع بأعلى دقة 1080p...")
        try:
            ytdl_cmd_retry = [
                'yt-dlp',
                '--no-playlist',
                '--socket-timeout', '30',
                '--concurrent-fragments', '5',
                '--download-sections', f"*{start_time_str}-{extended_end_time_str}",
                '--force-keyframes-at-cuts',
                '-f', target_format_hd,
                '--merge-output-format', 'mp4',
                '-o', temp_raw
            ]
            if os.path.exists(COOKIE_FILE_PATH) and os.path.getsize(COOKIE_FILE_PATH) > 0:
                ytdl_cmd_retry.extend(['--cookies', COOKIE_FILE_PATH])
            ytdl_cmd_retry.append(url)

            res = subprocess.run(ytdl_cmd_retry, capture_output=True, text=True, timeout=timeout_rescue)
            if res.returncode == 0 and os.path.exists(temp_raw) and os.path.getsize(temp_raw) > 1000:
                download_success = True
        except Exception as e2:
            print(f"⚠️ Tier 2 cut exception: {e2}", flush=True)

    # التحقق من وجود الملف المؤقت أو الملفات الجزئية
    if not os.path.exists(temp_raw):
        parent_dir = os.path.dirname(temp_raw)
        base_name = os.path.basename(temp_raw)
        for f in os.listdir(parent_dir):
            if f.startswith(base_name) and os.path.getsize(os.path.join(parent_dir, f)) > 1000:
                temp_raw = os.path.join(parent_dir, f)
                download_success = True
                break

    if os.path.exists(temp_raw) and os.path.getsize(temp_raw) > 1000:
        if progress_callback:
            progress_callback("✨ جاري تطبيق الفلاتر الصوتية والتلاشي...")

        ff_post = [
            'ffmpeg', '-y',
            '-i', temp_raw,
            '-filter_complex', f"[0:a]volume=1.5,afade=t=in:st=0:d={fade_in_duration},afade=t=out:st={start_fade_out}:d={fade_out_duration}[a]",
            '-map', '0:v', '-map', '[a]',
            '-c:v', 'copy',
            '-c:a', 'aac', '-b:a', '192k',
            output_path
        ]
        try:
            subprocess.run(ff_post, capture_output=True, text=True, timeout=40)
        except Exception as ffe:
            print(f"⚠️ FFmpeg post-processing notice: {ffe}", flush=True)
            
        try: os.remove(temp_raw)
        except: pass

        if os.path.exists(output_path) and os.path.getsize(output_path) > 1000:
            res_info = f"{quality}p"
            try:
                probe_cmd = ['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=width,height', '-of', 'csv=s=x:p=0', output_path]
                dim = subprocess.check_output(probe_cmd, text=True, timeout=5).strip()
                if dim and 'x' in dim:
                    res_info = f"{dim} ({dim.split('x')[1]}p Full HD)" if int(dim.split('x')[1]) >= 1080 else f"{dim} ({dim.split('x')[1]}p)"
            except Exception:
                pass
            print(f"🎉 Cut completed successfully! [📺 Output Quality / Resolution: {res_info}] ({os.path.getsize(output_path)/(1024*1024):.2f}MB)", flush=True)
            return output_path
        elif os.path.exists(temp_raw):
            try: os.rename(temp_raw, output_path)
            except: pass
            return output_path

    raise Exception("فشل استخراج المقطع من يوتيوب. يرجى التأكد من الرابط أو المحاولة مرة أخرى.")


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

    file_id = str(uuid.uuid4())[:8]
    output_path = os.path.join(TEMP_DIR, f"{file_id}.mp4")

    start_time_proc = time.time()
    try:
        cut_segment_fast(req.url, start_sec, end_sec, req.quality, output_path)
    except Exception as e:
        raise HTTPException(500, str(e))

    elapsed = time.time() - start_time_proc
    size_mb = os.path.getsize(output_path) / (1024 * 1024)
    print(f"✅ Cut success! {size_mb:.2f}MB | {elapsed:.1f}s | {req.quality}p MP4", flush=True)

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

        output_path = os.path.join(task_dir, "short_clip.mp4")

        def update_prog(msg):
            if task_id in TASKS:
                TASKS[task_id]["progress"] = msg

        cut_segment_fast(req.url, start_sec, end_sec, req.quality, output_path, progress_callback=update_prog)

        res_info = f"{req.quality}p"
        try:
            probe_cmd = ['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=width,height', '-of', 'csv=s=x:p=0', output_path]
            dim = subprocess.check_output(probe_cmd, text=True, timeout=5).strip()
            if dim and 'x' in dim:
                res_info = f"{dim} ({dim.split('x')[1]}p)"
        except Exception:
            pass

        video_url = f"public/temp_{task_id}/short_clip.mp4"
        TASKS[task_id] = {
            "status": "success",
            "progress": "✅ تم قص المقطع بنجاح!",
            "videoUrl": video_url
        }
        print(f"[{task_id}] Async Cut completed successfully: {video_url} [📺 Quality: {res_info}]", flush=True)

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

    out_w, out_h = 1080, 1920
    half_h = out_h // 2
    target_w = int(H * (9 / 16))

    if progress_callback:
        progress_callback("🔍 جاري تحليل وتحديد المشاهد (Fast Scene Detection)...")

    # 1. Fast Scene Detection with downscaling
    try:
        scene_list = detect(video_path, ContentDetector(threshold=27.0, min_scene_len=int(fps * 0.8)), start_in_scene=True)
        raw_cuts = [0] + [s[1].get_frames() for s in scene_list]
    except Exception:
        raw_cuts = [0, total_frames]

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
        progress_callback(f"👥 جاري تحليل الوجوه والكاميرات لـ {len(segments)} مشهد...")

    # 2. Fast sampling per scene using lightweight resized frames
    mp_face = mp.solutions.face_detection
    detector = mp_face.FaceDetection(model_selection=1, min_detection_confidence=0.3)
    scene_plans = []

    for s_idx, (start_f, end_f) in enumerate(segments):
        dur = (end_f - start_f) / fps
        n = int(max(4, min(10, dur * 1.5)))
        idxs = [start_f + int((end_f - start_f) * t) for t in np.linspace(0.08, 0.92, n)]
        
        frame_detections = []
        for idx in idxs:
            cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
            ok, frame = cap.read()
            if not ok or frame is None:
                continue
            
            small_frame = cv2.resize(frame, (640, int(640 * (H / W))))
            rgb = cv2.cvtColor(small_frame, cv2.COLOR_BGR2RGB)
            res = detector.process(rgb)
            
            if res.detections:
                current_faces = []
                for d in res.detections:
                    bb = d.location_data.relative_bounding_box
                    cx = int((bb.xmin + bb.width / 2) * W)
                    cy = int((bb.ymin + bb.height / 2) * H)
                    fw = int(bb.width * W)
                    if fw > W * 0.04:
                        current_faces.append((cx, cy))
                if current_faces:
                    frame_detections.append(current_faces)

        # Decide mode for this scene: Split (wide shot 2 speakers) or Single (1 speaker close-up/medium)
        dual_count = 0
        left_xs, right_xs, all_ys, all_xs = [], [], [], []

        for f_faces in frame_detections:
            if len(f_faces) >= 2:
                xs = sorted([f[0] for f in f_faces])
                if (xs[-1] - xs[0]) > target_w * 0.70:
                    dual_count += 1
            for cx, cy in f_faces:
                all_xs.append(cx)
                all_ys.append(cy)
                if cx < W * 0.46:
                    left_xs.append(cx)
                elif cx > W * 0.54:
                    right_xs.append(cx)

        is_split = False
        if dual_count >= 1 or (len(left_xs) >= 2 and len(right_xs) >= 2 and (np.median(right_xs) - np.median(left_xs)) > target_w * 0.75):
            is_split = True

        if is_split:
            cx1 = int(np.median(left_xs)) if left_xs else int(W * 0.25)
            cx2 = int(np.median(right_xs)) if right_xs else int(W * 0.75)
            cy_avg = int(np.median(all_ys)) if all_ys else int(H * 0.38)

            crop_w = int(min(W * 0.48, H * (1080 / 960) * 0.75))
            crop_h = int(crop_w * (960 / 1080))

            x1 = max(0, min(W - crop_w, cx1 - crop_w // 2))
            y1 = max(0, min(H - crop_h, cy_avg - int(crop_h * 0.42)))
            x2 = max(0, min(W - crop_w, cx2 - crop_w // 2))
            y2 = max(0, min(H - crop_h, cy_avg - int(crop_h * 0.42)))

            plan = {"mode": "split", "crop_w": crop_w, "crop_h": crop_h, "top": (x1, y1), "bottom": (x2, y2)}
        else:
            median_x = int(np.median(all_xs)) if all_xs else W // 2
            x1 = max(0, min(W - target_w, median_x - target_w // 2))
            plan = {"mode": "single", "x1": x1, "target_w": target_w}

        scene_plans.append(plan)

    detector.close()

    # 3. Native Direct FFmpeg Rendering Engine (Ultra-Fast Hardware/SIMD Pipeline)
    has_gpu = False
    try:
        gpu_check = subprocess.run(["nvidia-smi"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if gpu_check.returncode == 0:
            has_gpu = True
    except Exception:
        pass

    codec_args = ["-c:v", "h264_nvenc", "-preset", "p4", "-cq", "19"] if has_gpu else ["-c:v", "libx264", "-preset", "veryfast", "-crf", "19", "-threads", "0"]

    if progress_callback:
        progress_callback("⚡ جاري اقتصاص ورندرة الفيديو طولي (9:16) فائق السرعة عبر محرك FFmpeg المباشر...")

    # محاولة الرندر المباشر فائق السرعة عبر FFmpeg Native Filter (يوفر 80% من الوقت مع الحفاظ التام على جودة 1080p)
    ffmpeg_native_success = False
    try:
        if len(segments) == 1 and scene_plans[0]["mode"] == "single":
            x1 = scene_plans[0]["x1"]
            tw = scene_plans[0]["target_w"]
            fast_vf = f"crop={tw}:{H}:{x1}:0,scale={out_w}:{out_h}"
            fast_cmd = [
                "ffmpeg", "-y",
                "-i", video_path,
                "-vf", fast_vf,
                *codec_args,
                "-pix_fmt", "yuv420p",
                "-c:a", "copy",
                "-movflags", "+faststart",
                output_path
            ]
            ff_p = subprocess.run(fast_cmd, capture_output=True, text=True, timeout=120)
            if ff_p.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 1000:
                ffmpeg_native_success = True
                print("🎉 Native FFmpeg Single Crop Succeeded in seconds!", flush=True)
        elif len(segments) > 1:
            # Multi-segment native filter_complex
            filter_parts = []
            concat_inputs = []
            for i, (seg, plan) in enumerate(zip(segments, scene_plans)):
                start_f, end_f = seg
                prefix = f"v{i}"
                trim_filter = f"[0:v]trim=start_frame={start_f}:end_frame={end_f},setpts=PTS-STARTPTS"
                if plan["mode"] == "single":
                    x1 = plan["x1"]
                    tw = plan["target_w"]
                    filter_parts.append(f"{trim_filter},crop={tw}:{H}:{x1}:0,scale={out_w}:{out_h}[{prefix}]")
                    concat_inputs.append(f"[{prefix}]")
                elif plan["mode"] == "split":
                    x1, y1 = plan["top"]
                    x2, y2 = plan["bottom"]
                    cw, ch = plan["crop_w"], plan["crop_h"]
                    filter_parts.append(f"{trim_filter},split=2[{prefix}_raw1][{prefix}_raw2]")
                    filter_parts.append(f"[{prefix}_raw1]crop={cw}:{ch}:{x1}:{y1},scale={out_w}:{half_h}[{prefix}_top]")
                    filter_parts.append(f"[{prefix}_raw2]crop={cw}:{ch}:{x2}:{y2},scale={out_w}:{half_h}[{prefix}_bot]")
                    filter_parts.append(f"[{prefix}_top][{prefix}_bot]vstack,drawbox=y={half_h-1}:color=black@0.9:t=2[{prefix}]")
                    concat_inputs.append(f"[{prefix}]")
            
            fc_string = ";".join(filter_parts) + ";" + "".join(concat_inputs) + f"concat=n={len(concat_inputs)}:v=1:a=0[outv]"
            fast_cmd = [
                "ffmpeg", "-y",
                "-i", video_path,
                "-filter_complex", fc_string,
                "-map", "[outv]", "-map", "0:a?",
                *codec_args,
                "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "192k",
                "-movflags", "+faststart",
                "-shortest", output_path
            ]
            ff_p = subprocess.run(fast_cmd, capture_output=True, text=True, timeout=180)
            if ff_p.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 1000:
                ffmpeg_native_success = True
                print("🎉 Native FFmpeg Multi-Scene Filter Complex Succeeded!", flush=True)
    except Exception as nfe:
        print(f"⚠️ Native FFmpeg Notice (falling back to frame pipeline): {nfe}", flush=True)

    if ffmpeg_native_success:
        cap.release()
        return

    # 4. Fallback Frame Pipeline (إذا لزم الأمر كإجراء احتياطي)
    cmd = [
        "ffmpeg", "-y",
        "-f", "rawvideo", "-pix_fmt", "bgr24",
        "-s", f"{out_w}x{out_h}", "-r", str(fps),
        "-i", "-",
        "-i", video_path,
        "-map", "0:v", "-map", "1:a?",
        *codec_args,
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        "-shortest", output_path
    ]

    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
    seg_i, frame_i = 0, 0
    out_frame = np.zeros((out_h, out_w, 3), dtype=np.uint8)

    while True:
        ok, frame = cap.read()
        if not ok or frame is None:
            break
        
        while seg_i + 1 < len(segments) and frame_i >= segments[seg_i][1]:
            seg_i += 1
        
        plan = scene_plans[seg_i]

        if plan["mode"] == "split":
            x1, y1 = plan["top"]
            cw, ch = plan["crop_w"], plan["crop_h"]
            top_crop = frame[y1:y1 + ch, x1:x1 + cw]
            top_scaled = cv2.resize(top_crop, (out_w, half_h), interpolation=cv2.INTER_LINEAR)

            x2, y2 = plan["bottom"]
            bottom_crop = frame[y2:y2 + ch, x2:x2 + cw]
            bottom_scaled = cv2.resize(bottom_crop, (out_w, half_h), interpolation=cv2.INTER_LINEAR)

            out_frame[:half_h, :] = top_scaled
            out_frame[half_h:, :] = bottom_scaled
            out_frame[half_h - 1 : half_h + 1, :] = 25
        else:
            x1 = plan["x1"]
            tw = plan["target_w"]
            crop = frame[0:H, x1:x1 + tw]
            out_frame = cv2.resize(crop, (out_w, out_h), interpolation=cv2.INTER_LINEAR)

        proc.stdin.write(out_frame.tobytes())
        frame_i += 1
        
        if frame_i % 120 == 0 and progress_callback:
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
                '-f', 'bestvideo[height<=1080]+bestaudio/bestvideo[height<=1080]/best[height<=1080]/best',
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


# ==================== Zernio Social Media Posting & TikTok Scheduler API ====================

class ZernioScheduleRequest(BaseModel):
    apiKey: str
    content: str
    videoUrl: str
    publishNow: bool = False
    scheduledFor: str | None = None
    accountId: str | None = None
    platform: str = "tiktok"

@app.post("/api/zernio/profiles")
async def zernio_get_profiles(payload: dict):
    api_key = payload.get("apiKey") or os.environ.get("ZERNIO_API_KEY")
    if not api_key:
        raise HTTPException(400, "Zernio API Key is required.")
    
    try:
        headers = {
            "Authorization": f"Bearer {api_key.strip()}",
            "Content-Type": "application/json"
        }
        res = requests.get("https://zernio.com/api/v1/profiles", headers=headers, timeout=20)
        if res.status_code != 200:
            raise HTTPException(res.status_code, f"Zernio API Error: {res.text}")
        return res.json()
    except Exception as e:
        if isinstance(e, HTTPException): raise e
        raise HTTPException(500, f"Failed to connect to Zernio: {str(e)}")

@app.post("/api/zernio/schedule-post")
async def zernio_schedule_post(req: ZernioScheduleRequest):
    api_key = req.apiKey or os.environ.get("ZERNIO_API_KEY")
    if not api_key:
        raise HTTPException(400, "Zernio API Key is required.")

    if not req.videoUrl:
        raise HTTPException(400, "Video URL is required for TikTok posting.")

    try:
        headers = {
            "Authorization": f"Bearer {api_key.strip()}",
            "Content-Type": "application/json",
            "x-request-id": str(uuid.uuid4())
        }

        platform_entry = {
            "platform": req.platform
        }
        if req.accountId:
            platform_entry["accountId"] = req.accountId

        post_body = {
            "content": req.content,
            "platforms": [platform_entry],
            "media": [
                {
                    "type": "video",
                    "url": req.videoUrl
                }
            ],
            "publishNow": req.publishNow
        }

        if req.scheduledFor and not req.publishNow:
            post_body["scheduledFor"] = req.scheduledFor
            post_body["isDraft"] = False

        res = requests.post("https://zernio.com/api/v1/posts", headers=headers, json=post_body, timeout=30)
        if res.status_code not in [200, 201]:
            raise HTTPException(res.status_code, f"Zernio Post Error: {res.text}")

        return {
            "status": "success",
            "message": "تم إرسال الفيديو لـ TikTok بنجاح!" if req.publishNow else "تمت جدولة الفيديو على TikTok بنجاح!",
            "data": res.json()
        }
    except Exception as e:
        if isinstance(e, HTTPException): raise e
        raise HTTPException(500, f"Error posting to Zernio: {str(e)}")

@app.post("/api/zernio/posts")
async def zernio_list_posts(payload: dict):
    api_key = payload.get("apiKey") or os.environ.get("ZERNIO_API_KEY")
    if not api_key:
        raise HTTPException(400, "Zernio API Key is required.")

    try:
        headers = {
            "Authorization": f"Bearer {api_key.strip()}",
            "Content-Type": "application/json"
        }
        res = requests.get("https://zernio.com/api/v1/posts", headers=headers, timeout=20)
        if res.status_code != 200:
            raise HTTPException(res.status_code, f"Zernio API Error: {res.text}")
        return res.json()
    except Exception as e:
        if isinstance(e, HTTPException): raise e
        raise HTTPException(500, f"Failed to list posts from Zernio: {str(e)}")



