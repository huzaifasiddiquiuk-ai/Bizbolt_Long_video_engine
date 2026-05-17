import os, io, json, math, shutil, subprocess, time, requests
from PIL import Image, ImageDraw, ImageFont, ImageChops
import cairosvg
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload

# ── 1. AUTH ──────────────────────────────────────────────────
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
        scopes=["https://www.googleapis.com/auth/drive",
                "https://www.googleapis.com/auth/youtube.upload"]
    )
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return build("drive", "v3", credentials=creds, cache_discovery=False)

service = get_drive_service()
print("✅ Drive authenticated!")

MAIN_FOLDER_ID = os.environ.get("MAIN_FOLDER_ID")

def get_folder_id(name, parent_id=MAIN_FOLDER_ID, current_service=service):
    res = current_service.files().list(
        q=f"name='{name}' and '{parent_id}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false",
        fields="files(id,name)"
    ).execute()
    files = res.get("files", [])
    if not files: raise FileNotFoundError(f"❌ Folder not found: {name}")
    return files[0]["id"]

def list_files(folder_id, current_service=service):
    return current_service.files().list(
        q=f"'{folder_id}' in parents and trashed=false",
        fields="files(id,name)", orderBy="name"
    ).execute().get("files", [])

def download_file(file_id, local_path, current_service=service):
    req = current_service.files().get_media(fileId=file_id)
    with open(local_path, "wb") as f:
        dl = MediaIoBaseDownload(f, req)
        done = False
        while not done: _, done = dl.next_chunk()

def upload_file(local_path, name, parent_id, current_service):
    print(f"   🔄 Uploading {name}...")
    media = MediaFileUpload(local_path, mimetype="video/mp4", resumable=True, chunksize=5*1024*1024)
    req   = current_service.files().create(
        body={"name": name, "parents": [parent_id]},
        media_body=media, fields="id"
    )
    response = None; retries = 0
    while response is None:
        try:
            status, response = req.next_chunk()
            if status: print(f"   📤 {int(status.progress()*100)}%")
            retries = 0
        except Exception as e:
            retries += 1
            if retries > 10: raise e
            print(f"   ⚠️ Retry {retries}/10... ({e})"); time.sleep(5)
    print(f"   ✅ Uploaded: {name}")

def get_duration(path):
    r = subprocess.run(["ffprobe","-v","error","-show_entries","format=duration",
                        "-of","default=noprint_wrappers=1:nokey=1",path],
                       capture_output=True, text=True)
    return float(r.stdout.strip())

# ── 2. CONFIG ────────────────────────────────────────────────
VIDEO_W    = 1920
VIDEO_H    = 1080
FPS        = 60
BG_COLOR   = (255, 255, 255)   # White background — documentary style
ANIM_SECS  = 0.35
WORK_DIR   = "/tmp/motion_work"
FRAMES_DIR = os.path.join(WORK_DIR, "frames")
AUDIO_DIR  = os.path.join(WORK_DIR, "audio")

os.makedirs(FRAMES_DIR, exist_ok=True)
os.makedirs(AUDIO_DIR,  exist_ok=True)

# ── 3. EASING ────────────────────────────────────────────────
def ease_out_expo(t):
    return 1.0 if t >= 1.0 else 1 - math.pow(2, -10 * t)

def ease_out_back(t):
    c1, c3 = 1.70158, 2.70158
    return 1 + c3 * math.pow(t - 1, 3) + c1 * math.pow(t - 1, 2)

def ease_out_bounce(t):
    t = min(t, 1.0)
    if   t < 1/2.75:   return 7.5625*t*t
    elif t < 2/2.75:   t -= 1.5/2.75;  return 7.5625*t*t+0.75
    elif t < 2.5/2.75: t -= 2.25/2.75; return 7.5625*t*t+0.9375
    else:               t -= 2.625/2.75;return 7.5625*t*t+0.984375

# ── 4. ANIMATION STATE ───────────────────────────────────────
def get_anim_state(t_local, duration):
    af = ANIM_SECS
    ef = ANIM_SECS

    if t_local < af:
        t_in  = t_local / af
        alpha = ease_out_expo(t_in)
        scale = ease_out_back(t_in)
        scale = max(0.01, scale)
    elif t_local > duration - ef:
        t_out = (t_local - (duration - ef)) / ef
        t_out = min(t_out, 1.0)
        alpha = 1.0 - t_out
        scale = 1.0 - 0.06 * t_out
    else:
        idle_t = t_local - af
        alpha  = 1.0
        scale  = 1.0 + 0.008 * math.sin(idle_t * 1.8)  # subtle breathing

    return alpha, scale

# ── 5. FILM GRAIN ────────────────────────────────────────────
def apply_grain(image):
    grain = Image.effect_noise((VIDEO_W, VIDEO_H), 8)
    grain = grain.convert("RGBA")
    base  = image.convert("RGBA")
    result = Image.blend(base, grain, 0.03)
    return result.convert("RGB")

# ── 6. ASSET FETCH ───────────────────────────────────────────
def fetch_asset(raw_url, width, height):
    """GitHub raw_url se asset download karo — SVG ya PNG dono handle karta hai."""
    r = requests.get(raw_url, timeout=15)
    if r.status_code != 200:
        raise Exception(f"❌ Asset nahi mila: {raw_url} [{r.status_code}]")

    if raw_url.lower().endswith(".svg"):
        png_data = cairosvg.svg2png(
            bytestring=r.content,
            output_width=width,
            output_height=height
        )
        img = Image.open(io.BytesIO(png_data)).convert("RGBA")
    else:
        img = Image.open(io.BytesIO(r.content)).convert("RGBA")
        img = img.resize((width, height), Image.LANCZOS)

    return img

