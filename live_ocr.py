"""Sleduje ZIVY obraz streamu, cita z neho text (Tesseract OCR) a hlasi slova, ktore vyzeraju ako kod.

Pouzitie:  python live_ocr.py herdyn            (zivy stream, caka kym zacne)
           python live_ocr.py --file video.mp4  (test na stiahnutom zazname)

Disk:  snimky sa NEukladaju priebezne. V pamati drzim poslednych ~30 s.
       Na disk ide len to, kde sa objavil kandidat na kod (ocr_hits/<datum>/),
       a ocr_hits starsie ako KEEP_DAYS dni sa pri starte zmazu.
"""
import os
import re
import shutil
import subprocess
import sys
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path

TESSERACT = os.environ.get("TESSERACT") or shutil.which("tesseract") or r"C:\Program Files\Tesseract-OCR\tesseract.exe"
CF = 0x08000000 if os.name == "nt" else 0   # bez okna na Windows
FPS = int(os.environ.get("OCR_FPS", 2))      # kolko snimok za sekundu citat
WORKERS = int(os.environ.get("OCR_WORKERS", 4))            # paralelne OCR procesy (1 snimka ~0.5 s)
W, H = 1920, 1080
BUFFER_SEC = 30         # kolko sekund obrazu drzat v pamati
KEEP_DAYS = 3           # starsie ocr_hits sa mazu
HUD_MEMORY = 600        # slovo videne v poslednych 10 min = sucast hry/HUD, nehlasit
CONFIRM_FRAMES = int(os.environ.get("OCR_CONFIRM", 4))     # kandidat musi byt rovnako precitany aspon na 4 snimkach (2 s)
MIN_LEN = 8             # kratsie slova su skoro vzdy sum z ikon/HUD

BASE = Path(__file__).parent
HITS = BASE / "ocr_hits"
TOKEN_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9]{6,20}(?![A-Za-z0-9])")


def cleanup_old() -> None:
    HITS.mkdir(exist_ok=True)
    limit = datetime.now() - timedelta(days=KEEP_DAYS)
    for d in HITS.iterdir():
        if d.is_dir() and datetime.fromtimestamp(d.stat().st_mtime) < limit:
            shutil.rmtree(d, ignore_errors=True)
            print(f"zmazane stare snimky: {d.name}", flush=True)


def looks_like_code(tok: str) -> bool:
    has_digit = any(c.isdigit() for c in tok)
    has_alpha = any(c.isalpha() for c in tok)
    lowers = sum(c.islower() for c in tok)
    mixed = any(c.isupper() for c in tok[1:]) and lowers > 0
    if not has_digit and lowers == 1:      # OCR preklep typu "SUBWOOFEr"
        return False
    if re.fullmatch(r"\d+[xX]\d+", tok):      # rozlisenie typu 3840x2160
        return False
    letters = [c for c in tok if c.isalpha()]
    switches = sum(a.isupper() != b.isupper() for a, b in zip(letters, letters[1:]))
    if not has_digit and switches > 3:     # rozskakane pismena typu "pRoFILes"
        return False
    return has_alpha and (has_digit or mixed)


def ocr(jpg: bytes) -> str:
    r = subprocess.run([TESSERACT, "stdin", "stdout", "--psm", "11"], input=jpg,
                       capture_output=True, creationflags=CF)
    return r.stdout.decode("utf-8", errors="replace")


def encode_jpg(raw: bytes) -> bytes:
    r = subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
                        "-s", f"{W}x{H}", "-i", "pipe:0", "-q:v", "3", "-f", "mjpeg", "pipe:1"],
                       input=raw, capture_output=True, creationflags=CF)
    return r.stdout


def notify(code: str, text: str) -> None:
    try:
        from chat_logger import toast, push
        push(f"KOD NA OBRAZE: {code}", text)
        if os.name == "nt":
            subprocess.run("clip", input=code.encode("utf-16-le"), check=False)
            toast(f"KOD NA OBRAZE: {code}", text)
    except Exception as e:  # noqa: BLE001
        print("notifikacia zlyhala:", e, flush=True)


