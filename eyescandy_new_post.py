import json
import os
import shutil
import subprocess
from pathlib import Path

from atproto import Client, models
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from PIL import Image, ImageOps

# ============================================================
# EYESCANDY - NEW POST
# Posts the oldest supported media file from Google Drive/ready.
# On success: moves the original to Google Drive/used.
# On failure: leaves the original in Google Drive/ready.
# ============================================================

HANDLE = "eyescandy.bsky.social"
PASSWORD = os.getenv("EYESCANDY_PASSWORD")
RAW_SERVICE_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")

DRIVE_ROOT_ID = "1JQx_1SBOETCSMlmlQSIFOjSBuL4op5KA"
DRIVE_ACCOUNT_ID = "1pHJJ7MhlhFUljU2XUHsS8yKaGj-ws0X0"
DRIVE_READY_ID = "1hBt9c6yLNEhl4Apbmt0kKhp6mOwTeA2a"
DRIVE_USED_ID = "1B99lfVaFqLsIsitdj5eGVd3Rau9Q8sTK"

DOWNLOAD = Path("photo_downloads")
CONVERTED = DOWNLOAD / "converted"

SCOPES = ["https://www.googleapis.com/auth/drive"]

IMAGE_EXT = {
    ".jpg", ".jpeg", ".png", ".webp",
    ".bmp", ".tif", ".tiff"
}

VIDEO_EXT = {
    ".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm",
    ".wmv", ".flv", ".mpeg", ".mpg", ".3gp",
    ".mts", ".m2ts", ".ogv", ".gif"
}


def log(message):
    print(f"[Eyescandy New Post] {message}", flush=True)


def drive_service():
    if not RAW_SERVICE_JSON:
        raise RuntimeError(
            "Missing GitHub Secret GOOGLE_SERVICE_ACCOUNT_JSON."
        )

    try:
        info = json.loads(RAW_SERVICE_JSON)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"GOOGLE_SERVICE_ACCOUNT_JSON is invalid JSON: {exc}"
        ) from exc

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
    root = get_folder(drive, DRIVE_ROOT_ID)
    account = get_folder(drive, DRIVE_ACCOUNT_ID)
    ready = get_folder(drive, DRIVE_READY_ID)
    used = get_folder(drive, DRIVE_USED_ID)

    folder_mime = "application/vnd.google-apps.folder"

    expected = [
        (root, "Bleusky Photos"),
        (account, "Eyescandy"),
        (ready, "ready"),
        (used, "used"),
    ]

    for folder, expected_name in expected:
        if folder.get("mimeType") != folder_mime:
            raise RuntimeError(
                f"Drive ID for '{expected_name}' is not a folder."
            )

        actual_name = folder.get("name", "")

        if actual_name.casefold() != expected_name.casefold():
            raise RuntimeError(
                f"Drive folder ID mismatch: expected '{expected_name}', "
                f"found '{actual_name}'."
            )

    # Check parent relationships. A mismatch is logged rather than
    # stopping the publisher, because Drive parent metadata can differ.
    if DRIVE_ROOT_ID not in account.get("parents", []):
        log(
            "WARNING: Eyescandy parent ID does not match "
            "Bleusky Photos. Continuing after folder-name checks."
        )

    if DRIVE_ACCOUNT_ID not in ready.get("parents", []):
        log(
            "WARNING: ready parent ID does not match Eyescandy."
        )

    if DRIVE_ACCOUNT_ID not in used.get("parents", []):
        log(
            "WARNING: used parent ID does not match Eyescandy."
        )

    log("Verified folder names and IDs.")
    return {
        "ready": ready,
        "used": used
    }


def list_media(drive, ready_id):
    found = []
    page_token = None

    while True:
        result = drive.files().list(
            q=f"'{ready_id}' in parents and trashed=false",
            pageSize=1000,
            pageToken=page_token,
            orderBy="createdTime",
            fields=(
                "nextPageToken,"
                "files(id,name,mimeType,size,createdTime)"
            ),
            supportsAllDrives=True,
            includeItemsFromAllDrives=True
        ).execute()

        for item in result.get("files", []):
            extension = Path(item["name"]).suffix.lower()

            if extension in IMAGE_EXT or extension in VIDEO_EXT:
                found.append(item)

        page_token = result.get("nextPageToken")

        if not page_token:
            return found


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
                log(
                    f"Download "
                    f"{int(status.progress() * 100)}%"
                )

    expected_size = int(item.get("size", 0))

    if expected_size and path.stat().st_size != expected_size:
        path.unlink(missing_ok=True)
        raise RuntimeError(
            "Downloaded file size does not match Google Drive."
        )

    return path


