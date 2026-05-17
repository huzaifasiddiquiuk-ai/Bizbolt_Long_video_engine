import os, io, json, math, shutil, subprocess, time, requests, sys
from PIL import Image, ImageDraw, ImageFont, ImageChops

try:
    import cairosvg
except ImportError:
    cairosvg = None
    print("⚠️ cairosvg nahi mila — SVG assets skip honge")

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload

# ═══════════════════════════════════════════════════════════════
# 1. FONTS — Auto Download (No GitHub, No Pre-install needed)
# ═══════════════════════════════════════════════════════════════

FONT_DIR = "/tmp/fonts"
os.makedirs(FONT_DIR, exist_ok=True)

# Poppins Bold — sabse clean aur modern for video subtitles
FONTS_TO_DOWNLOAD = {
    "bold":        ("Poppins-Bold.ttf",        "https://github.com/google/fonts/raw/main/ofl/poppins/Poppins-Bold.ttf"),
    "semibold":    ("Poppins-SemiBold.ttf",    "https://github.com/google/fonts/raw/main/ofl/poppins/Poppins-SemiBold.ttf"),
    "extrabold":   ("Poppins-ExtraBold.ttf",   "https://github.com/google/fonts/raw/main/ofl/poppins/Poppins-ExtraBold.ttf"),
}

def download_fonts():
    print("\n▶ Fonts download ho rahe hain...")
    downloaded = {}
    for key, (filename, url) in FONTS_TO_DOWNLOAD.items():
        path = os.path.join(FONT_DIR, filename)
        if os.path.exists(path):
            print(f"   ✅ Already exists: {filename}")
            downloaded[key] = path
            continue
        retries = 3
        for attempt in range(retries):
            try:
                r = requests.get(url, timeout=20)
                r.raise_for_status()
                with open(path, "wb") as f:
                    f.write(r.content)
                print(f"   ✅ Downloaded: {filename}")
                downloaded[key] = path
                break
            except Exception as e:
                print(f"   ⚠️ Attempt {attempt+1}/{retries} failed for {filename}: {e}")
                time.sleep(2)
        else:
            print(f"   ❌ Font download fail: {filename} — PIL default use hoga")
            downloaded[key] = None
    return downloaded

font_paths = download_fonts()

FONT_SIZES = {"large": 72, "medium": 52, "small": 40}

def load_fonts(font_path_key="bold"):
    path = font_paths.get(font_path_key)
    loaded = {}
    for key, size in FONT_SIZES.items():
        try:
            if path and os.path.exists(path):
                loaded[key] = ImageFont.truetype(path, size)
            else:
                loaded[key] = ImageFont.load_default()
        except Exception as e:
            print(f"   ⚠️ Font load error ({key}): {e}")
            loaded[key] = ImageFont.load_default()
    return loaded

fonts = load_fonts("bold")
print(f"   ✅ Fonts ready: {list(fonts.keys())}")

# ═══════════════════════════════════════════════════════════════
# 2. GOOGLE DRIVE AUTH
# ═══════════════════════════════════════════════════════════════

def get_drive_service():
    print("🔄 Drive se naya connection bana raha hu...")
    try:
        creds_data = json.loads(os.environ["DRIVE_CREDENTIALS"])
        token_data = json.loads(os.environ["YOUTUBE_TOKEN"])
    except KeyError as e:
        raise EnvironmentError(f"❌ Environment variable nahi mila: {e}")
    except json.JSONDecodeError as e:
        raise ValueError(f"❌ JSON parse error credentials mein: {e}")

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
        try:
            creds.refresh(Request())
        except Exception as e:
            raise RuntimeError(f"❌ Token refresh fail: {e}")

    return build("drive", "v3", credentials=creds, cache_discovery=False)

service = get_drive_service()
print("✅ Drive authenticated!")

MAIN_FOLDER_ID = os.environ.get("MAIN_FOLDER_ID")
if not MAIN_FOLDER_ID:
    raise EnvironmentError("❌ MAIN_FOLDER_ID environment variable set nahi hai!")

