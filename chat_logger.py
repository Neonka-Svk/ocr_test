"""Twitch chat logger - anonymne (bez prihlasenia) cita chat a zapisuje ho do suboru.

Pouzitie:  python chat_logger.py herdyn
Vystup:    logs/<kanal>_<datum>.log        - cely chat
           logs/<kanal>_<datum>_kody.log   - spravy, ktore vyzeraju ako kod,
                                             alebo spravy od streamera/modov/VIP
"""
import os
import re
import socket
import ssl
import sys
import time
from datetime import datetime
from pathlib import Path

HOST, PORT = "irc.chat.twitch.tv", 6697
channel = (sys.argv[1] if len(sys.argv) > 1 else "herdyn").lower().lstrip("#")

log_dir = Path(__file__).parent / "logs"
log_dir.mkdir(exist_ok=True)
stamp = datetime.now().strftime("%Y-%m-%d")   # jeden subor za den, restart len dopisuje
all_log = log_dir / f"{channel}_{stamp}.log"
code_log = log_dir / f"{channel}_{stamp}_kody.log"

# slova typu "AB12-CD34", "XK7P9Q2M" (mix pismen a cisel, 5+ znakov) alebo zmienka o kode
CODE_RE = re.compile(r"\b(?=[A-Za-z0-9-]*\d)(?=[A-Za-z0-9-]*[A-Za-z])[A-Za-z0-9-]{5,}\b")
KEYWORD_RE = re.compile(r"k[oó]d|code|promo|giveaway|redeem", re.IGNORECASE)
IMPORTANT_BADGES = ("broadcaster", "moderator", "vip")


# kod na stream.herdyn.cz ma presne 12 znakov
import unicodedata


def strip_accents(s: str) -> str:
    """Zitrek -> Zitrek: pismena s diakritikou na zakladne (dlzka textu ostava rovnaka)."""
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))

GIVEAWAY_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9]{8,20}(?![A-Za-z0-9])")
alert_log = log_dir / f"{channel}_{stamp}_ALERT.log"
seen_by: dict[str, set[str]] = {}   # kandidat -> kto ho napisal
alerted: set[str] = set()


def looks_like_giveaway_code(tok: str) -> bool:
    has_digit = any(c.isdigit() for c in tok)
    has_alpha = any(c.isalpha() for c in tok)
    mixed = any(c.isupper() for c in tok[1:]) and any(c.islower() for c in tok)
    return has_alpha and (has_digit or mixed)


