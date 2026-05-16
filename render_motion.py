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
VIDEO_W    = 1080
VIDEO_H    = 1920
FPS        = 60          # ✨ 60fps for smooth animations
BG_COLOR   = (10, 10, 10)
ANIM_SECS  = 0.45        # entrance/exit duration
ICON_Y     = 880         # main icon vertical center
TEXT_Y     = 1200        # karaoke text Y position
LUCIDE_CDN = "https://cdn.jsdelivr.net/npm/lucide-static@0.441.0/icons/{}.svg"
FONT_PATH  = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
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

def ease_out_cubic(t):
    return 1 - (1 - min(t, 1.0)) ** 3

def ease_out_bounce(t):
    t = min(t, 1.0)
    if   t < 1/2.75:   return 7.5625*t*t
    elif t < 2/2.75:   t -= 1.5/2.75;  return 7.5625*t*t+0.75
    elif t < 2.5/2.75: t -= 2.25/2.75; return 7.5625*t*t+0.9375
    else:               t -= 2.625/2.75;return 7.5625*t*t+0.984375

# ── 4. ANIMATION CALCULATOR ──────────────────────────────────
def get_anim_state(t_local, duration, anim_type):
    """
    Returns (alpha, scale, line_progress)
    - alpha:         0.0 to 1.0
    - scale:         for icon scaling
    - line_progress: 0.0 to 1.0 — how much of line is drawn
    """
    af = ANIM_SECS   # entrance duration
    ef = ANIM_SECS   # exit duration

    # ── Entrance ──────────────────────────
    if t_local < af:
        t_in = t_local / af
        if   anim_type == "fade_in":   alpha=ease_out_expo(t_in); scale=1.0
        elif anim_type == "pop_in":    e=ease_out_back(t_in); alpha=ease_out_expo(t_in); scale=e
        elif anim_type == "zoom_in":   e=ease_out_expo(t_in); alpha=e; scale=0.4+0.6*e
        elif anim_type == "slide_up":  alpha=ease_out_expo(t_in); scale=1.0
        elif anim_type == "bounce_in": alpha=min(t_in*3,1.0); scale=ease_out_bounce(t_in)
        else:                          alpha=ease_out_expo(t_in); scale=1.0
        line_progress = alpha

    # ── Exit ──────────────────────────────
    elif t_local > duration - ef:
        t_out = (t_local - (duration - ef)) / ef
        t_out = min(t_out, 1.0)
        alpha = 1.0 - t_out
        scale = 1.0 - 0.08 * t_out   # subtle shrink on exit
        line_progress = alpha

    # ── Idle: breathing ───────────────────
    else:
        idle_t = t_local - af
        alpha  = 1.0
        scale  = 1.0 + 0.012 * math.sin(idle_t * 1.8)  # ✨ breathing
        line_progress = 1.0

    return alpha, scale, line_progress

# ── 5. FILM GRAIN ────────────────────────────────────────────
def apply_grain(image):
    """✨ Adds subtle organic texture to the frame."""
    grain = Image.effect_noise((VIDEO_W, VIDEO_H), 12)
    grain = grain.convert("RGBA")
    base  = image.convert("RGBA")
    try:
        result = ImageChops.overlay(base, grain)
    except AttributeError:
        # Fallback for older Pillow: soft light blend manually
        result = Image.blend(base, grain, 0.04)
    return result.convert("RGB")

# ── 6. ICON FETCH ────────────────────────────────────────────
def hex_to_rgb(h):
    h = h.lstrip("#"); return tuple(int(h[i:i+2],16) for i in (0,2,4))

def fetch_icon(name, hex_color, size):
    url = LUCIDE_CDN.format(name)
    r   = requests.get(url, timeout=10)
    if r.status_code != 200:
        print(f"   ⚠️ '{name}' nahi mila, circle fallback")
        r = requests.get(LUCIDE_CDN.format("circle"), timeout=10)
    svg = r.text
    svg = svg.replace('stroke="currentColor"', f'stroke="{hex_color}"')
    svg = svg.replace('stroke-width="2"', 'stroke-width="2.5"')
    svg = svg.replace('currentColor', hex_color)
    png = cairosvg.svg2png(bytestring=svg.encode(), output_width=size, output_height=size)
    return Image.open(io.BytesIO(png)).convert("RGBA")

