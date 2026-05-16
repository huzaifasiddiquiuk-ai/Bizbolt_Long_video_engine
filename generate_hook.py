import os, io, json, math, shutil, subprocess, time, requests
from PIL import Image, ImageDraw, ImageFont
import cairosvg
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload

# ── 1. AUTHENTICATION ────────────────────────────────────────
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
    retries  = 0
    while response is None:
        try:
            status, response = request.next_chunk()
            if status:
                print(f"   📤 {name}: {int(status.progress() * 100)}%")
            retries = 0
        except Exception as e:
            retries += 1
            print(f"   ⚠️ Network Drop (Attempt {retries}/10). 5 sec wait... Error: {e}")
            if retries > 10:
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

# ── 3. CONFIG ────────────────────────────────────────────────
VIDEO_W    = 1080
VIDEO_H    = 1920
FPS        = 30
BG_COLOR   = (10, 10, 10)
ICON_SIZE  = 200
FONT_SIZE  = 64
ANIM_SECS  = 0.4
LUCIDE_CDN = "https://cdn.jsdelivr.net/npm/lucide-static@0.441.0/icons/{}.svg"
FONT_PATH  = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

WORK_DIR   = "/tmp/motion_work"
FRAMES_DIR = os.path.join(WORK_DIR, "frames")
AUDIO_DIR  = os.path.join(WORK_DIR, "audio")

os.makedirs(FRAMES_DIR, exist_ok=True)
os.makedirs(AUDIO_DIR,  exist_ok=True)

# ── 4. DOWNLOAD MOTION JSON ──────────────────────────────────
print("\n▶ Downloading Motion Data JSON...")
motion_folder_id = get_folder_id("Motion_data", current_service=service)
all_files        = list_files(motion_folder_id, current_service=service)
json_file        = next((f for f in all_files if f["name"].endswith(".json")), None)

if not json_file:
    raise FileNotFoundError("❌ Motion_data folder mein koi .json file nahi mili!")

json_path = os.path.join(WORK_DIR, "motion_data.json")
download_file(json_file["id"], json_path, current_service=service)
print(f"   ✅ Downloaded: {json_file['name']}")

with open(json_path) as f:
    raw = json.load(f)

# Handle both flat array and wrapped {"scenes": [...]}
if isinstance(raw, list) and len(raw) > 0 and isinstance(raw[0], dict) and "scenes" in raw[0]:
    scenes = raw[0]["scenes"]
elif isinstance(raw, list):
    scenes = raw
else:
    raise ValueError("❌ Invalid JSON format")

scenes = sorted(scenes, key=lambda x: x["start_time"])
print(f"   ✅ {len(scenes)} scenes loaded")

# ── 5. DOWNLOAD HOOK AUDIO ───────────────────────────────────
print("\n▶ Downloading Hook Audio (final_mix.mp3)...")
audio_folder_id = get_folder_id("Final_audio", current_service=service)
audio_files     = list_files(audio_folder_id, current_service=service)

# final_mix.mp3 — script_final_mix nahi chahiye
hook_audio = next(
    (f for f in audio_files
     if "final_mix" in f["name"].lower() and "script" not in f["name"].lower()),
    None
)
if not hook_audio:
    raise FileNotFoundError("❌ Final_audio mein 'final_mix.mp3' nahi mila!")

audio_path = os.path.join(AUDIO_DIR, "final_mix.mp3")
download_file(hook_audio["id"], audio_path, current_service=service)
print("   ✅ Audio downloaded!")

audio_dur    = get_duration(audio_path)
total_frames = int(audio_dur * FPS)
print(f"   🎵 Duration: {audio_dur:.2f}s | Frames: {total_frames}")

# ── 6. EASING ────────────────────────────────────────────────
def ease_out_cubic(t):
    return 1 - (1 - min(t, 1.0)) ** 3

def ease_out_bounce(t):
    t = min(t, 1.0)
    if   t < 1/2.75:   return 7.5625 * t * t
    elif t < 2/2.75:   t -= 1.5/2.75;  return 7.5625*t*t + 0.75
    elif t < 2.5/2.75: t -= 2.25/2.75; return 7.5625*t*t + 0.9375
    else:               t -= 2.625/2.75; return 7.5625*t*t + 0.984375