# ═══════════════════════════════════════════════════════════════
# 3. DRIVE HELPERS
# ═══════════════════════════════════════════════════════════════

def get_folder_id(name, parent_id=MAIN_FOLDER_ID, current_service=service):
    try:
        res = current_service.files().list(
            q=f"name='{name}' and '{parent_id}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false",
            fields="files(id,name)"
        ).execute()
        files = res.get("files", [])
        if not files:
            raise FileNotFoundError(f"❌ Folder nahi mila: '{name}'")
        return files[0]["id"]
    except FileNotFoundError:
        raise
    except Exception as e:
        raise RuntimeError(f"❌ Folder search fail '{name}': {e}")

def list_files(folder_id, current_service=service):
    try:
        return current_service.files().list(
            q=f"'{folder_id}' in parents and trashed=false",
            fields="files(id,name)", orderBy="name"
        ).execute().get("files", [])
    except Exception as e:
        raise RuntimeError(f"❌ Files list fail: {e}")

def download_file(file_id, local_path, current_service=service):
    try:
        req = current_service.files().get_media(fileId=file_id)
        with open(local_path, "wb") as f:
            dl = MediaIoBaseDownload(f, req)
            done = False
            while not done:
                _, done = dl.next_chunk()
    except Exception as e:
        raise RuntimeError(f"❌ Download fail (id={file_id}): {e}")

def upload_file(local_path, name, parent_id, current_service):
    if not os.path.exists(local_path):
        raise FileNotFoundError(f"❌ Upload karne ke liye file nahi mili: {local_path}")

    print(f"   🔄 Uploading: {name}...")
    media = MediaFileUpload(local_path, mimetype="video/mp4", resumable=True, chunksize=5*1024*1024)
    req = current_service.files().create(
        body={"name": name, "parents": [parent_id]},
        media_body=media, fields="id"
    )
    response = None
    retries = 0
    while response is None:
        try:
            status, response = req.next_chunk()
            if status:
                print(f"   📤 {int(status.progress() * 100)}%")
            retries = 0
        except Exception as e:
            retries += 1
            if retries > 10:
                raise RuntimeError(f"❌ Upload fail after 10 retries: {e}")
            print(f"   ⚠️ Retry {retries}/10... ({e})")
            time.sleep(5)
    print(f"   ✅ Uploaded: {name}")

def get_duration(path):
    if not os.path.exists(path):
        raise FileNotFoundError(f"❌ Audio file nahi mili: {path}")
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True
    )
    if r.returncode != 0:
        raise RuntimeError(f"❌ ffprobe fail: {r.stderr}")
    try:
        return float(r.stdout.strip())
    except ValueError:
        raise RuntimeError(f"❌ Duration parse nahi hua: '{r.stdout.strip()}'")

# ═══════════════════════════════════════════════════════════════
# 4. VIDEO CONFIG
# ═══════════════════════════════════════════════════════════════

VIDEO_W    = 1920
VIDEO_H    = 1080
FPS        = 60
BG_COLOR   = (255, 255, 255)
ANIM_SECS  = 0.35

WORK_DIR   = "/tmp/motion_work"
FRAMES_DIR = os.path.join(WORK_DIR, "frames")
AUDIO_DIR  = os.path.join(WORK_DIR, "audio")

os.makedirs(FRAMES_DIR, exist_ok=True)
os.makedirs(AUDIO_DIR,  exist_ok=True)

# ═══════════════════════════════════════════════════════════════
# 5. EASING FUNCTIONS
# ═══════════════════════════════════════════════════════════════

def ease_out_expo(t):
    return 1.0 if t >= 1.0 else 1 - math.pow(2, -10 * t)

def ease_out_back(t):
    c1, c3 = 1.70158, 2.70158
    return 1 + c3 * math.pow(t - 1, 3) + c1 * math.pow(t - 1, 2)

