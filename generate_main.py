import os, io, json, subprocess, shutil, time
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload

# ── 1. AUTHENTICATION FUNCTION ───────────────────────────────
def get_drive_service():
    print("🔄 Drive se naya connection bana raha hu...")
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

def get_duration(path):
    r = subprocess.run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", path
    ], capture_output=True, text=True)
    return float(r.stdout.strip())

# ── 3. SETUP LOCAL DIRS ──────────────────────────────────────
WORK_DIR   = "/tmp/main_video_work"
IMAGES_DIR = os.path.join(WORK_DIR, "images")
AUDIO_DIR  = os.path.join(WORK_DIR, "audio")

os.makedirs(IMAGES_DIR, exist_ok=True)
os.makedirs(AUDIO_DIR, exist_ok=True)

# ── 4. DOWNLOAD MAIN SCRIPT IMAGES ───────────────────────────
print("\n▶ Downloading Main Script Images...")
script_images_folder_id = get_folder_id("Script_images", current_service=service)
all_files = list_files(script_images_folder_id, current_service=service)
image_files = [f for f in all_files if f["name"].lower().endswith(('.jpg', '.png'))]

if not image_files:
    raise FileNotFoundError("❌ Script_images folder mein koi images nahi mili!")

local_images = []
for f in image_files:
    local_path = os.path.join(IMAGES_DIR, f["name"])
    download_file(f["id"], local_path, current_service=service)
    local_images.append(local_path)
    print(f"   ✅ Downloaded: {f['name']}")

local_images = sorted(local_images)

# ── 5. DOWNLOAD MAIN AUDIO ───────────────────────────────────
print("\n▶ Downloading Main Audio (script_final_mix.mp3)...")
audio_folder_id = get_folder_id("Final_audio", current_service=service)
audio_files = list_files(audio_folder_id, current_service=service)

main_audio_file = next((f for f in audio_files if "script_final_mix" in f["name"].lower()), None)
if main_audio_file is None:
    raise FileNotFoundError("❌ Final_audio folder mein 'script_final_mix.mp3' nahi mila!")

audio_path = os.path.join(AUDIO_DIR, "script_final_mix.mp3")
download_file(main_audio_file["id"], audio_path, current_service=service)
print("   ✅ Main audio downloaded!")

# ── 6. VIDEO GENERATION ──────────────────────────────────────
print("\n▶ Generating Premium Main Video... (Isme time lagega)")
total_audio_duration = get_duration(audio_path)
num_images = len(local_images)
fps = 30
fade_duration = 1.0

if num_images == 1:
    duration_per_image = total_audio_duration
    fade_duration = 0
else:
    duration_per_image = (total_audio_duration + (num_images - 1) * fade_duration) / num_images

total_frames = int(duration_per_image * fps)

# ── Ken Burns: 6 patterns (zoom+pan combos), dynamically calculated ──────
# zoom goes from 1.0 → 1.38 over FULL clip duration (never hits max → no still frame)
# pan moves 280px across 3840-wide canvas over full clip
def get_kb_expr(i, frames):
    rate = round(0.38 / frames, 7)
    pan  = round(280.0  / frames, 5)
    d    = frames + 20

    patterns = [
        # 0: zoom-in, pan right
        (f"z='min(zoom+{rate},1.4)':"
         f"x='min(iw/2-(iw/zoom/2)+on*{pan},iw-(iw/zoom))':"
         f"y='ih/2-(ih/zoom/2)'", d),
        # 1: zoom-in, pan left
        (f"z='min(zoom+{rate},1.4)':"
         f"x='max(0,iw/2-(iw/zoom/2)-on*{pan})':"
         f"y='ih/2-(ih/zoom/2)'", d),
        # 2: zoom-in, tilt down
        (f"z='min(zoom+{rate},1.4)':"
         f"x='iw/2-(iw/zoom/2)':"
         f"y='min(ih/2-(ih/zoom/2)+on*{pan},ih-(ih/zoom))'", d),
        # 3: zoom-in, tilt up
        (f"z='min(zoom+{rate},1.4)':"
         f"x='iw/2-(iw/zoom/2)':"
         f"y='max(0,ih/2-(ih/zoom/2)-on*{pan})'", d),
        # 4: zoom-out from top-left corner
        (f"z='if(lte(on,1),1.4,max(1.0,zoom-{rate}))':"
         f"x='0':y='0'", d),
        # 5: zoom-out from bottom-right corner
        (f"z='if(lte(on,1),1.4,max(1.0,zoom-{rate}))':"
         f"x='iw-(iw/zoom)':y='ih-(ih/zoom)'", d),
    ]
    return patterns[i % len(patterns)]

filter_complex = ""
inputs = ""

for i, img_path in enumerate(local_images):
    inputs += f"-loop 1 -t {duration_per_image + 2.0} -i \"{img_path}\" "
    kb_expr, kb_d = get_kb_expr(i, total_frames)
    filter_complex += (
        f"[{i}:v]"
        f"scale=3840:2160:force_original_aspect_ratio=increase:flags=lanczos,"
        f"crop=3840:2160,setsar=1,"
        f"zoompan={kb_expr}:d={kb_d}:s=1920x1080:fps={fps},"
        f"trim=duration={duration_per_image:.4f},setpts=PTS-STARTPTS,"
        f"eq=brightness=0.05:contrast=1.1:saturation=1.3,"
        f"unsharp=5:5:0.8:5:5:0.0,"
        f"format=yuv420p[v{i}];"
    )

# ── xfade transitions ────────────────────────────────────────
transitions = ["fade", "slideleft", "slideright", "wipeleft", "wiperight",
               "fadeblack", "smoothleft", "smoothright"]

if num_images == 1:
    filter_complex += "[v0]copy[outv];"
else:
    last_out = "[v0]"
    for i in range(1, num_images):
        offset = i * (duration_per_image - fade_duration)
        out_name = f"[xf{i}]" if i < num_images - 1 else "[outv]"
        t = transitions[(i - 1) % len(transitions)]
        filter_complex += f"{last_out}[v{i}]xfade=transition={t}:duration={fade_duration}:offset={offset}{out_name};"
        last_out = out_name

filter_complex = filter_complex.rstrip(';')
output_path = os.path.join(WORK_DIR, "main_final.mp4")

ffmpeg_cmd = (
    f"ffmpeg -y {inputs} -i \"{audio_path}\" "
    f"-filter_complex \"{filter_complex}\" "
    f"-map \"[outv]\" -map {num_images}:a "
    f"-c:v libx264 -preset slow -crf 18 -pix_fmt yuv420p "
    f"-c:a aac -b:a 320k -shortest \"{output_path}\""
)

result = subprocess.run(ffmpeg_cmd, shell=True, capture_output=True, text=True)
if result.returncode != 0:
    print(result.stderr)
    raise RuntimeError("❌ FFmpeg crash ho gaya!")

print(f"✅ Main video ready: {output_path}")

# ── 7. RE-AUTHENTICATE & UPLOAD ──────────────────────────────
print("\n▶ Lambe render ke baad connection tut chuka hoga. Re-authenticating...")
fresh_service = get_drive_service()

video_folder_id = get_folder_id("Video", current_service=fresh_service)

print("\n▶ Uploading Main Video to Drive...")
upload_file(output_path, "main_final.mp4", video_folder_id, current_service=fresh_service)

# ── 8. CLEANUP ───────────────────────────────────────────────
shutil.rmtree(WORK_DIR)
print("🧹 Done! Main video folder cleaned.")
