
import json
import os
import random
import shutil
import subprocess
from pathlib import Path

from atproto import Client, models
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from PIL import Image, ImageOps

# ============================================================
# SEXYBYNATURE - NEW POST
# Random media selection from a limited Drive result.
# Downloads only the selected file.
# Success: move original from ready to used.
# Failure: original stays in ready.
# ============================================================

HANDLE = "sexybynature.bsky.social"
PASSWORD = os.getenv("SBN_PASSWORD")
RAW_SERVICE_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")

DRIVE_ROOT_ID = "1JQx_1SBOETCSMlmlQSIFOjSBuL4op5KA"
DRIVE_ACCOUNT_ID = "1XPdxz-wOSITThYe2NOatvHJSWaBVn8Lw"
DRIVE_READY_ID = "1_78SBsC24L4g4WMPX3xOTHjb6fy0ryAG"
DRIVE_USED_ID = "1uzY8zAJhjd3o236RCoDi3Mn8CDRQkG10"

DOWNLOAD = Path("photo_downloads")
CONVERTED = DOWNLOAD / "converted"

SCOPES = ["https://www.googleapis.com/auth/drive"]

MAX_CANDIDATES = 100

IMAGE_EXT = {
    ".jpg", ".jpeg", ".png", ".webp",
    ".bmp", ".tif", ".tiff"
}

VIDEO_EXT = {
    ".mp4", ".mov", ".m4v", ".avi", ".mkv",
    ".webm", ".wmv", ".flv", ".mpeg", ".mpg",
    ".3gp", ".mts", ".m2ts", ".ogv", ".gif"
}


def log(message):
    print(f"[SexyByNature] {message}", flush=True)


def drive_service():
    if not RAW_SERVICE_JSON:
        raise RuntimeError("Missing GOOGLE_SERVICE_ACCOUNT_JSON.")

    try:
        info = json.loads(RAW_SERVICE_JSON)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Invalid Google service account JSON.") from exc

    credentials = service_account.Credentials.from_service_account_info(
        info,
        scopes=SCOPES
    )

    return build(
        "drive",
        "v3",
        credentials=credentials,
        cache_discovery=False
    )


def get_folder(drive, folder_id):
    return drive.files().get(
        fileId=folder_id,
        fields="id,name,mimeType,parents",
        supportsAllDrives=True
    ).execute()


def verify_folders(drive):
    folders = [
        (DRIVE_ROOT_ID, "Bleusky Photos"),
        (DRIVE_ACCOUNT_ID, "SexyByNature"),
        (DRIVE_READY_ID, "ready"),
        (DRIVE_USED_ID, "used"),
    ]

    result = {}

    for folder_id, expected_name in folders:
        folder = get_folder(drive, folder_id)

        if folder.get("mimeType") != "application/vnd.google-apps.folder":
            raise RuntimeError(f"{expected_name} is not a Drive folder.")

        if folder.get("name", "").casefold() != expected_name.casefold():
            raise RuntimeError(
                f"Folder mismatch: expected {expected_name}, "
                f"found {folder.get('name')}."
            )

        result[expected_name] = folder

    if DRIVE_ACCOUNT_ID not in result["ready"].get("parents", []):
        log("WARNING: ready parent relationship differs from expected.")

    if DRIVE_ACCOUNT_ID not in result["used"].get("parents", []):
        log("WARNING: used parent relationship differs from expected.")

    log("Drive folders verified.")
    return result


def select_random_media(drive):
    # Request only one limited page of file metadata.
    # No recursive scan and no full folder download.

    result = drive.files().list(
        q=f"'{DRIVE_READY_ID}' in parents and trashed=false",
        pageSize=MAX_CANDIDATES,
        orderBy="createdTime desc",
        fields="files(id,name,mimeType,size,createdTime)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True
    ).execute()

    candidates = []

    for item in result.get("files", []):
        extension = Path(item["name"]).suffix.lower()

        if extension in IMAGE_EXT or extension in VIDEO_EXT:
            candidates.append(item)

    if not candidates:
        return None

    selected = random.choice(candidates)

    log(
        f"Random selection from {len(candidates)} candidates: "
        f"{selected['name']}"
    )

    return selected


def download(drive, item):
    DOWNLOAD.mkdir(parents=True, exist_ok=True)

    path = DOWNLOAD / Path(item["name"]).name
    path.unlink(missing_ok=True)

    request = drive.files().get_media(
        fileId=item["id"],
        supportsAllDrives=True
    )

    with path.open("wb") as output:
        downloader = MediaIoBaseDownload(
            output,
            request,
            chunksize=1024 * 1024
        )

        done = False

        while not done:
            status, done = downloader.next_chunk()

            if status:
                log(f"Download: {int(status.progress() * 100)}%")

    expected_size = int(item.get("size", 0))

    if expected_size and path.stat().st_size != expected_size:
        path.unlink(missing_ok=True)
        raise RuntimeError("Downloaded file size mismatch.")

    return path


