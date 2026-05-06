#!/usr/bin/env python3
"""
YouTube Shorts → Instagram Reels Auto Uploader
- Scarica video da Google Drive
- Recupera metadati da YouTube con yt-dlp
- Carica su Instagram Reels
- Notifica Gmail quando rimangono pochi video
- Parte dal numero 857
"""

import os
import json
import time
import subprocess
import smtplib
import tempfile
import requests
from pathlib import Path
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from google.oauth2 import service_account
import io

# ─────────────────────────────────────────────────────────────
# CONFIGURAZIONE
# ─────────────────────────────────────────────────────────────

START_NUMBER        = 857
YOUTUBE_CHANNEL_URL = os.environ["YOUTUBE_CHANNEL_URL"]
GDRIVE_FOLDER_NAME  = "Instagram Shorts Queue"
GDRIVE_CREDENTIALS  = json.loads(os.environ["GDRIVE_SERVICE_ACCOUNT_JSON"])
IG_USER_ID          = os.environ["IG_USER_ID"]
IG_ACCESS_TOKEN     = os.environ["IG_ACCESS_TOKEN"]
GMAIL_SENDER        = "giuppyc@gmail.com"
GMAIL_RECEIVER      = "giuppyc@gmail.com"
GMAIL_APP_PASSWORD  = os.environ["GMAIL_APP_PASSWORD"]
NOTIFY_THRESHOLD    = 3
STATE_FILE          = "upload_state.json"

# ─────────────────────────────────────────────────────────────
# STATO
# ─────────────────────────────────────────────────────────────

def load_state() -> dict:
    if Path(STATE_FILE).exists():
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"last_uploaded_number": START_NUMBER - 1, "uploaded": []}

def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)

# ─────────────────────────────────────────────────────────────
# GOOGLE DRIVE
# ─────────────────────────────────────────────────────────────

def get_drive_service():
    creds = service_account.Credentials.from_service_account_info(
        GDRIVE_CREDENTIALS,
        scopes=["https://www.googleapis.com/auth/drive.readonly"]
    )
    return build("drive", "v3", credentials=creds)

def list_drive_videos(service) -> list[dict]:
    folder_result = service.files().list(
        q=f"name='{GDRIVE_FOLDER_NAME}' and mimeType='application/vnd.google-apps.folder' and trashed=false",
        fields="files(id, name)"
    ).execute()
    folders = folder_result.get("files", [])
    if not folders:
        raise Exception(f"Cartella '{GDRIVE_FOLDER_NAME}' non trovata su Google Drive!")
    folder_id = folders[0]["id"]
    files_result = service.files().list(
        q=f"'{folder_id}' in parents and trashed=false and mimeType contains 'video/'",
        fields="files(id, name, size)",
        orderBy="name"
    ).execute()
    return files_result.get("files", [])

def download_video_from_drive(service, file_id: str, file_name: str, dest_dir: str) -> str:
    dest_path = str(Path(dest_dir) / file_name)
    print(f"⬇️  Download da Drive: {file_name}...")
    request = service.files().get_media(fileId=file_id)
    fh = io.FileIO(dest_path, "wb")
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while not done:
        status, done = downloader.next_chunk()
        if status:
            print(f"   {int(status.progress() * 100)}%", end="\r")
    print(f"   Download completato ✓")
    return dest_path

def extract_number_from_filename(filename: str) -> int | None:
    stem = Path(filename).stem
    parts = stem.split()
    for part in parts:
        if part.isdigit():
            return int(part)
    return None

# ─────────────────────────────────────────────────────────────
# YOUTUBE
# ─────────────────────────────────────────────────────────────