def ease_out_bounce(t):
    t = min(t, 1.0)
    if   t < 1/2.75:   return 7.5625 * t * t
    elif t < 2/2.75:   t -= 1.5/2.75;  return 7.5625*t*t + 0.75
    elif t < 2.5/2.75: t -= 2.25/2.75; return 7.5625*t*t + 0.9375
    else:               t -= 2.625/2.75;return 7.5625*t*t + 0.984375

# ═══════════════════════════════════════════════════════════════
# 6. ANIMATION STATE
# ═══════════════════════════════════════════════════════════════

def get_anim_state(t_local, duration):
    af = ANIM_SECS
    ef = ANIM_SECS

    if t_local < af:
        t_in  = t_local / af
        alpha = ease_out_expo(t_in)
        scale = ease_out_back(t_in)
        scale = max(0.01, scale)
    elif t_local > duration - ef and duration > ef * 2:
        t_out = (t_local - (duration - ef)) / ef
        t_out = min(t_out, 1.0)
        alpha = 1.0 - t_out
        scale = 1.0 - 0.05 * t_out
    else:
        idle_t = t_local - af
        alpha  = 1.0
        scale  = 1.0 + 0.007 * math.sin(idle_t * 1.8)

    return max(0.0, min(1.0, alpha)), scale

# ═══════════════════════════════════════════════════════════════
# 7. FILM GRAIN
# ═══════════════════════════════════════════════════════════════

def apply_grain(image):
    try:
        grain  = Image.effect_noise((VIDEO_W, VIDEO_H), 6)
        grain  = grain.convert("RGBA")
        base   = image.convert("RGBA")
        result = Image.blend(base, grain, 0.02)
        return result.convert("RGB")
    except Exception:
        return image  # grain fail ho toh original return karo

# ═══════════════════════════════════════════════════════════════
# 8. ASSET FETCH
# ═══════════════════════════════════════════════════════════════

def fetch_asset(raw_url, width, height):
    width  = max(1, width)
    height = max(1, height)

    try:
        r = requests.get(raw_url, timeout=20)
        r.raise_for_status()
    except requests.exceptions.Timeout:
        raise Exception(f"Timeout: {raw_url}")
    except requests.exceptions.HTTPError as e:
        raise Exception(f"HTTP {r.status_code}: {raw_url}")
    except Exception as e:
        raise Exception(f"Network error: {e}")

    try:
        if raw_url.lower().endswith(".svg"):
            if cairosvg is None:
                raise Exception("cairosvg nahi hai, SVG skip")
            png = cairosvg.svg2png(bytestring=r.content, output_width=width, output_height=height)
            return Image.open(io.BytesIO(png)).convert("RGBA")
        else:
            img = Image.open(io.BytesIO(r.content)).convert("RGBA")
            return img.resize((width, height), Image.LANCZOS)
    except Exception as e:
        raise Exception(f"Image parse fail: {e}")

# ═══════════════════════════════════════════════════════════════
# 9. TEXT WRAP
# ═══════════════════════════════════════════════════════════════

def wrap_text(text, font, max_width):
    words   = text.split()
    lines   = []
    current = ""
    dummy   = ImageDraw.Draw(Image.new("RGBA", (1, 1)))

    for word in words:
        test = (current + " " + word).strip()
        try:
            w = dummy.textbbox((0, 0), test, font=font)[2]
        except Exception:
            w = len(test) * 20  # fallback estimate
        if w <= max_width:
            current = test
        else:
            if current:
                lines.append(current)
            current = word

    if current:
        lines.append(current)
    return lines if lines else [text]

# ═══════════════════════════════════════════════════════════════
# 10. SUBTITLE RENDERER
# ═══════════════════════════════════════════════════════════════