# ── 7. DOWNLOAD JSON ─────────────────────────────────────────
print("\n▶ Downloading Motion JSON...")
mid   = get_folder_id("Motion_data", current_service=service)
files = list_files(mid, current_service=service)
jf    = next((f for f in files if f["name"].endswith(".json")), None)
if not jf: raise FileNotFoundError("❌ .json nahi mila in Motion_data!")

jp = os.path.join(WORK_DIR, "motion_data.json")
os.makedirs(WORK_DIR, exist_ok=True)
download_file(jf["id"], jp, current_service=service)
print(f"   ✅ {jf['name']}")

with open(jp) as f: raw = json.load(f)
if isinstance(raw, list) and raw and "scenes" in raw[0]:
    scenes = raw[0]["scenes"]
else:
    scenes = raw if isinstance(raw, list) else []
scenes = sorted(scenes, key=lambda x: x["start_time"])
print(f"   ✅ {len(scenes)} scenes loaded")

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

# ── 9. PRE-FETCH ICONS ───────────────────────────────────────
print("\n▶ Pre-fetching icons...")
icon_cache = {}
for sc in scenes:
    for el in sc.get("elements", []):
        if el["type"] == "icon":
            key = (el["name"], sc["highlight_color"], el.get("size", 240))
            if key not in icon_cache:
                print(f"   ⬇ {el['name']} sz={el.get('size',240)}")
                icon_cache[key] = fetch_icon(el["name"], sc["highlight_color"], el.get("size",240))
print("   ✅ Icons ready")

# ── 10. FONT ─────────────────────────────────────────────────
try:
    font_main  = ImageFont.truetype(FONT_PATH, 56)
    font_word  = ImageFont.truetype(FONT_PATH, 52)
except Exception:
    font_main = font_word = ImageFont.load_default()

# ── 11. DRAW SCENE ELEMENTS ──────────────────────────────────
def draw_scene(frame_rgba, scene, t):
    s_time = scene["start_time"]
    dur    = scene["duration"]
    t_local= t - s_time

    alpha, scale, line_prog = get_anim_state(t_local, dur,
                              scene.get("animation_in", "fade_in"))
    if alpha <= 0.01:
        return frame_rgba

    color = scene.get("highlight_color", "#3B82F6")
    rgb   = hex_to_rgb(color)
    a255  = int(255 * alpha)

    overlay = Image.new("RGBA", (VIDEO_W, VIDEO_H), (0,0,0,0))
    draw    = ImageDraw.Draw(overlay)

    for el in scene.get("elements", []):

        # ── ✨ Animated Lines (grow from start to end) ──
        if el["type"] == "line":
            sx, sy = el["start_pos"]
            ex, ey = el["end_pos"]
            # Current endpoint grows with line_progress
            curr_ex = sx + (ex - sx) * line_prog
            curr_ey = sy + (ey - sy) * line_prog
            draw.line([(sx, sy), (curr_ex, curr_ey)],
                      fill=(*rgb, a255), width=el.get("thickness", 3))

        # ── Circles ──
        elif el["type"] == "circle":
            cx2, cy2 = el["x"], el["y"]
            r = el.get("radius", 14) * scale
            r = max(3, r)
            # Circles appear only after line is 80% drawn
            if line_prog >= 0.8:
                circle_alpha = int(a255 * min((line_prog - 0.8) / 0.2, 1.0))
                bb = [cx2-r, cy2-r, cx2+r, cy2+r]
                if el.get("fill", True):
                    draw.ellipse(bb, fill=(*rgb, circle_alpha))
                else:
                    draw.ellipse(bb, outline=(*rgb, circle_alpha), width=2)

        # ── Icon (with scale + breathing) ──
        elif el["type"] == "icon":
            sz  = el.get("size", 240)
            key = (el["name"], color, sz)
            img = icon_cache.get(key)
            if img is None: continue

            scaled_sz = max(10, int(sz * scale))
            if scaled_sz != sz:
                img = img.resize((scaled_sz, scaled_sz), Image.LANCZOS)

            ix = el["x"] - scaled_sz // 2
            iy = el["y"] - scaled_sz // 2

            r2,g2,b2,a2 = img.split()
            a2 = a2.point(lambda p: int(p * alpha))
            icon_final = Image.merge("RGBA",(r2,g2,b2,a2))
            overlay.paste(icon_final, (int(ix), int(iy)), icon_final)

    # ── ✨ Karaoke Text ──────────────────────────────────────
    words = scene.get("words", [])
    text  = scene.get("text", "").strip()

    if words:
        # Word-by-word highlight
        dummy = ImageDraw.Draw(Image.new("RGBA",(1,1)))
        all_text = " ".join(w["word"] for w in words)
        total_w  = dummy.textbbox((0,0), all_text+" ", font=font_word)[2]
        curr_x   = (VIDEO_W - total_w) // 2
        ty       = TEXT_Y

        for w in words:
            w_str = w["word"] + " "
            is_active = w["start"] <= t <= w["end"]
            w_rgb = rgb if is_active else (160, 160, 160)
            w_a   = 255 if is_active else int(180 * alpha)
            draw.text((curr_x+2, ty+2), w_str, font=font_word, fill=(0,0,0,w_a))
            draw.text((curr_x,   ty),   w_str, font=font_word, fill=(*w_rgb, w_a))
            curr_x += dummy.textbbox((0,0), w_str, font=font_word)[2]

    elif text:
        # Static label below icon
        dummy = ImageDraw.Draw(Image.new("RGBA",(1,1)))
        bb = dummy.textbbox((0,0), text, font=font_main)
        tw = bb[2]-bb[0]
        tx = (VIDEO_W-tw)//2
        ty = TEXT_Y
        draw.text((tx+2, ty+2), text, font=font_main, fill=(0,0,0,a255))
        draw.text((tx,   ty),   text, font=font_main, fill=(*rgb, a255))

    return Image.alpha_composite(frame_rgba, overlay)