def get_anim_props(frame_in_scene, anim_type):
    anim_frames = int(ANIM_SECS * FPS)
    t = min(frame_in_scene / max(anim_frames, 1), 1.0)
    if   anim_type == "fade_in":   return ease_out_cubic(t), 1.0, 0
    elif anim_type == "pop_in":    e = ease_out_cubic(t); return e, e, 0
    elif anim_type == "zoom_in":   e = ease_out_cubic(t); return e, 0.4 + 0.6*e, 0
    elif anim_type == "slide_up":  e = ease_out_cubic(t); return e, 1.0, int((1-e)*120)
    elif anim_type == "bounce_in": return min(t*3, 1.0), ease_out_bounce(t), 0
    return 1.0, 1.0, 0

# ── 7. ICON FETCH ────────────────────────────────────────────
def fetch_icon(icon_name, hex_color, size=ICON_SIZE):
    url = LUCIDE_CDN.format(icon_name)
    r   = requests.get(url, timeout=10)
    if r.status_code != 200:
        print(f"   ⚠️ Icon '{icon_name}' nahi mila, 'circle' fallback")
        r = requests.get(LUCIDE_CDN.format("circle"), timeout=10)
    svg = r.text
    svg = svg.replace('stroke="currentColor"', f'stroke="{hex_color}"')
    svg = svg.replace('stroke-width="2"',       'stroke-width="2.5"')
    svg = svg.replace('currentColor',            hex_color)
    png = cairosvg.svg2png(bytestring=svg.encode(), output_width=size, output_height=size)
    return Image.open(io.BytesIO(png)).convert("RGBA")

