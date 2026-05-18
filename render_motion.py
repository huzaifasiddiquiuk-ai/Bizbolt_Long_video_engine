import os, io, json, math, shutil, subprocess, time, requests, sys
from PIL import Image, ImageDraw, ImageFont, ImageFilter

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
# 1. FONTS
# ═══════════════════════════════════════════════════════════════

FONT_DIR = "/tmp/fonts"
os.makedirs(FONT_DIR, exist_ok=True)

FONTS_TO_DOWNLOAD = {
    "bold":      ("Poppins-Bold.ttf",      "https://github.com/google/fonts/raw/main/ofl/poppins/Poppins-Bold.ttf"),
    "semibold":  ("Poppins-SemiBold.ttf",  "https://github.com/google/fonts/raw/main/ofl/poppins/Poppins-SemiBold.ttf"),
    "extrabold": ("Poppins-ExtraBold.ttf", "https://github.com/google/fonts/raw/main/ofl/poppins/Poppins-ExtraBold.ttf"),
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
        for attempt in range(3):
            try:
                r = requests.get(url, timeout=20)
                r.raise_for_status()
                with open(path, "wb") as f:
                    f.write(r.content)
                print(f"   ✅ Downloaded: {filename}")
                downloaded[key] = path
                break
            except Exception as e:
                print(f"   ⚠️ Attempt {attempt+1}/3 failed for {filename}: {e}")
                time.sleep(2)
        else:
            print(f"   ❌ Font download fail: {filename}")
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
        raise FileNotFoundError(f"❌ Upload ke liye file nahi mili: {local_path}")
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

VIDEO_W        = 1920
VIDEO_H        = 1080
FPS            = 60
BG_COLOR       = (255, 255, 255)
ANIM_SECS      = 0.35
HIGH_RES_CACHE = 1200

WORK_DIR   = "/tmp/motion_work"
FRAMES_DIR = os.path.join(WORK_DIR, "frames")
AUDIO_DIR  = os.path.join(WORK_DIR, "audio")

os.makedirs(FRAMES_DIR, exist_ok=True)
os.makedirs(AUDIO_DIR,  exist_ok=True)

# Optional AI upscaling — set UPSCALE_API_KEY env var to enable DeepAI SR
UPSCALE_API_KEY = os.environ.get("UPSCALE_API_KEY")
if UPSCALE_API_KEY:
    print("   🤖 AI upscaling enabled (DeepAI)")
else:
    print("   🖼️ Multi-pass PIL upscaling (3 passes)")

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
# 7. IMAGE UPSCALING — MULTI-PASS (Option B + C Combined)
#
# Pass 1 (always): PIL LANCZOS 2x + strong UnsharpMask
# Pass 2 (always): AI API (if key set) OR PIL 1.5x + UnsharpMask
# Pass 3 (always): Final LANCZOS downscale to target + light sharpen
#
# Ye 3 passes har image pe laagu hote hain — custom PNG ya emoji sab pe.
# ═══════════════════════════════════════════════════════════════

def ai_upscale_api(img):
    """DeepAI Super Resolution API — sirf tab call hota hai jab UPSCALE_API_KEY set ho."""
    buf = io.BytesIO()
    img.convert("RGBA").save(buf, "PNG")
    buf.seek(0)
    response = requests.post(
        "https://api.deepai.org/api/torch-srgan",
        files={"image": ("image.png", buf, "image/png")},
        headers={"api-key": UPSCALE_API_KEY},
        timeout=60
    )
    response.raise_for_status()
    output_url = response.json().get("output_url")
    if not output_url:
        raise Exception("API se output_url nahi mila")
    img_resp = requests.get(output_url, timeout=30)
    img_resp.raise_for_status()
    return Image.open(io.BytesIO(img_resp.content)).convert("RGBA")


def multi_pass_upscale(img, target_w, target_h):
    """
    3-pass upscaling pipeline:
    Pass 1 → PIL 2x + strong UnsharpMask
    Pass 2 → AI API ya PIL 1.5x + UnsharpMask
    Pass 3 → Exact target resize (downscale) + final sharpen
    """
    current  = img.copy().convert("RGBA")
    orig_w, orig_h = current.size

    # ── Pass 1: PIL 2x ───────────────────────────────────────
    p1_size = max(orig_w * 2, target_w * 2, HIGH_RES_CACHE)
    ratio   = p1_size / max(orig_w, orig_h, 1)
    p1_w    = max(1, int(orig_w * ratio))
    p1_h    = max(1, int(orig_h * ratio))
    current = current.resize((p1_w, p1_h), Image.LANCZOS)
    current = current.filter(ImageFilter.UnsharpMask(radius=2.5, percent=200, threshold=3))

    # ── Pass 2: AI API ya PIL 1.5x ───────────────────────────
    if UPSCALE_API_KEY:
        try:
            current = ai_upscale_api(current)
            current = current.filter(ImageFilter.UnsharpMask(radius=1.5, percent=150, threshold=2))
        except Exception as e:
            print(f"      ⚠️ AI API fail ({e}) — PIL fallback")
            p2_w = max(1, int(current.width * 1.5))
            p2_h = max(1, int(current.height * 1.5))
            current = current.resize((p2_w, p2_h), Image.LANCZOS)
            current = current.filter(ImageFilter.UnsharpMask(radius=1.5, percent=150, threshold=2))
    else:
        p2_w = max(1, min(int(current.width * 1.5), target_w * 4))
        p2_h = max(1, min(int(current.height * 1.5), target_h * 4))
        current = current.resize((p2_w, p2_h), Image.LANCZOS)
        current = current.filter(ImageFilter.UnsharpMask(radius=1.5, percent=150, threshold=2))

    # ── Pass 3: Final exact resize to target (downscale = sharp) ─
    current = current.resize((max(1, target_w), max(1, target_h)), Image.LANCZOS)
    current = current.filter(ImageFilter.UnsharpMask(radius=1.0, percent=120, threshold=2))

    return current

# ═══════════════════════════════════════════════════════════════
# 8. ASSET FETCH
# ═══════════════════════════════════════════════════════════════

def fetch_asset(raw_url, target_w, target_h):
    target_w = max(1, target_w)
    target_h = max(1, target_h)

    try:
        r = requests.get(raw_url, timeout=20)
        r.raise_for_status()
    except requests.exceptions.Timeout:
        raise Exception(f"Timeout: {raw_url}")
    except requests.exceptions.HTTPError:
        raise Exception(f"HTTP error: {raw_url}")
    except Exception as e:
        raise Exception(f"Network error: {e}")

    try:
        if raw_url.lower().endswith(".svg"):
            if cairosvg is None:
                raise Exception("cairosvg nahi hai")
            # SVG: vector render at 4x — zero quality loss
            svg_w = max(target_w * 4, HIGH_RES_CACHE * 2)
            svg_h = max(target_h * 4, HIGH_RES_CACHE * 2)
            png_bytes = cairosvg.svg2png(
                bytestring=r.content,
                output_width=svg_w,
                output_height=svg_h
            )
            img = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
            return img  # SVG already at high res, no upscaling needed

        else:
            img = Image.open(io.BytesIO(r.content)).convert("RGBA")
            # PNG/JPG: multi-pass upscale at 2x target
            # (draw_frame mein downscale hoga — guaranteed sharp)
            return multi_pass_upscale(img, target_w * 2, target_h * 2)

    except Exception as e:
        raise Exception(f"Image parse/upscale fail: {e}")


def resize_for_render(cached_img, target_w, target_h):
    """High-res cache se per-frame resize — hamesha downscale (sharp)."""
    target_w = max(1, target_w)
    target_h = max(1, target_h)
    if cached_img.width == target_w and cached_img.height == target_h:
        return cached_img.copy()
    resized = cached_img.resize((target_w, target_h), Image.LANCZOS)
    resized = resized.filter(ImageFilter.UnsharpMask(radius=1.0, percent=120, threshold=2))
    return resized

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
            w = len(test) * 20
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
# 10. SAFE SUBTITLE Y CALCULATOR
#
# FIX: Subtitle visual ke upar aa jaata tha — ye function
# guaranteed bahar rakhta hai chahe visuals kahi bhi ho.
#
# Priority:
#   1. Visuals ke NEECHE (preferred)
#   2. Visuals ke UPAR (agar neeche jagah nahi)
#   3. Force below with clamp (last resort)
# ═══════════════════════════════════════════════════════════════

def get_safe_subtitle_y(all_active_visuals, total_text_h):
    MARGIN = 30

    if not all_active_visuals:
        return (VIDEO_H - total_text_h) // 2

    max_bottom = max(v.get("y", 0) + v.get("height", 0) for v in all_active_visuals)
    min_top    = min(v.get("y", 0) for v in all_active_visuals)

    # Option 1: Neeche
    y_below     = max_bottom + MARGIN
    space_below = VIDEO_H - y_below - total_text_h - 10
    if space_below >= 0:
        return y_below

    # Option 2: Upar
    y_above = min_top - MARGIN - total_text_h
    if y_above >= 10:
        return y_above

    # Option 3: Force, clamp to frame
    return max(10, min(y_below, VIDEO_H - total_text_h - 10))

# ═══════════════════════════════════════════════════════════════
# 11. SUBTITLE RENDERER (Redesigned)
#
# FIX: Sirf safe zone mein render hoti hai — visuals se guarantee
# bahar. position field sirf X alignment ke liye use hota hai.
# ═══════════════════════════════════════════════════════════════

def draw_subtitle(overlay, draw, text, position, font_size_key,
                  all_active_visuals=None, center_screen=False):
    if not text or not text.strip():
        return

    font    = fonts.get(font_size_key, fonts["medium"])
    max_w   = int(VIDEO_W * 0.80)
    lines   = wrap_text(text, font, max_w)
    line_h  = FONT_SIZES.get(font_size_key, 52) + 14
    total_h = len(lines) * line_h
    dummy   = ImageDraw.Draw(Image.new("RGBA", (1, 1)))

    # ── Y Position ───────────────────────────────────────────
    if center_screen or not all_active_visuals:
        text_y = (VIDEO_H - total_h) // 2
    else:
        text_y = get_safe_subtitle_y(all_active_visuals, total_h)

    # Hard clamp — kabhi bhi frame boundary se bahar nahi
    text_y = max(10, min(text_y, VIDEO_H - total_h - 10))

    # ── Render Lines ─────────────────────────────────────────
    for i, line in enumerate(lines):
        try:
            lw = dummy.textbbox((0, 0), line, font=font)[2]
        except Exception:
            lw = len(line) * 20

        y = text_y + i * line_h

        if position == "left":
            x = 60
        elif position == "right":
            x = max(60, VIDEO_W - lw - 60)
        else:
            x = max(0, (VIDEO_W - lw) // 2)

        # Shadow
        for dx, dy in [(2, 2), (-1, -1), (2, -1), (-1, 2)]:
            try:
                draw.text((x + dx, y + dy), line, font=font, fill=(200, 200, 200, 180))
            except Exception:
                pass

        # Main text
        try:
            draw.text((x, y), line, font=font, fill=(10, 10, 10, 255))
        except Exception as e:
            print(f"   ⚠️ Text draw fail: {e}")

# ═══════════════════════════════════════════════════════════════
# 12. DOWNLOAD JSON DATA
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
# 13. DOWNLOAD AUDIO
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
# 14. PRE-CACHE ASSETS AT HIGH RESOLUTION
# ═══════════════════════════════════════════════════════════════

print("\n▶ GitHub se assets fetch ho rahe hain (multi-pass upscaling)...")

url_max_size = {}
for vis in visuals:
    key = vis.get("raw_url", "")
    if not key:
        continue
    w = vis.get("width", 400)
    h = vis.get("height", 400)
    prev_w, prev_h = url_max_size.get(key, (0, 0))
    url_max_size[key] = (max(prev_w, w), max(prev_h, h))

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

    fname        = vis.get("asset_path", key).split("/")[-1]
    max_w, max_h = url_max_size.get(key, (vis.get("width", 400), vis.get("height", 400)))

    for attempt in range(3):
        try:
            print(f"   ⬇ {fname}")
            asset_cache[key] = fetch_asset(key, max_w, max_h)
            print(f"      ✅ cached at {asset_cache[key].size[0]}x{asset_cache[key].size[1]}")
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
print(f"   ✅ {ok_count} assets ready | ❌ {fail_count} failed")

# ═══════════════════════════════════════════════════════════════
# 15. PRE-COMPUTE SUBTITLE–VISUAL PAIRING
#
# FIX (Duplication): Subtitle ka start time visual ke start time
# se pehle hota hai, isliye dono baar render hoti thi.
#
# Solution: Pehle check karo — kya poori video mein koi VALID
# (non-failed) visual is subtitle ke saath overlap karta hai?
#
#   _has_visual = True  → sirf jab visual screen pe ho tab render
#   _has_visual = False → narrator line, center screen pe render
#
# failed_urls ke baad run hota hai taaki failed assets ko
# visual-paired nahi count kiya jaaye.
# ═══════════════════════════════════════════════════════════════

print("\n▶ Subtitle–visual pairing pre-compute ho rahi hai...")
for sub in subtitles:
    s_start = sub.get("start", 0)
    s_end   = sub.get("end",   0)

    paired = any(
        vis.get("raw_url", "") and
        vis.get("raw_url", "") not in failed_urls and
        vis.get("start", 0) < s_end and
        vis.get("end",   0) > s_start
        for vis in visuals
    )
    sub["_has_visual"] = paired

    status = "🖼️  visual-paired" if paired else "📝 text-only    "
    print(f"   {status}: '{sub.get('text', '')[:55]}'")

print("   ✅ Pairing done")

# ═══════════════════════════════════════════════════════════════
# 16. DRAW FRAME
#
# FIX 1 (Duplication):
#   - has_visual=True → `if not active_vis: continue`
#     Visual screen pe nahi? Subtitle skip — wait karo.
#   - has_visual=False → center_screen (narrator text)
#
# FIX 2 (Overlap):
#   - draw_subtitle() ab get_safe_subtitle_y() use karta hai
#     jo guarantee karta hai text visuals ke bahar ho.
#
# FIX 3 (Fallback removed):
#   - Purana "nearest subtitle" fallback hata diya.
#   - Blank frames allowed hain — cleaner aur professional.
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

    # ── RENDER VISUALS ────────────────────────────────────────
    for vis in active_vis:
        key        = vis.get("raw_url", "")
        cached_img = asset_cache.get(key)
        if cached_img is None:
            continue

        t_local  = t - vis.get("start", 0)
        duration = vis.get("end", 0) - vis.get("start", 0)
        alpha, scale = get_anim_state(t_local, max(duration, 0.01))

        if alpha <= 0.01:
            continue

        vw = max(1, vis.get("width",  400))
        vh = max(1, vis.get("height", 400))
        w  = max(1, int(vw * scale))
        h  = max(1, int(vh * scale))

        try:
            img_s = resize_for_render(cached_img, w, h)
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
        except Exception as e:
            print(f"   ⚠️ Paste fail: {e}")

    # ── RENDER SUBTITLES ──────────────────────────────────────
    for sub in active_subs:
        text          = sub.get("text", "").strip()
        position      = sub.get("position", "center")
        font_size_key = sub.get("font_size", "medium")
        has_visual    = sub.get("_has_visual", False)

        if not text:
            continue

        if has_visual:
            # Visual-paired subtitle:
            # Visual abhi screen pe nahi aaya? Skip — dobara mat dikhao
            if not active_vis:
                continue  # ← DUPLICATION FIX

            # Visual hai — safe zone mein render (no overlap guaranteed)
            draw_subtitle(
                overlay, draw,
                text, position, font_size_key,
                all_active_visuals=active_vis,
                center_screen=False
            )
        else:
            # Narrator/text-only — center screen
            draw_subtitle(
                overlay, draw,
                text, position, font_size_key,
                all_active_visuals=None,
                center_screen=True
            )

    return Image.alpha_composite(frame, overlay).convert("RGB")

# ═══════════════════════════════════════════════════════════════
# 17. RENDER ALL FRAMES
# ═══════════════════════════════════════════════════════════════

print("\n▶ Frames render ho rahe hain...")

if os.path.exists(FRAMES_DIR):
    shutil.rmtree(FRAMES_DIR)
os.makedirs(FRAMES_DIR, exist_ok=True)

log_every = max(1, FPS * 5)

for i in range(total_frames):
    t          = i / FPS
    frame_img  = draw_frame(t)
    frame_path = os.path.join(FRAMES_DIR, f"frame_{i:06d}.png")
    frame_img.save(frame_path, "PNG")

    if i % log_every == 0 or i == total_frames - 1:
        pct = int((i / max(total_frames - 1, 1)) * 100)
        print(f"   ⏱ {pct}% — frame {i}/{total_frames} (t={t:.1f}s)")

print("   ✅ Saare frames ready!")

# ═══════════════════════════════════════════════════════════════
# 18. ASSEMBLE VIDEO WITH FFMPEG
# ═══════════════════════════════════════════════════════════════

print("\n▶ Video assemble ho rahi hai FFmpeg se...")

output_video = os.path.join(WORK_DIR, "final_output.mp4")

ffmpeg_cmd = [
    "ffmpeg", "-y",
    "-framerate", str(FPS),
    "-i", os.path.join(FRAMES_DIR, "frame_%06d.png"),
    "-i", ap,
    "-c:v", "libx264",
    "-preset", "fast",
    "-crf", "18",
    "-c:a", "aac",
    "-b:a", "192k",
    "-shortest",
    "-pix_fmt", "yuv420p",
    output_video
]

result = subprocess.run(ffmpeg_cmd, capture_output=True, text=True)
if result.returncode != 0:
    print(f"❌ FFmpeg error:\n{result.stderr}")
    sys.exit(1)

print("   ✅ Video ready!")

# Frames cleanup — disk space bachao
try:
    shutil.rmtree(FRAMES_DIR)
    print("   🗑️ Frames cleaned up")
except Exception:
    pass

# ═══════════════════════════════════════════════════════════════
# 19. UPLOAD TO GOOGLE DRIVE
# ═══════════════════════════════════════════════════════════════

print("\n▶ Drive pe upload ho rahi hai...")

try:
    vid_folder_id = get_folder_id("Video")
except FileNotFoundError:
    print("   📁 'Video' folder nahi mila — bana raha hu...")
    vid_folder_id = service.files().create(
        body={
            "name": "Video",
            "parents": [MAIN_FOLDER_ID],
            "mimeType": "application/vnd.google-apps.folder"
        },
        fields="id"
    ).execute()["id"]
    print(f"   ✅ Video folder created: {vid_folder_id}")

upload_file(output_video, "final_video.mp4", vid_folder_id, service)

print("\n🎉 Pipeline complete! Video Drive pe upload ho gayi.")
