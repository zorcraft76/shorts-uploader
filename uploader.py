#!/usr/bin/env python3
"""
YouTube Shorts → Instagram Reels + TikTok Auto Uploader
- Scarica video da Google Drive
- Recupera metadati (titolo/hashtag) da YouTube con yt-dlp
- Carica su Instagram e TikTok in parallelo
- Notifica Gmail quando rimangono ≤ 3 video
- Parte dal numero 857, incrementa ogni giorno
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

# Numero da cui partire
START_NUMBER = 857

# Nome canale YouTube (per cercare i video con yt-dlp)
YOUTUBE_CHANNEL_URL = os.environ["YOUTUBE_CHANNEL_URL"]  # es. https://www.youtube.com/@filfoxezorcraft

# Google Drive
GDRIVE_FOLDER_NAME  = "Instagram Shorts Queue"
GDRIVE_CREDENTIALS  = json.loads(os.environ["GDRIVE_SERVICE_ACCOUNT_JSON"])

# Instagram
IG_USER_ID      = os.environ["IG_USER_ID"]
IG_ACCESS_TOKEN = os.environ["IG_ACCESS_TOKEN"]

# TikTok
TIKTOK_ACCESS_TOKEN = os.environ["TIKTOK_ACCESS_TOKEN"]

# Gmail notifiche
GMAIL_SENDER   = "giuppyc@gmail.com"
GMAIL_RECEIVER = "giuppyc@gmail.com"
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]  # App Password Gmail

# Soglia notifica video rimanenti
NOTIFY_THRESHOLD = 3

# File di stato
STATE_FILE = "upload_state.json"

# ─────────────────────────────────────────────────────────────
# STATO
# ─────────────────────────────────────────────────────────────

def load_state() -> dict:
    if Path(STATE_FILE).exists():
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "last_uploaded_number": START_NUMBER - 1,
        "uploaded": []
    }


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
    """Elenca tutti i file video nella cartella Drive."""
    # Trova la cartella
    folder_result = service.files().list(
        q=f"name='{GDRIVE_FOLDER_NAME}' and mimeType='application/vnd.google-apps.folder' and trashed=false",
        fields="files(id, name)"
    ).execute()

    folders = folder_result.get("files", [])
    if not folders:
        raise Exception(f"Cartella '{GDRIVE_FOLDER_NAME}' non trovata su Google Drive!")

    folder_id = folders[0]["id"]

    # Elenca i video nella cartella
    files_result = service.files().list(
        q=f"'{folder_id}' in parents and trashed=false and mimeType contains 'video/'",
        fields="files(id, name, size)",
        orderBy="name"
    ).execute()

    return files_result.get("files", [])


def download_video_from_drive(service, file_id: str, file_name: str, dest_dir: str) -> str:
    """Scarica un video da Google Drive in una directory temporanea."""
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
    """Estrae il numero da 'shorts 857.mp4' → 857"""
    stem = Path(filename).stem  # 'shorts 857'
    parts = stem.split()
    for part in parts:
        if part.isdigit():
            return int(part)
    return None


# ─────────────────────────────────────────────────────────────
# YOUTUBE — recupera metadati con yt-dlp (no API key)
# ─────────────────────────────────────────────────────────────

def get_youtube_metadata(channel_url: str, short_number: int) -> dict:
    """
    Cerca negli Shorts del canale il video con #<numero> nel titolo.
    Usa yt-dlp senza API key.
    """
    print(f"📡 Cerco metadati YouTube per short #{short_number}...")

    # Scarica lista Shorts del canale (solo metadati, no video)
    cmd = [
        "yt-dlp",
        "--flat-playlist",
        "--dump-json",
        "--playlist-end", "200",       # ultimi 200 short
        f"{channel_url}/shorts"
    ]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)

    for line in result.stdout.strip().split("\n"):
        if not line:
            continue
        try:
            entry = json.loads(line)
            title = entry.get("title", "")
            # Cerca #857 nel titolo
            if f"#{short_number}" in title:
                # Estrai nome del gioco (3° hashtag)
                hashtags = [t for t in title.split() if t.startswith("#")]
                game = hashtags[2].lstrip("#") if len(hashtags) >= 3 else "gaming"
                return {
                    "title":    title,
                    "hashtags": hashtags,
                    "game":     game,
                    "url":      f"https://www.youtube.com/shorts/{entry.get('id', '')}"
                }
        except json.JSONDecodeError:
            continue

    # Fallback se non trovato
    print(f"   ⚠️  Metadati non trovati, uso hashtag di default")
    return {
        "title":    f"#shorts #{short_number} #gaming #shortsclip #gameplayita #filfoxezorcraft",
        "hashtags": [f"#shorts", f"#{short_number}", "#gaming", "#shortsclip", "#gameplayita", "#filfoxezorcraft"],
        "game":     "gaming",
        "url":      ""
    }


def build_caption(metadata: dict, short_number: int) -> str:
    """Costruisce la caption per Instagram/TikTok dagli hashtag YouTube."""
    hashtags = metadata["hashtags"]
    game = metadata["game"].capitalize()

    caption = f"🎮 {game} - Short #{short_number}\n\n"
    caption += " ".join(hashtags)

    return caption[:2200]  # limite Instagram


# ─────────────────────────────────────────────────────────────
# INSTAGRAM UPLOAD
# ─────────────────────────────────────────────────────────────

def upload_to_instagram(video_path: str, caption: str) -> bool:
    """Carica un Reel su Instagram tramite Graph API con upload resumable."""
    base_url = f"https://graph.facebook.com/v19.0/{IG_USER_ID}"
    file_size = Path(video_path).stat().st_size

    print("📸 Instagram: creazione container...")

    # Step 1: inizializza upload resumable
    init_resp = requests.post(
        f"{base_url}/media",
        data={
            "media_type":    "REELS",
            "caption":       caption,
            "share_to_feed": "true",
            "upload_type":   "resumable",
            "access_token":  IG_ACCESS_TOKEN,
        }
    )
    init_data = init_resp.json()

    if "error" in init_data:
        print(f"   ❌ Errore: {init_data['error']['message']}")
        return False

    container_id = init_data["id"]
    upload_url   = init_data["uri"]

    # Step 2: carica il file
    print("   Upload file...")
    with open(video_path, "rb") as vf:
        upload_resp = requests.post(
            upload_url,
            headers={
                "Authorization": f"OAuth {IG_ACCESS_TOKEN}",
                "offset":        "0",
                "file_size":     str(file_size),
                "Content-Type":  "application/octet-stream",
            },
            data=vf
        )

    if upload_resp.status_code not in (200, 201):
        print(f"   ❌ Errore upload: {upload_resp.text}")
        return False

    # Step 3: polling processing
    print("   Attendo elaborazione", end="")
    for _ in range(20):
        time.sleep(15)
        print(".", end="", flush=True)
        status_resp = requests.get(
            f"https://graph.facebook.com/v19.0/{container_id}",
            params={"fields": "status_code", "access_token": IG_ACCESS_TOKEN}
        )
        status = status_resp.json().get("status_code", "")
        if status == "FINISHED":
            break
        if status == "ERROR":
            print(f"\n   ❌ Errore elaborazione")
            return False
    print()

    # Step 4: pubblica
    pub_resp = requests.post(
        f"{base_url}/media_publish",
        data={"creation_id": container_id, "access_token": IG_ACCESS_TOKEN}
    )
    pub_data = pub_resp.json()

    if "error" in pub_data:
        print(f"   ❌ Errore pubblicazione: {pub_data['error']['message']}")
        return False

    print(f"   ✅ Pubblicato su Instagram! ID: {pub_data.get('id')}")
    return True


# ─────────────────────────────────────────────────────────────
# TIKTOK UPLOAD
# ─────────────────────────────────────────────────────────────

def upload_to_tiktok(video_path: str, caption: str) -> bool:
    """Carica un video su TikTok tramite Content Posting API."""
    file_size = Path(video_path).stat().st_size

    print("🎵 TikTok: inizializzazione upload...")

    # Step 1: inizializza upload
    init_resp = requests.post(
        "https://open.tiktokapis.com/v2/post/publish/video/init/",
        headers={
            "Authorization": f"Bearer {TIKTOK_ACCESS_TOKEN}",
            "Content-Type":  "application/json; charset=UTF-8",
        },
        json={
            "post_info": {
                "title":          caption[:150],  # TikTok: max 150 char
                "privacy_level":  "PUBLIC_TO_EVERYONE",
                "disable_duet":   False,
                "disable_stitch": False,
                "disable_comment": False,
            },
            "source_info": {
                "source":          "FILE_UPLOAD",
                "video_size":      file_size,
                "chunk_size":      file_size,  # upload in un solo chunk
                "total_chunk_count": 1,
            }
        }
    )

    init_data = init_resp.json()

    if init_data.get("error", {}).get("code") != "ok":
        print(f"   ❌ Errore init: {init_data}")
        return False

    upload_url   = init_data["data"]["upload_url"]
    publish_id   = init_data["data"]["publish_id"]

    # Step 2: carica il file
    print("   Upload file...")
    with open(video_path, "rb") as vf:
        upload_resp = requests.put(
            upload_url,
            headers={
                "Content-Type":        "video/mp4",
                "Content-Range":       f"bytes 0-{file_size-1}/{file_size}",
                "Content-Length":      str(file_size),
            },
            data=vf
        )

    if upload_resp.status_code not in (200, 201, 206):
        print(f"   ❌ Errore upload: {upload_resp.text}")
        return False

    # Step 3: polling status
    print("   Attendo elaborazione", end="")
    for _ in range(20):
        time.sleep(10)
        print(".", end="", flush=True)
        status_resp = requests.post(
            "https://open.tiktokapis.com/v2/post/publish/status/fetch/",
            headers={
                "Authorization": f"Bearer {TIKTOK_ACCESS_TOKEN}",
                "Content-Type":  "application/json; charset=UTF-8",
            },
            json={"publish_id": publish_id}
        )
        status_data = status_resp.json()
        status = status_data.get("data", {}).get("status", "")

        if status == "PUBLISH_COMPLETE":
            print()
            print(f"   ✅ Pubblicato su TikTok!")
            return True
        if status in ("FAILED", "ERROR"):
            print(f"\n   ❌ Errore TikTok: {status_data}")
            return False

    print(f"\n   ⚠️  Timeout TikTok — verifica manualmente")
    return False


# ─────────────────────────────────────────────────────────────
# NOTIFICA EMAIL
# ─────────────────────────────────────────────────────────────

def send_notification_email(remaining_videos: list[str]):
    """Manda email quando rimangono pochi video."""
    count = len(remaining_videos)
    names = "\n".join(f"  • {v}" for v in remaining_videos)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"⚠️ YouTube→Social: rimangono solo {count} video!"
    msg["From"]    = GMAIL_SENDER
    msg["To"]      = GMAIL_RECEIVER

    body = f"""Ciao! 👋