def move_file(drive, item, source_id, target_id):
    drive.files().update(
        fileId=item["id"],
        addParents=target_id,
        removeParents=source_id,
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
            (
                process.stderr
                or process.stdout
                or "FFmpeg failed"
            )[-2500:]
        )


def prepare_image(path):
    # Preserve original aspect ratio.
    # No cropping, stretching or padding.
    with Image.open(path) as original:
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
        )

        if not needs_jpeg:
            width, height = image.size
            return path, width, height

        CONVERTED.mkdir(parents=True, exist_ok=True)

        output_path = (
            CONVERTED / f"{path.stem}_converted.jpg"
        )

        if (
            image.mode in ("RGBA", "LA")
            or "transparency" in image.info
        ):
            rgba = image.convert("RGBA")
            background = Image.new("RGB", rgba.size, "white")
            background.paste(
                rgba,
                mask=rgba.getchannel("A")
            )
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
    # Preserve aspect ratio.
    # No cropping, stretching or padding bars.
    CONVERTED.mkdir(parents=True, exist_ok=True)

    output_path = (
        CONVERTED / f"{path.stem}_compatible.mp4"
    )

    video_filter = (
        "scale=w='min(1920,iw)':h=-2:"
        "force_original_aspect_ratio=decrease,"
        "setsar=1,format=yuv420p"
    )

    if path.suffix.lower() == ".gif":
        run_ffmpeg([
            "-i", str(path),
            "-vf", video_filter + ",fps=30",
            "-an",
            "-movflags", "+faststart",
            str(output_path)
        ])
    else:
        run_ffmpeg([
            "-i", str(path),
            "-vf", video_filter,
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "23",
            "-c:a", "aac",
            "-b:a", "128k",
            "-movflags", "+faststart",
            str(output_path)
        ])

    if (
        not output_path.exists()
        or not output_path.stat().st_size
    ):
        raise RuntimeError(
            "Video conversion did not produce an MP4."
        )

    return output_path


def main():
    if not PASSWORD:
        raise RuntimeError(
            "Missing GitHub Secret EYESCANDY_PASSWORD."
        )

    if not RAW_SERVICE_JSON:
        raise RuntimeError(
            "Missing GitHub Secret GOOGLE_SERVICE_ACCOUNT_JSON."
        )

    drive = drive_service()

    folders = verify_folders(drive)

    ready_id = folders["ready"]["id"]
    used_id = folders["used"]["id"]

    candidates = list_media(drive, ready_id)

    if not candidates:
        log("No supported media in ready. Nothing to post.")
        return

    # Oldest supported file first.
    item = candidates[0]

    log(f"Selected oldest ready file: {item['name']}")

    local_path = download(drive, item)
    extension = local_path.suffix.lower()

    # If preparation fails, original stays in ready.
    if extension in IMAGE_EXT:
        upload_path, width, height = prepare_image(local_path)

        kind = "image"

        log(
            f"Image dimensions for Bluesky: "
            f"{width}x{height}"
        )

    elif extension in VIDEO_EXT:
        upload_path = prepare_video(local_path)

        width = None
        height = None
        kind = "video"

    else:
        raise RuntimeError(
            f"Unsupported file type: {extension}"
        )

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
        raise RuntimeError(
            "Bluesky returned no post response; "
            "original stays in ready."
        )

    log(
        "Post successful: "
        f"{getattr(response, 'uri', 'URI unavailable')}"
    )

    # Move original only after successful Bluesky post.
    move_file(
        drive,
        item,
        ready_id,
        used_id
    )

    log(
        "Original file moved from "
        "Eyescandy/ready to Eyescandy/used."
    )

    log("EYESCANDY NEW POST COMPLETE")


if __name__ == "__main__":
    main()