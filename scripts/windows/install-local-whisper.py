"""Install the pinned MIT multilingual tiny Whisper model into this user's private steward profile."""
import hashlib
import os
from pathlib import Path
import tempfile
import time
from urllib.parse import urljoin, urlsplit

import httpx

REPO = "Systran/faster-whisper-tiny"
REV = "d90ca5fe260221311c53c58e660288d3deb8d356"
# Verified against the publisher's pinned Git blobs and LFS SHA-256.
FILES = {
    "config.json": (2249, "a73a28cdfe1c43ccc7202fa333d1f89c202477271407ae9a7f19afa52039cac8"),
    "model.bin": (75538270, "dcb76c6586fc06cbdac6dd21f14cfd129cc4cdd9dce19bf4ffa62e59cbe6e6d1"),
    "tokenizer.json": (2203239, "fb7b63191e9bb045082c79fd742a3106a12c99513ab30df4a0d47fa6cb6fd0ab"),
    "vocabulary.txt": (459861, "34ce3fe1c5041027b3f8d42912270993f986dbc4bb34cf27f951e34a1e453913"),
}


def fetch(client, url, limit, sink):
    start = time.monotonic()
    for _ in range(8):
        target = urlsplit(url)
        host = target.hostname or ""
        if (target.scheme != "https" or target.username or target.password
                or not (host == "huggingface.co" or host.endswith((".huggingface.co",".hf.co")))):
            raise ValueError("Unexpected model download origin")
        with client.stream("GET", url) as response:
            if response.is_redirect:
                url = urljoin(url,response.headers["location"])
                continue
            response.raise_for_status()
            size = 0
            for chunk in response.iter_bytes(128*1024):
                size += len(chunk)
                if size > limit or time.monotonic()-start > 180:
                    raise ValueError("Download exceeded size or time limit")
                sink(chunk)
            return size
    raise ValueError("Too many model redirects")


def install():
    profile = Path(os.environ["LOCALAPPDATA"]) / "SuparagentSteward"
    if not profile.is_dir():
        raise ValueError("Initialize the private steward profile first")
    parent = profile / "models"
    parent.mkdir(exist_ok=True)
    dest = parent / "faster-whisper-tiny"
    if dest.exists():
        for name, (size, digest) in FILES.items():
            path = dest / name
            if not path.is_file() or path.stat().st_size != size:
                raise ValueError("Existing model directory is incomplete; no files were overwritten")
            with path.open("rb") as artifact:
                if hashlib.file_digest(artifact, "sha256").hexdigest() != digest:
                    raise ValueError("Existing model hash mismatch; no files were overwritten")
        print("Verified existing local model.")
        return
    total = sum(size for size, _ in FILES.values())
    with httpx.Client(timeout=httpx.Timeout(25,connect=12),headers={"Accept-Encoding":"identity"}) as client:
        with tempfile.TemporaryDirectory(dir=parent,prefix=".whisper-install-") as tmp:
            stage = Path(tmp) / "model"
            stage.mkdir()
            for name, (expected_size, expected_hash) in FILES.items():
                sha = hashlib.sha256()
                with (stage/name).open("xb") as out:
                    def write(part):
                        out.write(part)
                        sha.update(part)
                    size = fetch(client,f"https://huggingface.co/{REPO}/resolve/{REV}/{name}?download=true",expected_size,write)
                if size != expected_size or sha.hexdigest() != expected_hash:
                    raise ValueError("Hash or size mismatch for "+name)
                print(name,size,sha.hexdigest(),flush=True)
            stage.rename(dest)
    print("Installed",total,"bytes in",dest)


if __name__ == "__main__":
    try:
        install()
    except (httpx.HTTPError, OSError, ValueError, KeyError) as exc:
        # Do not log signed URLs on a transport error.
        raise SystemExit("Install failed: "+(str(exc) if not isinstance(exc,httpx.HTTPError) else type(exc).__name__)) from None