L'uploader automatico ti avvisa che nella cartella Google Drive
"{GDRIVE_FOLDER_NAME}" rimangono solo {count} video:

{names}

Ricordati di:
  1. Eliminare i video già pubblicati
  2. Aggiungere nuovi short alla cartella

La cartella Drive: https://drive.google.com

— Bot YouTube→Instagram/TikTok
"""

    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_SENDER, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_SENDER, GMAIL_RECEIVER, msg.as_string())
        print(f"📧 Email di notifica inviata a {GMAIL_RECEIVER}")
    except Exception as e:
        print(f"⚠️  Errore invio email: {e}")


# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────

def main():
    print(f"\n{'='*55}")
    print(f"  📲 YouTube Shorts → Instagram + TikTok Uploader")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*55}\n")

    state = load_state()
    next_number = state["last_uploaded_number"] + 1
    print(f"🔢 Prossimo short da caricare: #{next_number}")

    # ── Connetti a Google Drive ──────────────────────────────
    drive_service = get_drive_service()
    all_videos = list_drive_videos(drive_service)

    if not all_videos:
        print("❌ Nessun video trovato su Google Drive!")
        return

    # Ordina per numero
    all_videos.sort(key=lambda f: extract_number_from_filename(f["name"]) or 0)
    print(f"📁 Video su Drive: {len(all_videos)} file trovati")

    # Trova il video con il numero corretto
    target_file = None
    for f in all_videos:
        num = extract_number_from_filename(f["name"])
        if num == next_number:
            target_file = f
            break

    if not target_file:
        print(f"⚠️  File 'shorts {next_number}.mp4' non trovato su Drive.")
        print(f"   Assicurati che il file sia nella cartella '{GDRIVE_FOLDER_NAME}'")
        return

    # ── Recupera metadati da YouTube ────────────────────────
    metadata = get_youtube_metadata(YOUTUBE_CHANNEL_URL, next_number)
    caption  = build_caption(metadata, next_number)

    print(f"\n📝 Caption ({len(caption)} caratteri):")
    print(f"   {caption[:120]}...")

    # ── Download video da Drive ──────────────────────────────
    with tempfile.TemporaryDirectory() as tmpdir:
        video_path = download_video_from_drive(
            drive_service,
            target_file["id"],
            target_file["name"],
            tmpdir
        )

        # ── Upload Instagram ─────────────────────────────────
        ig_ok = upload_to_instagram(video_path, caption)

        # ── Upload TikTok ────────────────────────────────────
        tt_ok = upload_to_tiktok(video_path, caption)

    # ── Aggiorna stato ───────────────────────────────────────
    if ig_ok or tt_ok:
        state["last_uploaded_number"] = next_number
        state["uploaded"].append({
            "number":      next_number,
            "file":        target_file["name"],
            "instagram":   ig_ok,
            "tiktok":      tt_ok,
            "uploaded_at": datetime.now().isoformat(),
            "caption":     caption[:100]
        })
        save_state(state)
        print(f"\n✅ Short #{next_number} caricato!")
        print(f"   Instagram: {'✓' if ig_ok else '✗'}  |  TikTok: {'✓' if tt_ok else '✗'}")
    else:
        print(f"\n❌ Upload fallito su entrambe le piattaforme. Riprova domani.")
        return

    # ── Controlla video rimanenti e notifica ─────────────────
    uploaded_numbers = {state["last_uploaded_number"]}
    remaining = [
        f["name"] for f in all_videos
        if extract_number_from_filename(f["name"]) not in uploaded_numbers
        and (extract_number_from_filename(f["name"]) or 0) > next_number
    ]

    print(f"\n📊 Video rimanenti su Drive: {len(remaining)}")

    if len(remaining) <= NOTIFY_THRESHOLD:
        print(f"⚠️  Soglia notifica raggiunta ({NOTIFY_THRESHOLD} video)!")
        send_notification_email(remaining)


if __name__ == "__main__":
    main()
