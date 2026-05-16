import os, json, subprocess, time, urllib.request
import whisper
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload

# ── 1. AUTHENTICATION FUNCTION ───────────────────────────────
def get_drive_service():
    print("🔄 Drive se connection bana raha hu...")
    creds_data = json.loads(os.environ["DRIVE_CREDENTIALS"])
    token_data  = json.loads(os.environ["YOUTUBE_TOKEN"])

    creds = Credentials(
        token=token_data.get("token"),
        refresh_token=token_data.get("refresh_token"),
        token_uri=creds_data["installed"]["token_uri"],
        client_id=creds_data["installed"]["client_id"],
        client_secret=creds_data["installed"]["client_secret"],
        scopes=[
            "https://www.googleapis.com/auth/drive",
            "https://www.googleapis.com/auth/youtube.upload"
        ]
    )
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return build("drive", "v3", credentials=creds, cache_discovery=False)

service = get_drive_service()
print("✅ Initial Drive authentication successful!")

# ── 2. DRIVE HELPERS ─────────────────────────────────────────
MAIN_FOLDER_ID = os.environ.get("MAIN_FOLDER_ID")

def get_folder_id(name, parent_id=MAIN_FOLDER_ID, current_service=service):
    results = current_service.files().list(
        q=f"name='{name}' and '{parent_id}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false",
        fields="files(id, name)"
    ).execute()
    files = results.get("files", [])
    if not files:
        raise FileNotFoundError(f"❌ Folder not found: {name}")
    return files[0]["id"]

def list_files(folder_id, current_service=service):
    results = current_service.files().list(
        q=f"'{folder_id}' in parents and trashed=false",
        fields="files(id, name)",
        orderBy="name"
    ).execute()
    return results.get("files", [])