# ── 8. POSITION ──────────────────────────────────────────────
def get_anchor(position, ew, eh):
    M  = 100
    cx = VIDEO_W // 2
    cy = VIDEO_H // 2
    return {
        "center":        (cx - ew//2,           cy - eh//2),
        "left":          (M,                     cy - eh//2),
        "right":         (VIDEO_W - ew - M,      cy - eh//2),
        "top-left":      (M,                     M + 280),
        "top-right":     (VIDEO_W - ew - M,      M + 280),
        "bottom-center": (cx - ew//2,            VIDEO_H - eh - M - 280),
    }.get(position, (cx - ew//2, cy - eh//2))

def hex_to_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))

# ── 9. PRE-FETCH ICONS ───────────────────────────────────────
print("\n▶ Pre-fetching Lucide icons...")
icon_cache = {}
for sc in scenes:
    key = (sc["icon_name"], sc["highlight_color"])
    if key not in icon_cache:
        print(f"   ⬇ {sc['icon_name']} ({sc['highlight_color']})")
        icon_cache[key] = fetch_icon(sc["icon_name"], sc["highlight_color"])
print("   ✅ All icons ready")

# ── 10. PRE-RENDER SCENE GRAPHICS ────────────────────────────
try:
    font = ImageFont.truetype(FONT_PATH, FONT_SIZE)
except Exception:
    font = ImageFont.load_default()

print("\n▶ Pre-rendering scene graphics...")
PR = {}   # prerendered store

for i, sc in enumerate(scenes):
    icon_img = icon_cache[(sc["icon_name"], sc["highlight_color"])]
    text     = sc.get("text", "").strip()
    gap      = 24

    if text:
        dummy = ImageDraw.Draw(Image.new("RGBA", (1,1)))
        bbox  = dummy.textbbox((0,0), text, font=font)
        tw, th = bbox[2]-bbox[0], bbox[3]-bbox[1]
    else:
        tw = th = 0

    ew = max(ICON_SIZE, tw + 20)
    eh = ICON_SIZE + (gap + th if text else 0)

    canvas = Image.new("RGBA", (ew, eh), (0,0,0,0))
    canvas.paste(icon_img, ((ew - ICON_SIZE)//2, 0), icon_img)

    if text:
        draw = ImageDraw.Draw(canvas)
        rgb  = hex_to_rgb(sc["highlight_color"])
        tx   = (ew - tw) // 2
        ty   = ICON_SIZE + gap
        draw.text((tx+2, ty+2), text, font=font, fill=(0,0,0,200))
        draw.text((tx,   ty),   text, font=font, fill=(*rgb, 255))

    ax, ay = get_anchor(sc["position"], ew, eh)
    full   = Image.new("RGBA", (VIDEO_W, VIDEO_H), (0,0,0,0))
    full.paste(canvas, (ax, ay), canvas)

    PR[i] = {"img": full, "ew": ew, "eh": eh, "ax": ax, "ay": ay}
    print(f"   ✅ Scene {sc['scene']} | {sc['icon_name']} | pos={sc['position']}")

# ── 11. RENDER FRAMES ────────────────────────────────────────
print(f"\n▶ Rendering {total_frames} frames @ {FPS}fps...")
bg = Image.new("RGB", (VIDEO_W, VIDEO_H), BG_COLOR)
t0 = time.time()

for f_idx in range(total_frames):
    t     = f_idx / FPS
    frame = bg.copy().convert("RGBA")

    for i, sc in enumerate(scenes):
        s = sc["start_time"]
        e = s + sc["duration"]
        if not (s <= t < e):
            continue

        alpha_mul, scale, offset_y = get_anim_props(
            int((t - s) * FPS), sc.get("animation_in", "fade_in")
        )
        if alpha_mul <= 0:
            continue

        p    = PR[i]
        base = p["img"]

        if scale < 0.99 or offset_y != 0:
            ew, eh = p["ew"], p["eh"]
            ax, ay = p["ax"], p["ay"]
            elem   = base.crop((ax, ay, ax+ew, ay+eh))
            if scale < 0.99:
                nw, nh = max(1, int(ew*scale)), max(1, int(eh*scale))
                elem   = elem.resize((nw, nh), Image.LANCZOS)
                ax     = ax + (ew-nw)//2
                ay     = ay + (eh-nh)//2
                ew, eh = nw, nh
            r,g,b,a = elem.split()
            a       = a.point(lambda px: int(px * alpha_mul))
            elem    = Image.merge("RGBA", (r,g,b,a))
            overlay = Image.new("RGBA", (VIDEO_W, VIDEO_H), (0,0,0,0))
            overlay.paste(elem, (ax, ay + offset_y), elem)
        else:
            r,g,b,a = base.split()
            a       = a.point(lambda px: int(px * alpha_mul))
            overlay = Image.merge("RGBA", (r,g,b,a))

        frame = Image.alpha_composite(frame, overlay)

    frame.convert("RGB").save(os.path.join(FRAMES_DIR, f"frame_{f_idx:06d}.png"))

    if f_idx % 90 == 0:
        elapsed = time.time() - t0
        eta     = (elapsed / max(f_idx, 1)) * (total_frames - f_idx)
        print(f"   {f_idx}/{total_frames} | ETA: {eta:.0f}s")

print(f"✅ Frames done in {time.time()-t0:.1f}s")

# ── 12. FFMPEG ENCODE ────────────────────────────────────────
print("\n▶ Encoding with FFmpeg...")
output_path = os.path.join(WORK_DIR, "motion_hook.mp4")
ffmpeg_cmd  = (
    f'ffmpeg -y '
    f'-r {FPS} -i "{FRAMES_DIR}/frame_%06d.png" '
    f'-i "{audio_path}" '
    f'-c:v libx264 -preset fast -crf 20 -pix_fmt yuv420p '
    f'-c:a aac -b:a 192k -shortest '
    f'"{output_path}"'
)
result = subprocess.run(ffmpeg_cmd, shell=True, capture_output=True, text=True)
if result.returncode != 0:
    print(result.stderr[-2000:])
    raise RuntimeError("❌ FFmpeg crash ho gaya!")
print("✅ Video encoded!")

# ── 13. RE-AUTHENTICATE & UPLOAD ─────────────────────────────
print("\n▶ Render ke baad re-authenticating...")
fresh_service   = get_drive_service()
video_folder_id = get_folder_id("Video", current_service=fresh_service)
upload_file(output_path, "motion_hook.mp4", video_folder_id, current_service=fresh_service)

# ── 14. CLEANUP ──────────────────────────────────────────────
shutil.rmtree(WORK_DIR)
print("🧹 Cleanup done. All finished!")
