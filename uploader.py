#!/usr/bin/env python3
"""
YouTube Shorts → Instagram Reels Auto Uploader
- Scarica video da Google Drive
- Recupera metadati da YouTube con yt-dlp
- Carica su Instagram Reels (via tmpfiles.org)
- Rinnova automaticamente il token Instagram
- Notifica Gmail quando rimangono pochi video
- Parte dal numero 857
"""

import os
import json
import time
import subprocess
import smtplib
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
GH_PAT              = os.environ["GH_PAT"]
GH_REPO             = "zorcraft76/shorts-uploader"
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
# TOKEN INSTAGRAM — rinnovo automatico
# ─────────────────────────────────────────────────────────────

def refresh_instagram_token(current_token: str) -> str:
    """Rinnova il token Instagram e aggiorna il Secret su GitHub."""
    print("🔄 Rinnovo token Instagram...")

    resp = requests.get(
        "https://graph.facebook.com/v19.0/oauth/access_token",
        params={
            "grant_type":        "fb_exchange_token",
            "client_id":         "964846999240039",
            "client_secret":     os.environ.get("IG_APP_SECRET", ""),
            "fb_exchange_token": current_token,
        }
    )
    data = resp.json()

    if "access_token" in data:
        new_token = data["access_token"]
        print(f"   Nuovo token ottenuto ✓")
        update_github_secret("IG_ACCESS_TOKEN", new_token)
        return new_token
    else:
        print(f"   ⚠️ Rinnovo fallito: {data} — uso token attuale")
        return current_token