def move_file(drive, item):
    drive.files().update(
        fileId=item["id"],
        addParents=DRIVE_USED_ID,
        removeParents=DRIVE_READY_ID,
        fields="id,name,parents",
        supportsAllDrives=True
    ).execute()


def run_ffmpeg(args):
    if not shutil.which("ffmpeg"):
        raise RuntimeError("FFmpeg is not installed.")

    process = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            *args
        ],
        capture_output=True,
        text=True
    )

    if process.returncode:
        raise RuntimeError(
            (process.stderr or process.stdout or "FFmpeg failed")[-2500:]
        )


def prepare_image(path):
    with Image.open(path) as original:
        orientation = original.getexif().get(274, 1)
        image = ImageOps.exif_transpose(original)
        image.load()

        image.thumbnail(
            (2000, 2000),
            Image.Resampling.LANCZOS
        )

        needs_jpeg = (
            path.suffix.lower() == ".webp"
            or path.stat().st_size > 900_000
            or image.mode not in ("RGB", "L")
            or orientation != 1
        )

        if not needs_jpeg:
            return path, image.width, image.height

        CONVERTED.mkdir(parents=True, exist_ok=True)
        output_path = CONVERTED / f"{path.stem}_converted.jpg"

        if image.mode in ("RGBA", "LA") or "transparency" in image.info:
            rgba = image.convert("RGBA")
            background = Image.new("RGB", rgba.size, "white")
            background.paste(rgba, mask=rgba.getchannel("A"))
            image = background
        else:
            image = image.convert("RGB")

        image.save(
            output_path,
            "JPEG",
            quality=90,
            optimize=True
        )

    with Image.open(output_path) as check:
        return output_path, check.width, check.height


def prepare_video(path):
    CONVERTED.mkdir(parents=True, exist_ok=True)

    output_path = CONVERTED / f"{path.stem}_compatible.mp4"

    video_filter = (
        "scale=w='min(1920,iw)':h='min(1920,ih)':"
        "force_original_aspect_ratio=decrease:"
        "force_divisible_by=2,"
        "setsar=1,format=yuv420p"
    )

    if path.suffix.lower() == ".gif":
        run_ffmpeg([
            "-i", str(path),
            "-vf", video_filter + ",fps=30",
            "-an",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "23",
            "-movflags", "+faststart",
            str(output_path)
        ])
    else:
        run_ffmpeg([
            "-i", str(path),
            "-map", "0:v:0",
            "-map", "0:a?",
            "-vf", video_filter,
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "23",
            "-c:a", "aac",
            "-b:a", "128k",
            "-movflags", "+faststart",
            str(output_path)
        ])

    if not output_path.exists() or not output_path.stat().st_size:
        raise RuntimeError("Video conversion failed.")

    return output_path


def main():
    if not PASSWORD:
        raise RuntimeError("Missing GitHub Secret SBN_PASSWORD.")

    if not RAW_SERVICE_JSON:
        raise RuntimeError("Missing GOOGLE_SERVICE_ACCOUNT_JSON.")

    drive = drive_service()
    verify_folders(drive)

    item = select_random_media(drive)

    if not item:
        log("No supported media in ready. Nothing to post.")
        return

    local_path = download(drive, item)
    extension = local_path.suffix.lower()

    if extension in IMAGE_EXT:
        upload_path, width, height = prepare_image(local_path)
        kind = "image"

        log(f"Image dimensions: {width}x{height}")

    elif extension in VIDEO_EXT:
        upload_path = prepare_video(local_path)
        kind = "video"

    else:
        raise RuntimeError(f"Unsupported file: {extension}")

    client = Client()
    client.login(HANDLE, PASSWORD)

    log(f"Logged in as {HANDLE}.")

    with upload_path.open("rb") as media_file:
        blob = client.upload_blob(media_file.read()).blob

    if kind == "image":
        image = models.AppBskyEmbedImages.Image(
            alt="",
            image=blob,
            aspect_ratio={
                "width": width,
                "height": height
            }
        )

        embed = models.AppBskyEmbedImages.Main(
            images=[image]
        )

    else:
        embed = models.AppBskyEmbedVideo.Main(
            video=blob,
            alt=None
        )

    response = client.send_post(
        text="",
        embed=embed
    )

    if not response:
        raise RuntimeError("No Bluesky post response.")

    log(f"Post successful: {getattr(response, 'uri', 'URI unavailable')}")

    # Move original only after confirmed successful post.
    move_file(drive, item)

    log(f"Moved original to used: {item['name']}")
    log("SEXYBYNATURE NEW POST COMPLETE")


if __name__ == "__main__":
    main()