def draw_subtitle(overlay, draw, text, position, font_size_key, active_visual=None):
    if not text or not text.strip():
        return

    font      = fonts.get(font_size_key, fonts["medium"])
    max_w     = int(VIDEO_W * 0.80)
    lines     = wrap_text(text, font, max_w)
    line_h    = FONT_SIZES.get(font_size_key, 52) + 14
    total_h   = len(lines) * line_h
    dummy     = ImageDraw.Draw(Image.new("RGBA", (1, 1)))

    # Y position
    if position == "below_visual" and active_visual:
        vis_bottom = active_visual.get("y", 0) + active_visual.get("height", 0)
        text_y     = min(vis_bottom + 30, VIDEO_H - total_h - 20)
    elif position == "center_screen":
        text_y = (VIDEO_H - total_h) // 2
    elif position in ("left", "right"):
        text_y = (VIDEO_H - total_h) // 2
    else:
        text_y = VIDEO_H - total_h - 60

    text_y = max(10, min(text_y, VIDEO_H - total_h - 10))

    for i, line in enumerate(lines):
        try:
            lw = dummy.textbbox((0, 0), line, font=font)[2]
        except Exception:
            lw = len(line) * 20

        y = text_y + i * line_h

        # X position
        if position == "left":
            x = 60
        elif position == "right":
            x = max(60, VIDEO_W - lw - 60)
        else:
            x = max(0, (VIDEO_W - lw) // 2)

        # Shadow for readability
        shadow_offsets = [(2, 2), (-1, -1), (2, -1), (-1, 2)]
        for dx, dy in shadow_offsets:
            try:
                draw.text((x + dx, y + dy), line, font=font, fill=(200, 200, 200, 180))
            except Exception:
                pass

        # Main text — crisp black
        try:
            draw.text((x, y), line, font=font, fill=(10, 10, 10, 255))
        except Exception as e:
            print(f"   ⚠️ Text draw fail: {e}")

# ═══════════════════════════════════════════════════════════════
# 11. DOWNLOAD JSON DATA
# ═══════════════════════════════════════════════════════════════

print("\n▶ Visual JSON download ho raha hai...")
try:
    mid    = get_folder_id("Motion_data")
    mfiles = list_files(mid)
    jf     = next((f for f in mfiles if f["name"].endswith(".json")), None)
    if not jf:
        raise FileNotFoundError("❌ Motion_data folder mein koi .json nahi mila!")

    jp = os.path.join(WORK_DIR, "visuals.json")
    os.makedirs(WORK_DIR, exist_ok=True)
    download_file(jf["id"], jp)

    with open(jp, "r", encoding="utf-8") as f:
        raw = json.load(f)

    visuals   = sorted(raw.get("visuals",   []), key=lambda v: v.get("start", 0))
    subtitles = sorted(raw.get("subtitles", []), key=lambda s: s.get("start", 0))

    if not subtitles:
        print("   ⚠️ Koi subtitles nahi mili JSON mein!")
    print(f"   ✅ {len(visuals)} visuals, {len(subtitles)} subtitles loaded")

except Exception as e:
    print(f"❌ JSON load fail: {e}")
    sys.exit(1)

# ═══════════════════════════════════════════════════════════════
# 12. DOWNLOAD AUDIO
# ═══════════════════════════════════════════════════════════════

print("\n▶ Audio download ho raha hai...")
try:
    aid    = get_folder_id("Final_audio")
    afiles = list_files(aid)
    af2    = next(
        (f for f in afiles
         if "final_mix" in f["name"].lower() and "script" not in f["name"].lower()),
        None
    )
    if not af2:
        raise FileNotFoundError("❌ Final_audio mein 'final_mix' file nahi mili!")

    ap = os.path.join(AUDIO_DIR, "final_mix.mp3")
    download_file(af2["id"], ap)

    audio_dur    = get_duration(ap)
    total_frames = int(audio_dur * FPS)
    print(f"   ✅ Duration: {audio_dur:.2f}s | Total frames: {total_frames} @ {FPS}fps")

except Exception as e:
    print(f"❌ Audio load fail: {e}")
    sys.exit(1)

# ═══════════════════════════════════════════════════════════════
# 13. PRE-CACHE ASSETS
# ═══════════════════════════════════════════════════════════════

print("\n▶ GitHub se assets fetch ho rahe hain...")
asset_cache = {}
failed_urls = set()

for vis in visuals:
    key = vis.get("raw_url", "")
    if not key:
        print(f"   ⚠️ Visual '{vis.get('id', '?')}' mein raw_url nahi hai — skip")
        failed_urls.add(key)
        continue
    if key in asset_cache:
        continue

    fname = vis.get("asset_path", key).split("/")[-1]
    w     = vis.get("width", 400)
    h     = vis.get("height", 400)

    for attempt in range(3):
        try:
            print(f"   ⬇ {fname}")
            asset_cache[key] = fetch_asset(key, w, h)
            break
        except Exception as e:
            if attempt < 2:
                print(f"   ⚠️ Retry {attempt+1}/3: {fname} → {e}")
                time.sleep(1)
            else:
                print(f"   ❌ Failed (3 tries): {fname} → {e}")
                asset_cache[key] = None
                failed_urls.add(key)

ok_count   = sum(1 for v in asset_cache.values() if v is not None)
fail_count = len(failed_urls)
print(f"   ✅ {ok_count} assets ready | ❌ {fail_count} failed (subtitle fallback use hoga)")

# ═══════════════════════════════════════════════════════════════
# 14. DRAW FRAME
# ═══════════════════════════════════════════════════════════════

def draw_frame(t):
    frame   = Image.new("RGBA", (VIDEO_W, VIDEO_H), (*BG_COLOR, 255))
    overlay = Image.new("RGBA", (VIDEO_W, VIDEO_H), (0, 0, 0, 0))
    draw    = ImageDraw.Draw(overlay)

    active_vis  = sorted(
        [v for v in visuals  if v.get("start", 0) <= t < v.get("end", 0)],
        key=lambda v: v.get("z_index", 0)
    )
    active_subs = [s for s in subtitles if s.get("start", 0) <= t < s.get("end", 0)]

    rendered_vis = {}

    for vis in active_vis:
        key = vis.get("raw_url", "")
        img = asset_cache.get(key)
        if img is None:
            continue

        t_local  = t - vis.get("start", 0)
        duration = vis.get("end", 0) - vis.get("start", 0)
        alpha, scale = get_anim_state(t_local, max(duration, 0.01))

        if alpha <= 0.01:
            continue

        vw = max(1, vis.get("width", 400))
        vh = max(1, vis.get("height", 400))
        w  = max(1, int(vw * scale))
        h  = max(1, int(vh * scale))

        try:
            img_s = img.resize((w, h), Image.LANCZOS) if (w != img.width or h != img.height) else img.copy()
        except Exception as e:
            print(f"   ⚠️ Resize fail: {e}")
            continue

        try:
            r2, g2, b2, a2 = img_s.split()
            a2 = a2.point(lambda p: int(p * alpha))
            img_final = Image.merge("RGBA", (r2, g2, b2, a2))
        except Exception as e:
            print(f"   ⚠️ Alpha apply fail: {e}")
            continue

        x = int(vis.get("x", 0) + (vw - w) / 2)
        y = int(vis.get("y", 0) + (vh - h) / 2)
        x = max(0, min(x, VIDEO_W - w))
        y = max(0, min(y, VIDEO_H - h))

        try:
            overlay.paste(img_final, (x, y), img_final)
            rendered_vis[vis.get("id", "")] = vis
        except Exception as e:
            print(f"   ⚠️ Paste fail: {e}")

    # Subtitles
    for sub in active_subs:
        position      = sub.get("position", "below_visual")
        font_size_key = sub.get("font_size", "medium")
        text          = sub.get("text", "").strip()
        if not text:
            continue

        matching_vis = None
        for vis in active_vis:
            if (vis.get("start", 0) <= sub.get("start", 0) and
                vis.get("end",   0) >= sub.get("end",   0) and
                vis.get("raw_url", "") not in failed_urls):
                matching_vis = vis
                break

        if matching_vis is None and position == "below_visual":
            position      = "center_screen"
            font_size_key = "large"

        draw_subtitle(overlay, draw, text, position, font_size_key, active_visual=matching_vis)

    # Fallback — koi bhi active nahi toh nearest subtitle dikhao
    if not active_vis and not active_subs and subtitles:
        nearest = min(subtitles, key=lambda s: abs(s.get("start", 0) - t), default=None)
        if nearest and abs(nearest.get("start", 0) - t) < 2.0:
            draw_subtitle(overlay, draw, nearest.get("text", ""), "center_screen", "large")

    try:
        return Image.alpha_composite(frame, overlay).convert("RGB")
    except Exception as e:
        print(f"   ⚠️ Composite fail at t={t:.2f}: {e}")
        return frame.convert("RGB")

# ═══════════════════════════════════════════════════════════════
# 15. FRAME RENDER LOOP
# ═══════════════════════════════════════════════════════════════

print(f"\n▶ {total_frames} frames render ho rahe hain @ {FPS}fps...")
t0            = time.time()
failed_frames = []

for fi in range(total_frames):
    t = fi / FPS
    try:
        rendered = apply_grain(draw_frame(t))
        rendered.save(os.path.join(FRAMES_DIR, f"frame_{fi:06d}.png"))
    except Exception as e:
        print(f"   ❌ Frame {fi} fail: {e} — black frame use hoga")
        failed_frames.append(fi)
        Image.new("RGB", (VIDEO_W, VIDEO_H), BG_COLOR).save(
            os.path.join(FRAMES_DIR, f"frame_{fi:06d}.png")
        )

    if fi % 300 == 0 or fi == total_frames - 1:
        el  = time.time() - t0
        eta = (el / max(fi, 1)) * (total_frames - fi)
        pct = fi / total_frames * 100
        print(f"   [{pct:.1f}%] {fi}/{total_frames} | ETA: {eta:.0f}s")

elapsed = time.time() - t0
print(f"✅ Frames done in {elapsed:.1f}s")
if failed_frames:
    print(f"   ⚠️ {len(failed_frames)} frames fail hue (black use kiya)")

# ═══════════════════════════════════════════════════════════════
# 16. FFMPEG ENCODE
# ═══════════════════════════════════════════════════════════════

print("\n▶ FFmpeg se video encode ho raha hai...")
out_path = os.path.join(WORK_DIR, "motion_video.mp4")

ffmpeg_cmd = (
    f'ffmpeg -y -r {FPS} -i "{FRAMES_DIR}/frame_%06d.png" -i "{ap}" '
    f'-c:v libx264 -preset fast -crf 18 -pix_fmt yuv420p '
    f'-c:a aac -b:a 192k -shortest "{out_path}"'
)

res = subprocess.run(ffmpeg_cmd, shell=True, capture_output=True, text=True)

if res.returncode != 0:
    print("❌ FFmpeg error output:")
    print(res.stderr[-3000:])
    raise RuntimeError("FFmpeg encode fail hua!")

if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
    raise RuntimeError("❌ FFmpeg chala par output file empty/missing hai!")

size_mb = os.path.getsize(out_path) / (1024 * 1024)
print(f"✅ Encode complete! Size: {size_mb:.1f} MB")

# ═══════════════════════════════════════════════════════════════
# 17. UPLOAD TO DRIVE
# ═══════════════════════════════════════════════════════════════

print("\n▶ Upload se pehle re-authenticate kar raha hu...")
try:
    fs  = get_drive_service()
    vid = get_folder_id("Video", current_service=fs)
    upload_file(out_path, "motion_video.mp4", vid, current_service=fs)
except Exception as e:
    print(f"❌ Upload fail: {e}")
    print(f"   📁 Local file yahan hai: {out_path}")
    sys.exit(1)

# ═══════════════════════════════════════════════════════════════
# 18. CLEANUP
# ═══════════════════════════════════════════════════════════════

try:
    shutil.rmtree(WORK_DIR)
    print("🧹 Temp files clean ho gaye!")
except Exception as e:
    print(f"⚠️ Cleanup fail (ignore kar sakte ho): {e}")

print("\n🎉 Sab kuch ho gaya! Video Drive pe upload ho gaya.")