def get_youtube_metadata(channel_url: str, short_number: int) -> dict:
    print(f"📡 Cerco metadati YouTube per short #{short_number}...")
    cmd = [
        "yt-dlp", "--flat-playlist", "--dump-json",
        "--playlist-end", "200", f"{channel_url}/shorts"
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    for line in result.stdout.strip().split("\n"):
        if not line:
            continue
        try:
            entry = json.loads(line)
            title = entry.get("title", "")
            if f"#{short_number}" in title:
                hashtags = [t for t in title.split() if t.startswith("#")]
                game = hashtags[2].lstrip("#") if len(hashtags) >= 3 else "gaming"
                return {"title": title, "hashtags": hashtags, "game": game}
        except json.JSONDecodeError:
            continue
    print(f"   ⚠️  Metadati non trovati, uso hashtag di default")
    return {
        "title": f"#shorts #{short_number} #gaming #shortsclip #gameplayita #filfoxezorcraft",
        "hashtags": [f"#shorts", f"#{short_number}", "#gaming", "#shortsclip", "#gameplayita", "#filfoxezorcraft"],
        "game": "gaming"
    }

def build_caption(metadata: dict, short_number: int) -> str:
    hashtags = metadata["hashtags"]
    game = metadata["game"].capitalize()
    caption = f"🎮 {game} - Short #{short_number}\n\n"
    caption += " ".join(hashtags)
    return caption[:2200]

# ─────────────────────────────────────────────────────────────
# INSTAGRAM UPLOAD
# ─────────────────────────────────────────────────────────────

def upload_to_instagram(video_path: str, caption: str) -> bool:
    base_url = f"https://graph.facebook.com/v19.0/{IG_USER_ID}"
    file_size = Path(video_path).stat().st_size

    print("📸 Instagram: creazione container...")

    # Step 1: crea container resumable
    init_resp = requests.post(
        f"{base_url}/media",
        params={
            "media_type":    "REELS",
            "caption":       caption,
            "share_to_feed": "true",
            "upload_type":   "resumable",
            "access_token":  IG_ACCESS_TOKEN,
        }
    )
    init_data = init_resp.json()
    print(f"   Container response: {init_data}")

    if "error" in init_data:
        print(f"   ❌ Errore container: {init_data['error']['message']}")
        return False

    container_id = init_data.get("id")
    upload_url   = init_data.get("uri")

    if not upload_url:
        print("   ❌ Nessun upload URL ricevuto")
        return False

    print(f"   Container ID: {container_id}")
    print(f"   Upload URL: {upload_url[:60]}...")

    # Step 2: carica il file con PUT
    print("   Upload file...")
    with open(video_path, "rb") as vf:
        video_data = vf.read()

    upload_resp = requests.post(
        upload_url,
        headers={
            "Authorization": f"OAuth {IG_ACCESS_TOKEN}",
            "offset":        "0",
            "file_size":     str(file_size),
            "Content-Type":  "application/octet-stream",
        },
        data=video_data
    )
    print(f"   Upload status: {upload_resp.status_code}")
    print(f"   Upload response: {upload_resp.text[:300]}")

    if upload_resp.status_code not in (200, 201):
        print(f"   ❌ Errore upload")
        return False

    # Step 3: polling
    print("   Attendo elaborazione", end="", flush=True)
    for _ in range(20):
        time.sleep(15)
        print(".", end="", flush=True)
        status_resp = requests.get(
            f"https://graph.facebook.com/v19.0/{container_id}",
            params={"fields": "status_code,status", "access_token": IG_ACCESS_TOKEN}
        )
        status_data = status_resp.json()
        status = status_data.get("status_code", "")
        if status == "FINISHED":
            break
        if status == "ERROR":
            print(f"\n   ❌ Errore: {status_data}")
            return False
    print()

    # Step 4: pubblica
    pub_resp = requests.post(
        f"{base_url}/media_publish",
        params={"creation_id": container_id, "access_token": IG_ACCESS_TOKEN}
    )
    pub_data = pub_resp.json()
    print(f"   Publish response: {pub_data}")

    if "error" in pub_data:
        print(f"   ❌ Errore pubblicazione: {pub_data['error']['message']}")
        return False

    print(f"   ✅ Pubblicato su Instagram! ID: {pub_data.get('id')}")
    return True

# ─────────────────────────────────────────────────────────────
# EMAIL
# ─────────────────────────────────────────────────────────────

def send_notification_email(remaining_videos: list[str]):
    count = len(remaining_videos)
    names = "\n".join(f"  • {v}" for v in remaining_videos)
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"⚠️ YouTube→Social: rimangono solo {count} video!"
    msg["From"]    = GMAIL_SENDER
    msg["To"]      = GMAIL_RECEIVER
    body = f"""Ciao!\n\nRimangono solo {count} video nella cartella Drive:\n\n{names}\n\nRicordati di aggiungerne di nuovi!\n\n— Bot YouTube→Instagram"""
    msg.attach(MIMEText(body, "plain"))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_SENDER, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_SENDER, GMAIL_RECEIVER, msg.as_string())
        print(f"📧 Email inviata a {GMAIL_RECEIVER}")
    except Exception as e:
        print(f"⚠️  Errore email: {e}")

# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────

def main():
    print(f"\n{'='*55}")
    print(f"  📲 YouTube Shorts → Instagram Uploader")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*55}\n")

    state = load_state()
    next_number = state["last_uploaded_number"] + 1
    print(f"🔢 Prossimo short: #{next_number}")

    drive_service = get_drive_service()
    all_videos = list_drive_videos(drive_service)

    if not all_videos:
        print("❌ Nessun video trovato su Drive!")
        return

    all_videos.sort(key=lambda f: extract_number_from_filename(f["name"]) or 0)
    print(f"📁 Video su Drive: {len(all_videos)}")

    target_file = next(
        (f for f in all_videos if extract_number_from_filename(f["name"]) == next_number),
        None
    )

    if not target_file:
        print(f"⚠️  File 'shorts {next_number}.mp4' non trovato su Drive!")
        return

    metadata = get_youtube_metadata(YOUTUBE_CHANNEL_URL, next_number)
    caption  = build_caption(metadata, next_number)
    print(f"\n📝 Caption: {caption[:100]}...\n")

    with tempfile.TemporaryDirectory() as tmpdir:
        video_path = download_video_from_drive(
            drive_service, target_file["id"], target_file["name"], tmpdir
        )
        ig_ok = upload_to_instagram(video_path, caption)

    if ig_ok:
        state["last_uploaded_number"] = next_number
        state["uploaded"].append({
            "number":      next_number,
            "file":        target_file["name"],
            "instagram":   ig_ok,
            "uploaded_at": datetime.now().isoformat(),
        })
        save_state(state)
        print(f"\n✅ Short #{next_number} caricato su Instagram!")
    else:
        print(f"\n❌ Upload fallito. Riprova domani.")
        return

    remaining = [
        f["name"] for f in all_videos
        if (extract_number_from_filename(f["name"]) or 0) > next_number
    ]
    print(f"\n📊 Video rimanenti: {len(remaining)}")
    if len(remaining) <= NOTIFY_THRESHOLD:
        send_notification_email(remaining)

if __name__ == "__main__":
    main()