class Detector:
    def __init__(self):
        self.seen_at: dict[str, float] = {}     # slovo -> kedy naposledy (HUD pamat)
        self.first_at: dict[str, float] = {}    # slovo -> kedy sa prvy raz objavilo v tejto vlne
        self.count: dict[str, int] = {}
        self.alerted: set[str] = set()
        self.reported: set[str] = set()
        self.started = None

    def feed(self, t: float, text: str):
        if self.started is None:
            self.started = t
        new = []
        for tok in set(TOKEN_RE.findall(text)):
            if len(tok) < MIN_LEN or not looks_like_code(tok):
                continue
            last = self.seen_at.get(tok)
            if last is None or t - last > HUD_MEMORY:
                self.first_at[tok] = t
                self.count[tok] = 0
            self.seen_at[tok] = t
            # slovo, ktore je na obraze dlhsie ako 2 minuty v kuse, je HUD/hra
            if t - self.first_at[tok] > 120:
                continue
            self.count[tok] += 1
            if self.count[tok] >= CONFIRM_FRAMES and tok not in self.alerted:
                self.alerted.add(tok)
                # varianta niecoho uz nahlaseneho (OCR cita kazdu snimku trochu inak) -> ticho
                low = tok.lower()
                if any(SequenceMatcher(None, low, a.lower()).ratio() > 0.7 for a in self.reported):
                    continue
                self.reported.add(tok)
                new.append(tok)
        return new


def frames_from(cmd_src):
    """Spusti streamlink/ffmpeg a vracia raw RGB snimky (FPS za sekundu)."""
    ff = subprocess.Popen(["ffmpeg", "-loglevel", "error", "-i", "pipe:0" if cmd_src else sys.argv[2],
                           "-vf", f"fps={FPS},scale={W}:{H}", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
                          stdin=cmd_src.stdout if cmd_src else None, stdout=subprocess.PIPE,
                          creationflags=CF)
    size = W * H * 3
    while True:
        raw = ff.stdout.read(size)
        if len(raw) < size:
            return
        yield raw


def run(source) -> None:
    det = Detector()
    buf: deque = deque(maxlen=BUFFER_SEC * FPS)     # (cas, raw) - len v pamati
    pool = ThreadPoolExecutor(WORKERS)
    pending: deque = deque()
    t0 = time.time()
    n = 0
    for raw in frames_from(source):
        t = n / FPS          # cas od zaciatku
        n += 1
        buf.append((t, raw))
        pending.append((t, raw, pool.submit(lambda r=raw: ocr(encode_jpg(r)))))
        while pending and (pending[0][2].done() or len(pending) > WORKERS * 3):
            pt, praw, fut = pending.popleft()
            for code in det.feed(pt, fut.result()):
                save_hit(code, pt, praw, buf)
                wall = datetime.now().strftime("%H:%M:%S")
                print(f"[{wall}] KANDIDAT NA OBRAZE: {code}  (t={pt:.0f}s)", flush=True)
                notify(code, f"zachytene na streame o {wall}, snimka v ocr_hits")
        if n % (FPS * 60) == 0:
            lag = (time.time() - t0) - t
            print(f"bezi {t / 60:.0f} min, oneskorenie OCR {lag:+.0f}s", flush=True)
    for pt, praw, fut in pending:
        for code in det.feed(pt, fut.result()):
            save_hit(code, pt, praw, buf)
            print(f"KANDIDAT NA OBRAZE: {code}  (t={pt:.0f}s)", flush=True)


def save_hit(code: str, t: float, raw: bytes, buf) -> None:
    d = HITS / datetime.now().strftime("%Y-%m-%d")
    d.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%H-%M-%S")
    (d / f"{stamp}_{code}.jpg").write_bytes(encode_jpg(raw))
    with (d / "zoznam.txt").open("a", encoding="utf-8") as f:
        f.write(f"{datetime.now():%H:%M:%S}  {code}  (t={t:.0f}s)\n")


def main() -> None:
    cleanup_old()
    if len(sys.argv) > 2 and sys.argv[1] == "--file":
        run(None)
        return
    channel = (sys.argv[1] if len(sys.argv) > 1 else "herdyn").lower()
    while True:
        print(f"[{datetime.now():%H:%M:%S}] pripajam sa na twitch.tv/{channel} ...", flush=True)
        sl = subprocess.Popen([sys.executable, "-m", "streamlink", "--twitch-disable-ads",
                               "--stream-segment-threads", "2", "-O", f"https://www.twitch.tv/{channel}",
                               "1080p60,1080p,720p60,best"],
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=CF)
        run(sl)
        sl.kill()
        print(f"[{datetime.now():%H:%M:%S}] stream nebezi alebo spadol, skusim o 60 s", flush=True)
        time.sleep(60)


if __name__ == "__main__":
    os.chdir(BASE)
    main()