TOAST_PS = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null
$t = [Security.SecurityElement]::Escape($env:TOAST_TITLE)
$b = [Security.SecurityElement]::Escape($env:TOAST_BODY)
$x = New-Object Windows.Data.Xml.Dom.XmlDocument
$x.LoadXml("<toast scenario='reminder'><visual><binding template='ToastGeneric'><text>$t</text><text>$b</text></binding></visual><actions><action content='OK' arguments='ok' activationType='system'/></actions><audio src='ms-winsoundevent:Notification.Looping.Alarm' loop='false'/></toast>")
$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show([Windows.UI.Notifications.ToastNotification]::new($x))
"""


def push(title: str, body: str) -> None:
    """Push na mobil cez ntfy.sh, ak je nastavena premenna NTFY_TOPIC."""
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        return
    import urllib.request
    try:
        req = urllib.request.Request(f"https://ntfy.sh/{topic}", data=body.encode("utf-8"),
                                     headers={"Title": title.encode("ascii", "ignore").decode(), "Priority": "high"})
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:  # noqa: BLE001
        print("ntfy zlyhalo:", e, flush=True)


def toast(title: str, body: str) -> None:
    """Windows notifikacia - spusti sa v samostatnom procese, logger necaka."""
    import os
    import subprocess
    env = dict(os.environ, TOAST_TITLE=title, TOAST_BODY=body)
    subprocess.Popen(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", TOAST_PS],
                     env=env, creationflags=0x08000000)  # CREATE_NO_WINDOW


def alert(code: str, why: str, line: str) -> None:
    import subprocess
    write(alert_log, f"{code}   ({why})   {line}")
    print(f"\n*** MOZNY KOD: {code}  ({why}) ***\n", flush=True)
    push(f"MOZNY KOD: {code}", f"{why}\n{line[:200]}")
    if os.name != "nt":
        return
    try:
        subprocess.run("clip", input=code.encode("utf-16-le"), check=False)
        toast(f"MOZNY KOD: {code}", f"{why} (v schranke)\n{line[:150]}")
    except OSError:
        pass


BOTS = {"streamelements", "nightbot", "fossabot", "moobot", "streamlabs", "wizebot"}
known_names: set[str] = set()


EMOTE_LIKE_RE = re.compile(r"^[a-z]+\d*[A-Z][a-z]+\d*$")   # napr. skywhi1Smirk, donkHey


def emote_spans(emotes_tag: str):
    spans = []
    for part in emotes_tag.split("/"):
        _, _, ranges = part.partition(":")
        for r in ranges.split(","):
            a, _, b = r.partition("-")
            if a.isdigit() and b.isdigit():
                spans.append((int(a), int(b)))
    return spans


def check_giveaway(nick: str, text: str, important: bool, line: str, emotes_tag: str = "") -> None:
    text = strip_accents(text)
    known_names.add(nick.lower())
    if nick.lower() in BOTS:
        return
    for mention in re.findall(r"@(\w+)", text):
        known_names.add(mention.lower())   # @meno nie je kod
    spans = emote_spans(emotes_tag)
    for m in GIVEAWAY_RE.finditer(text):
        tok = m.group(0)
        if any(m.start() <= e and s <= m.end() - 1 for s, e in spans):   # oficialny emote
            continue
        # 7TV/BTTV emote (FeelsDankMan). Dlhsie CamelCase (ZitrekBudeLepsi) alebo pri slove "kod" nechame prejst.
        kod_ctx = re.search(r"k[oó]d|code", text, re.IGNORECASE)
        if EMOTE_LIKE_RE.match(tok) or (re.fullmatch(r"(?:[A-Z][a-z]+){2,}", tok) and len(tok) <= 12 and not kod_ctx):
            continue
        if not looks_like_giveaway_code(tok) or tok in alerted:
            continue
        if len(set(tok.lower())) <= 3:     # xDDDDDDD, hahahaha
            continue
        if tok.lower() in known_names:   # meno chattera, nie kod
            continue
        users = seen_by.setdefault(tok, set())
        users.add(nick.lower())
        if important:
            why = f"napisal {nick} (streamer/mod/VIP)"
        elif len(users) >= 3:
            why = f"napisalo uz {len(users)} ludi"
        else:
            continue
        alerted.add(tok)
        alert(tok, why, line)


from collections import deque  # noqa: E402

recent = deque()          # (ts, nick, text, emotes)  posledne 3 minuty
claims = deque()          # (ts, nick)               "Code was succesfully claimed"
last_confirmed = [0.0]
CLAIM_RE = re.compile(r"succe?s+fully\s+claimed|u[sš]pe[sš]n[eě]\s+(?:claim|uplat)", re.IGNORECASE)
TOKEN_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9]{6,24}(?![A-Za-z0-9])")
KOD_CTX_RE = re.compile(
    r"(?:!k[oó]d\s+|k[oó]d\s*(?:je|is|:)\s*|code\s*(?:is|:)\s*)([A-Za-z0-9]{6,24})(?![A-Za-z0-9])",
    re.IGNORECASE)


def candidates(now: float, top: int = 5):
    """Slova z poslednych 3 minut, ktore by mohli byt kod (zmes velkych/malych/cisel)."""
    score: dict[str, set[str]] = {}
    last_seen: dict[str, float] = {}
    ctx: set[str] = set()
    for ts, nick, text, emotes in recent:
        if now - ts > 180 or "http" in text or nick.lower() in BOTS:
            continue
        spans = emote_spans(emotes)
        for m in TOKEN_RE.finditer(text):
            tok = m.group(0)
            if any(m.start() <= e and s <= m.end() - 1 for s, e in spans):
                continue
            if tok.lower() in known_names or EMOTE_LIKE_RE.match(tok):
                continue
            has_digit = any(c.isdigit() for c in tok)
            mixed = any(c.isupper() for c in tok[1:]) and any(c.islower() for c in tok)
            if not (has_digit or mixed or tok.isupper()):
                continue                      # obycajne slovo / meno
            score.setdefault(tok, set()).add(nick.lower())
            last_seen[tok] = ts
        for m in KOD_CTX_RE.finditer(text):
            ctx.add(m.group(1))
    # pri zhode vyhra naposledy spomenuty (potvrdenia chodia hned po spravnom kode)
    ranked = sorted(score, key=lambda t: (len(score[t]) + (2 if t in ctx else 0), last_seen[t]), reverse=True)
    return [(t, len(score[t]), t in ctx) for t in ranked[:top]]


def observe(nick: str, text: str, emotes_tag: str, ts: float, line: str) -> None:
    """Hlasi, ked viac roznych ludi potvrdi, ze kod zabral. Holy '!kod' spam sa ignoruje."""
    text = strip_accents(text)
    known_names.add(nick.lower())
    recent.append((ts, nick, text, emotes_tag))
    while recent and ts - recent[0][0] > 180:
        recent.popleft()
    if nick.lower() not in BOTS and CLAIM_RE.search(text):
        claims.append((ts, nick.lower()))
    while claims and ts - claims[0][0] > 60:
        claims.popleft()
    if len({n for _, n in claims}) >= 4 and ts - last_confirmed[0] > 600:
        last_confirmed[0] = ts
        cands = candidates(ts)
        if cands:
            txt = ", ".join(f"{t} ({n}x{', po kod' if c else ''})" for t, n, c in cands)
            alert(cands[0][0], f"KOD ASI PADOL - potvrdilo {len({n for _, n in claims})} ludi. Kandidati: {txt}", line)


def write(path: Path, line: str) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def parse(raw: str):
    """Vrati (tags, prikaz, nick, text) z jedneho IRC riadku."""
    tags = {}
    if raw.startswith("@"):
        tag_str, raw = raw[1:].split(" ", 1)
        for kv in tag_str.split(";"):
            k, _, v = kv.partition("=")
            tags[k] = v
    prefix = ""
    if raw.startswith(":"):
        prefix, raw = raw[1:].split(" ", 1)
    text = ""
    if " :" in raw:
        raw, text = raw.split(" :", 1)
    parts = raw.split()
    cmd = parts[0] if parts else ""
    nick = tags.get("display-name") or prefix.split("!")[0]
    return tags, cmd, nick, text


def handle_line(sock, raw: str) -> None:
    if raw.startswith("PING"):
        sock.sendall(raw.replace("PING", "PONG", 1).encode() + b"\r\n")
        return
    tags, cmd, nick, text = parse(raw)
    if cmd == "RECONNECT":
        raise ConnectionError("server poziadal o reconnect")
    if cmd not in ("PRIVMSG", "USERNOTICE"):
        return
    now = datetime.now().strftime("%H:%M:%S")
    badges = tags.get("badges", "")
    if cmd == "USERNOTICE":
        text = tags.get("system-msg", "").replace("\\s", " ") + (" | " + text if text else "")
    line = f"[{now}] {nick}: {text}"
    write(all_log, line)

    important = any(b in badges for b in IMPORTANT_BADGES)
    if cmd == "PRIVMSG":
        check_giveaway(nick, text, important, line, tags.get("emotes", ""))
        observe(nick, text, tags.get("emotes", ""), time.time(), line)
    looks_like_code = bool(CODE_RE.search(text) or KEYWORD_RE.search(text))
    if important or looks_like_code:
        role = next((b for b in IMPORTANT_BADGES if b in badges), "")
        write(code_log, f"{line}   <{role}>" if role else line)
        print(f"!! {line}", flush=True)


def run_once() -> None:
    raw_sock = socket.create_connection((HOST, PORT), timeout=360)
    sock = ssl.create_default_context().wrap_socket(raw_sock, server_hostname=HOST)
    nick = f"justinfan{int(time.time()) % 100000}"
    for msg in ("CAP REQ :twitch.tv/tags twitch.tv/commands", "PASS SCHMOOPIIE",
                f"NICK {nick}", f"JOIN #{channel}"):
        sock.sendall(msg.encode() + b"\r\n")
    print(f"Pripojene k #{channel}, logujem do {all_log}", flush=True)
    write(all_log, f"--- pripojene {datetime.now():%Y-%m-%d %H:%M:%S} ---")
    buf = b""
    while True:
        data = sock.recv(65536)
        if not data:
            raise ConnectionError("spojenie zatvorene")
        buf += data
        *lines, buf = buf.split(b"\r\n")
        for line in lines:
            if line:
                handle_line(sock, line.decode("utf-8", errors="replace"))


def main() -> None:
    delay = 2
    while True:
        try:
            run_once()
        except KeyboardInterrupt:
            print("Koniec.")
            return
        except Exception as e:  # noqa: BLE001 - pri akejkolvek chybe sa znovu pripojime
            print(f"Odpojene ({e}), skusam znova o {delay}s", flush=True)
            write(all_log, f"--- odpojene {datetime.now():%H:%M:%S}: {e} ---")
            time.sleep(delay)
            delay = min(delay * 2, 60)
        else:
            delay = 2


if __name__ == "__main__":
    main()