# ── 12. FRAME LOOP ───────────────────────────────────────────
print(f"\n▶ Rendering {total_frames} frames @ {FPS}fps...")
bg = Image.new("RGB", (VIDEO_W, VIDEO_H), BG_COLOR)
t0 = time.time()

for fi in range(total_frames):
    t     = fi / FPS
    frame = bg.copy().convert("RGBA")

    for sc in scenes:
        s = sc["start_time"]; e = s + sc["duration"]
        if s <= t < e:
            frame = draw_scene(frame, sc, t)

    # ✨ Apply film grain to final composited frame
    final = apply_grain(frame)
    final.save(os.path.join(FRAMES_DIR, f"frame_{fi:06d}.png"))

    if fi % 180 == 0:  # every 3 seconds at 60fps
        el  = time.time()-t0
        eta = (el/max(fi,1))*(total_frames-fi)
        print(f"   {fi}/{total_frames} | ETA: {eta:.0f}s")

print(f"✅ Frames done in {time.time()-t0:.1f}s")

# ── 13. ENCODE ───────────────────────────────────────────────
print("\n▶ FFmpeg encode...")
out = os.path.join(WORK_DIR, "motion_hook.mp4")
res = subprocess.run(
    f'ffmpeg -y -r {FPS} -i "{FRAMES_DIR}/frame_%06d.png" -i "{ap}" '
    f'-c:v libx264 -preset fast -crf 18 -pix_fmt yuv420p '
    f'-c:a aac -b:a 192k -shortest "{out}"',
    shell=True, capture_output=True, text=True
)
if res.returncode != 0:
    print(res.stderr[-2000:]); raise RuntimeError("❌ FFmpeg fail!")
print("✅ Encoded!")

# ── 14. UPLOAD ───────────────────────────────────────────────
print("\n▶ Re-authenticating before upload...")
fs  = get_drive_service()
vid = get_folder_id("Video", current_service=fs)
upload_file(out, "motion_hook.mp4", vid, current_service=fs)

shutil.rmtree(WORK_DIR)
print("🧹 Done!")