def update_github_secret(secret_name: str, secret_value: str):
    """Aggiorna un Secret su GitHub tramite API."""
    headers = {
        "Authorization": f"Bearer {GH_PAT}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    # Ottieni la public key del repository
    key_resp = requests.get(
        f"https://api.github.com/repos/{GH_REPO}/actions/secrets/public-key",
        headers=headers
    )
    key_data = key_resp.json()
    public_key = key_data["key"]
    key_id     = key_data["key_id"]

    # Cifra il secret con la public key
    from base64 import b64encode
    from nacl import encoding, public

    pk = public.PublicKey(public_key.encode("utf-8"), encoding.Base64Encoder())
    sealed_box = public.SealedBox(pk)
    encrypted = sealed_box.encrypt(secret_value.encode("utf-8"))
    encrypted_b64 = b64encode(encrypted).decode("utf-8")

    # Aggiorna il secret
    update_resp = requests.put(
        f"https://api.github.com/repos/{GH_REPO}/actions/secrets/{secret_name}",
        headers=headers,
        json={
            "encrypted_value": encrypted_b64,
            "key_id":          key_id,
        }
    )

    if update_resp.status_code in (201, 204):
        print(f"   Secret {secret_name} aggiornato su GitHub ✓")
    else:
        print(f"   ⚠️ Errore aggiornamento secret: {update_resp.text}")

# ─────────────────────────────────────────────────────────────
# GOOGLE DRIVE
# ─────────────────────────────────────────────────────────────

def get_drive_service():
    creds = service_account.Credentials.from_service_account_info(
        GDRIVE_CREDENTIALS,
        scopes=["https://www.googleapis.com/auth/drive"]
    )
    return build("drive", "v3", credentials=creds)

def list_drive_videos(service) -> list[dict]:
    folder_result = service.files().list(
        q=f"name='{GDRIVE_FOLDER_NAME}' and mimeType='application/vnd.google-apps.folder' and trashed=false",
        fields="files(id, name)"
    ).execute()
    folders = folder_result.get("files", [])
    if not folders:
        raise Exception(f"Cartella '{GDRIVE_FOLDER_NAME}' non trovata!")
    folder_id = folders[0]["id"]
    files_result = service.files().list(
        q=f"'{folder_id}' in parents and trashed=false and mimeType contains 'video/'",
        fields="files(id, name, size)",
        orderBy="name"
    ).execute()
    return files_result.get("files", [])

def download_video_from_drive(service, file_id: str) -> bytes:
    """Scarica il video da Google Drive e restituisce i bytes."""
    print("   📥 Download video da Drive...")
    
    request = service.files().get_media(fileId=file_id)
    file_data = io.BytesIO()
    downloader = MediaIoBaseDownload(file_data, request)
    
    done = False
    while not done:
        status, done = downloader.next_chunk()
        print(f"   Download: {int(status.progress() * 100)}%", end="\r")
    
    print(f"\n   ✅ Download completato ({file_data.tell() / 1024 / 1024:.1f} MB)")
    file_data.seek(0)
    return file_data.read()

def extract_number_from_filename(filename: str) -> int | None:
    stem = Path(filename).stem
    parts = stem.split()
    for part in parts:
        if part.isdigit():
            return int(part)
    return None

# ─────────────────────────────────────────────────────────────
# UPLOAD SU TMPFILES.ORG (per ottenere URL pubblico)
# ─────────────────────────────────────────────────────────────

def upload_to_tmpfiles(video_bytes: bytes, filename: str) -> str:
    """Carica il video su tmpfiles.org e restituisce URL pubblico."""
    print("   📤 Upload su tmpfiles.org...")
    
    resp = requests.post(
        "https://tmpfiles.org/api/v1/upload",
        files={"file": (filename, video_bytes, "video/mp4")}
    )
    
    if resp.status_code != 200:
        raise Exception(f"tmpfiles.org errore: {resp.text}")
    
    # L'URL diretto per il download è quello con /dl/
    file_url = resp.json()["data"]["url"]
    direct_url = file_url.replace("/api/v1/", "/dl/")
    print(f"   ✅ URL pubblico ottenuto: {direct_url[:80]}...")
    return direct_url

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
    print("   ⚠️  Metadati non trovati, uso hashtag di default")
    return {
        "title": f"#shorts #{short_number} #gaming #shortsclip #gameplayita #filfoxezorcraft",
        "hashtags": [f"#shorts", f"#{short_number}", "#gaming", "#shortsclip", "#gameplayita", "#filfoxezorcraft"],
        "game": "gaming"
    }

def build_caption(metadata: dict, short_number: int) -> str:
    hashtags = metadata["hashtags"]
    game = metadata["game"].capitalize()
    caption = f"🎮 {game} - Short #{short_number}\n\n" + " ".join(hashtags)
    return caption[:2200]

# ─────────────────────────────────────────────────────────────
# INSTAGRAM UPLOAD - Versione con URL pubblico
# ─────────────────────────────────────────────────────────────

def upload_to_instagram(video_url: str, caption: str, token: str) -> bool:
    """
    Carica video su Instagram usando un URL pubblico.
    """
    base_url = f"https://graph.facebook.com/v19.0/{IG_USER_ID}"

    print("📸 Instagram: creazione container con URL...")
    print(f"   URL video: {video_url[:80]}...")

    init_resp = requests.post(
        f"{base_url}/media",
        data={
            "media_type":    "REELS",
            "video_url":     video_url,
            "caption":       caption,
            "share_to_feed": "true",
            "access_token":  token,
        }
    )
    init_data = init_resp.json()
    print(f"   Container response: {init_data}")

    if "error" in init_data:
        print(f"   ❌ Errore container: {init_data['error']['message']}")
        return False

    container_id = init_data.get("id")
    print(f"   Container ID: {container_id}")

    print("   Attendo elaborazione", end="", flush=True)
    for _ in range(45):
        time.sleep(15)
        print(".", end="", flush=True)
        status_resp = requests.get(
            f"https://graph.facebook.com/v19.0/{container_id}",
            params={"fields": "status_code,status", "access_token": token}
        )
        status_data = status_resp.json()
        status = status_data.get("status_code", "")
        if status == "FINISHED":
            print(f" ✓")
            break
        if status == "ERROR":
            print(f"\n   ❌ Errore elaborazione: {status_data}")
            return False
    else:
        print(f"\n   ❌ Timeout dopo 11 minuti")
        return False

    pub_resp = requests.post(
        f"{base_url}/media_publish",
        data={"creation_id": container_id, "access_token": token}
    )
    pub_data = pub_resp.json()
    print(f"   Publish response: {pub_data}")

    if "error" in pub_data:
        print(f"   ❌ Errore pubblicazione: {pub_data['error']['message']}")
        return False

    print(f"   ✅ Pubblicato! ID: {pub_data.get('id')}")
    return True

# ─────────────────────────────────────────────────────────────
# EMAIL
# ─────────────────────────────────────────────────────────────

def send_notification_email(remaining_videos: list[str]):
    count = len(remaining_videos)
    names = "\n".join(f"  • {v}" for v in remaining_videos)
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"⚠️ Shorts Bot: rimangono solo {count} video!"
    msg["From"]    = GMAIL_SENDER
    msg["To"]      = GMAIL_RECEIVER
    body = f"Ciao!\n\nRimangono solo {count} video:\n\n{names}\n\nAggiungi nuovi short!\n\n— Bot"
    msg.attach(MIMEText(body, "plain"))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_SENDER, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_SENDER, GMAIL_RECEIVER, msg.as_string())
        print(f"📧 Email inviata!")
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

    # Rinnova token Instagram
    token = refresh_instagram_token(IG_ACCESS_TOKEN)

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

    # Scarica il video da Drive
    video_data = download_video_from_drive(drive_service, target_file["id"])

    # Carica su tmpfiles.org per ottenere URL pubblico
    video_url = upload_to_tmpfiles(video_data, target_file["name"])

    # Upload su Instagram usando l'URL
    ig_ok = upload_to_instagram(video_url, caption, token)

    if ig_ok:
        state["last_uploaded_number"] = next_number
        state["uploaded"].append({
            "number":      next_number,
            "file":        target_file["name"],
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