# ── 7. DOWNLOAD JSON FROM DRIVE ──────────────────────────────
print("\n▶ Downloading Visual JSON...")
mid   = get_folder_id("Motion_data", current_service=service)
files = list_files(mid, current_service=service)
jf    = next((f for f in files if f["name"].endswith(".json")), None)
if not jf: raise FileNotFoundError("❌ .json nahi mila in Motion_data!")

jp = os.path.join(WORK_DIR, "visuals.json")
os.makedirs(WORK_DIR, exist_ok=True)
download_file(jf["id"], jp, current_service=service)
print(f"   ✅ {jf['name']}")

with open(jp) as f:
    raw = json.load(f)

visuals = raw.get("visuals", [])
visuals = sorted(visuals, key=lambda v: v["start"])
print(f"   ✅ {len(visuals)} visuals loaded")

# ── 8. DOWNLOAD AUDIO ────────────────────────────────────────
print("\n▶ Downloading Audio...")
aid   = get_folder_id("Final_audio", current_service=service)
afiles= list_files(aid, current_service=service)
af2   = next((f for f in afiles if "final_mix" in f["name"].lower()
              and "script" not in f["name"].lower()), None)
if not af2: raise FileNotFoundError("❌ final_mix.mp3 nahi mila!")

ap = os.path.join(AUDIO_DIR, "final_mix.mp3")
download_file(af2["id"], ap, current_service=service)
audio_dur    = get_duration(ap)
total_frames = int(audio_dur * FPS)
print(f"   ✅ {audio_dur:.2f}s | {total_frames} frames @ {FPS}fps")

# ── 9. PRE-CACHE ALL ASSETS ──────────────────────────────────
print("\n▶ Pre-fetching assets from GitHub...")
asset_cache = {}
for vis in visuals:
    key = vis["raw_url"]
    if key not in asset_cache:
        try:
            print(f"   ⬇ {vis['asset_path'].split('/')[-1]}")
            asset_cache[key] = fetch_asset(key, vis["width"], vis["height"])
        except Exception as e:
            print(f"   ⚠️ Skip: {e}")
            asset_cache[key] = None
print(f"   ✅ {len(asset_cache)} assets ready")

# ── 10. DRAW FRAME ───────────────────────────────────────────
def draw_frame(frame_rgba, visuals, t):
    # Active visuals at time t, sorted by z_index (low = bottom)
    active = [v for v in visuals if v["start"] <= t < v["end"]]
    active = sorted(active, key=lambda v: v["z_index"])

    overlay = Image.new("RGBA", (VIDEO_W, VIDEO_H), (0, 0, 0, 0))

    for vis in active:
        img = asset_cache.get(vis["raw_url"])
        if img is None:
            continue

        t_local  = t - vis["start"]
        duration = vis["end"] - vis["start"]
        alpha, scale = get_anim_state(t_local, duration)

        if alpha <= 0.01:
            continue

        # Scale karo
        w = max(1, int(vis["width"]  * scale))
        h = max(1, int(vis["height"] * scale))

        if w != img.width or h != img.height:
            img_scaled = img.resize((w, h), Image.LANCZOS)
        else:
            img_scaled = img

        # Scale hone pe center maintain karo
        x = int(vis["x"] + (vis["width"]  - w) / 2)
        y = int(vis["y"] + (vis["height"] - h) / 2)

        # Alpha apply karo
        r2, g2, b2, a2 = img_scaled.split()
        a2 = a2.point(lambda p: int(p * alpha))
        img_final = Image.merge("RGBA", (r2, g2, b2, a2))

        overlay.paste(img_final, (x, y), img_final)

    return Image.alpha_composite(frame_rgba, overlay)

# ── 11. FRAME LOOP ───────────────────────────────────────────
print(f"\n▶ Rendering {total_frames} frames @ {FPS}fps...")
bg = Image.new("RGB", (VIDEO_W, VIDEO_H), BG_COLOR)
t0 = time.time()

for fi in range(total_frames):
    t     = fi / FPS
    frame = bg.copy().convert("RGBA")
    frame = draw_frame(frame, visuals, t)
    final = apply_grain(frame)
    final.save(os.path.join(FRAMES_DIR, f"frame_{fi:06d}.png"))

    if fi % 300 == 0:
        el  = time.time() - t0
        eta = (el / max(fi, 1)) * (total_frames - fi)
        print(f"   {fi}/{total_frames} | ETA: {eta:.0f}s")

print(f"✅ Frames done in {time.time()-t0:.1f}s")

# ── 12. ENCODE ───────────────────────────────────────────────
print("\n▶ FFmpeg encode...")
out = os.path.join(WORK_DIR, "motion_video.mp4")
res = subprocess.run(
    f'ffmpeg -y -r {FPS} -i "{FRAMES_DIR}/frame_%06d.png" -i "{ap}" '
    f'-c:v libx264 -preset fast -crf 18 -pix_fmt yuv420p '
    f'-c:a aac -b:a 192k -shortest "{out}"',
    shell=True, capture_output=True, text=True
)
if res.returncode != 0:
    print(res.stderr[-2000:])
    raise RuntimeError("❌ FFmpeg fail!")
print("✅ Encoded!")

# ── 13. UPLOAD ───────────────────────────────────────────────
print("\n▶ Re-authenticating before upload...")
fs  = get_drive_service()
vid = get_folder_id("Video", current_service=fs)
upload_file(out, "motion_video.mp4", vid, current_service=fs)

shutil.rmtree(WORK_DIR)
print("🧹 Done!")