def download_file(file_id, local_path, current_service=service):
    request = current_service.files().get_media(fileId=file_id)
    with open(local_path, "wb") as f:
        downloader = MediaIoBaseDownload(f, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()

def upload_file(local_path, name, parent_id, current_service):
    print(f"   🔄 Uploading {name}...")
    media = MediaFileUpload(local_path, mimetype="video/mp4", resumable=True, chunksize=5*1024*1024)
    file_meta = {"name": name, "parents": [parent_id]}
    request = current_service.files().create(body=file_meta, media_body=media, fields="id")
    
    response = None
    retries = 0
    while response is None:
        try:
            status, response = request.next_chunk()
            if status:
                print(f"   📤 {name}: {int(status.progress() * 100)}%")
            retries = 0
        except Exception as e:
            retries += 1
            print(f"   ⚠️ Network Drop Hua (Attempt {retries}/10). 5 sec wait kar raha hu... Error: {e}")
            if retries > 10:
                print("❌ Bhai, Google API baar-baar connection tod rahi hai. Upload fail!")
                raise e
            time.sleep(5)
            
    print(f"   ✅ Uploaded: {name}")
    return response["id"]

# ── 3. SETUP LOCAL DIRS & DOWNLOAD FONT ──────────────────────
WORK_DIR = "/tmp/combine_work"
os.makedirs(WORK_DIR, exist_ok=True)

font_url  = "https://github.com/JulietaUla/Montserrat/raw/master/fonts/ttf/Montserrat-Black.ttf"
font_path = os.path.join(WORK_DIR, "Montserrat-Black.ttf")
print("\n▶ Downloading Montserrat Black Font...")
urllib.request.urlretrieve(font_url, font_path)
print("✅ Font downloaded!")

# ── 4. DOWNLOAD HOOK & MAIN VIDEO ────────────────────────────
print("\n▶ Fetching Videos from Drive...")
video_folder_id = get_folder_id("Video", current_service=service)
video_files = list_files(video_folder_id, current_service=service)

hook_file_id = next((f["id"] for f in video_files if f["name"] == "hook_final.mp4"), None)
main_file_id = next((f["id"] for f in video_files if f["name"] == "main_final.mp4"), None)

if not hook_file_id or not main_file_id:
    raise FileNotFoundError("❌ hook_final.mp4 ya main_final.mp4 Drive ke Video folder mein nahi mili!")

hook_path = os.path.join(WORK_DIR, "hook_final.mp4")
main_path = os.path.join(WORK_DIR, "main_final.mp4")

download_file(hook_file_id, hook_path, current_service=service)
print("✅ Hook video downloaded")
download_file(main_file_id, main_path, current_service=service)
print("✅ Main video downloaded")

# ── 5. CONCATENATE VIDEOS ────────────────────────────────────
print("\n▶ Combining Videos...")
concat_list = os.path.join(WORK_DIR, "concat_list.txt")
with open(concat_list, "w") as f:
    f.write(f"file '{hook_path}'\n")
    f.write(f"file '{main_path}'\n")

combined_raw_path = os.path.join(WORK_DIR, "combined_raw.mp4")
subprocess.run([
    "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_list,
    "-c", "copy", combined_raw_path
], check=True)
print("✅ Videos combined successfully")

# ── 6. EXTRACT AUDIO & TRANSCRIBE (WHISPER) ──────────────────
print("\n▶ Extracting Audio for Transcription...")
audio_path = os.path.join(WORK_DIR, "audio_for_subs.wav")
subprocess.run(["ffmpeg", "-y", "-i", combined_raw_path, "-q:a", "0", "-map", "a", audio_path], check=True)

print("▶ Running Whisper AI for Word-Level Timestamps... (Isme time lagega)")
model  = whisper.load_model("base")
result = model.transcribe(audio_path, word_timestamps=True)

# ── 7. GENERATE WORD-BY-WORD ASS SUBTITLES ───────────────────
print("\n▶ Generating Word-Level Subtitles...")

def format_ass_time(sec):
    h  = int(sec // 3600)
    m  = int((sec % 3600) // 60)
    s  = int(sec % 60)
    cs = int(round((sec - int(sec)) * 100))
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"

ass_path = os.path.join(WORK_DIR, "subtitles.ass")

# ── ASS Style Notes ───────────────────────────────────────────
# PlayResX/Y = actual video resolution (1920x1080 landscape)
# Fontsize 90 → large, clearly visible
# PrimaryColour &H00FFFFFF = pure white
# OutlineColour &H00000000 = black outline
# Outline 6 + Shadow 3 = strong contrast on any background
# Alignment 2 = center-bottom
# MarginV 80 = 80px from bottom edge
# No yellow highlight — white text only throughout

ass_header = """\
[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Montserrat Black,90,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,1,0,1,6,3,2,50,50,80,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

with open(ass_path, "w", encoding="utf-8") as f:
    f.write(ass_header)

    for segment in result["segments"]:
        words = segment.get("words", [])
        if not words:
            continue

        for i, word_obj in enumerate(words):
            start = word_obj["start"]

            # end = exactly when next word starts → mathematically zero overlap
            if i < len(words) - 1:
                end = words[i + 1]["start"]
            else:
                end = word_obj["end"] + 0.05   # tiny tail for last word

            # Skip zero-duration events (can happen if whisper gives same timestamp twice)
            if end <= start:
                end = start + 0.05

            word_text = word_obj["word"].strip().upper()
            if not word_text:
                continue

            ass_line = (
                f"Dialogue: 0,"
                f"{format_ass_time(start)},"
                f"{format_ass_time(end)},"
                f"Default,,0,0,0,,{word_text}\n"
            )
            f.write(ass_line)

print("✅ ASS Subtitles generated!")

# ── 8. BURN SUBTITLES INTO VIDEO ─────────────────────────────
print("\n▶ Burning Subtitles into Video... (Isme bhi time lagega)")
final_output_path = os.path.join(WORK_DIR, "final_ready_video.mp4")

subprocess.run([
    "ffmpeg", "-y", "-i", combined_raw_path,
    "-vf", f"ass={ass_path}:fontsdir={WORK_DIR}",
    "-c:v", "libx264", "-preset", "slow", "-crf", "18",
    "-c:a", "copy",
    final_output_path
], check=True)

print("✅ Subtitles burned successfully!")

# ── 9. RE-AUTHENTICATE & UPLOAD FINAL VIDEO ──────────────────
print("\n▶ Lambe process ke baad connection tut chuka hoga. Re-authenticating...")
fresh_service = get_drive_service()

print("\n▶ Uploading Final Video to Drive...")
upload_file(final_output_path, "final_ready_video.mp4", video_folder_id, current_service=fresh_service)

print("\n🎉 BINGO! Final video is ready in your Drive!")
