"""Fetching e parsing delle fonti gratuite di pallanuoto Serie A1.

Fonti:
 - wpdworld.com  : news, risultati+parziali, tabellini (marcatori), classifica
 - prorecco.com  : API REST WordPress (news + rosa per ruolo)
 - Google News   : RSS per ricerca libera per squadra/giocatore
"""

import json
import os
import re
import subprocess
import tempfile
import time
import threading
import urllib.request
from typing import Dict, List, Optional
from urllib.parse import quote as _quote

import feedparser
from bs4 import BeautifulSoup

from cache import CACHE

# UA generico: evita il mismatch fingerprint-TLS che alcune CDN (es. Cloudflare)
# rifiutano quando si usa uno UA da Chrome pieno con il TLS di curl.
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
    "Connection": "keep-alive",
}

# Squadre canoniche Serie A1 maschile + keyword di riconoscimento nei testi
TEAMS: List[Dict] = [
    {"name": "Pro Recco", "short": "Recco", "kw": ["pro recco", "recco"]},
    {"name": "Brescia", "short": "Brescia", "kw": ["brescia"]},
    {"name": "Savona", "short": "Savona", "kw": ["savona"]},
    {"name": "Posillipo", "short": "Posillipo", "kw": ["posillipo"]},
    {"name": "Ortigia", "short": "Ortigia", "kw": ["ortigia"]},
    {"name": "Trieste", "short": "Trieste", "kw": ["trieste"]},
    {"name": "Telimar", "short": "Telimar", "kw": ["telimar", "palermo"]},
    {"name": "Quinto", "short": "Quinto", "kw": ["quinto"]},
    {"name": "Lazio", "short": "Lazio", "kw": ["lazio"]},
    {"name": "Chiavari", "short": "Chiavari", "kw": ["chiavari"]},
    {"name": "De Akker", "short": "De Akker", "kw": ["de akker", "akker"]},
    {"name": "Olympic Roma", "short": "Olympic", "kw": ["olympic", "academy roma", "training"]},
    {"name": "Canottieri Napoli", "short": "Canottieri", "kw": ["canottieri", "napoli"]},
    {"name": "Roma Vis Nova", "short": "Vis Nova", "kw": ["vis nova"]},
]
TEAM_KEYWORDS = [(t["name"], k) for t in TEAMS for k in t["kw"]]

WPD_A1M_FEED = "https://www.wpdworld.com/category/news/a1-m/feed/"
WPD_A1F_FEED = "https://www.wpdworld.com/category/news/a1-f/feed/"
WPD_ALL_FEED = "https://www.wpdworld.com/feed/"

RECCO_REST = "https://prorecco.com/wp-json/wp/v2/"
# Categorie WordPress della Pro Recco per ruolo
RECCO_CATS = [
    (100, "Allenatore"),
    (101, "Portieri"),
    (102, "Difensori"),
    (103, "Centrovasca"),
    (104, "Attaccanti"),
    (105, "Centroboa"),
]

_session = None  # non più usato: il fetch passa da curl (più affidabile con Cloudflare)
_netlock = threading.Lock()
_CURL_BIN = "curl.exe" if os.name == "nt" else "curl"


class _Resp:
    """Minimal wrapper compatibile con l'API di requests usata nel modulo."""

    def __init__(self, content: bytes, status_code: int):
        self.content = content
        self.status_code = status_code

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", "replace")

    def json(self):
        return json.loads(self.content.decode("utf-8", "replace"))


def _urllib_get(url: str, timeout: int, referer: Optional[str] = None) -> Optional[_Resp]:
    """Fallback puro-Python quando curl non è disponibile (es. GitHub Actions)."""
    hdrs = dict(HEADERS)
    if referer:
        hdrs["Referer"] = referer
    req = urllib.request.Request(url, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            return _Resp(body, getattr(resp, "status", 200) or 200)
    except Exception as exc:  # noqa: BLE001
        print(f"[fetch] urllib {url} -> {exc}")
        return None


def http_get(
    url: str,
    timeout: int = 25,
    retries: int = 3,
    referer: Optional[str] = None,
) -> Optional[_Resp]:
    """Scarica una risorsa usando curl (portabile Windows/Linux), con fallback urllib."""
    last = None
    with _netlock:
        for attempt in range(retries):
            tmp = None
            try:
                fd, tmp = tempfile.mkstemp()
                os.close(fd)
                cmd = [
                    _CURL_BIN, "-s", "-L",
                    "--max-time", str(timeout),
                    "-A", UA,
                    "-H", "Accept: text/html,application/xhtml+xml,application/json,*/*;q=0.8",
                    "-H", "Accept-Language: it-IT,it;q=0.9,en;q=0.8",
                ]
                if referer:
                    cmd += ["-H", "Referer: " + referer]
                cmd += ["-o", tmp, "-w", "%{http_code}", url]
                proc = subprocess.run(cmd, capture_output=True)
                code = (proc.stdout or b"").decode("utf-8", "replace").strip()
                with open(tmp, "rb") as fh:
                    body = fh.read()
                if code == "200" and body:
                    return _Resp(body, 200)
                last = "HTTP " + (code or "ERR")
            except FileNotFoundError:
                # curl non installato (es. GitHub Actions): usa urllib
                r = _urllib_get(url, timeout, referer)
                if r is not None:
                    return r
                last = "curl/urllib assenti"
            except Exception as exc:  # noqa: BLE001
                last = str(exc)
            finally:
                if tmp and os.path.exists(tmp):
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
            time.sleep(1.5 * (attempt + 1))
    print(f"[fetch] {url} -> {last}")
    return None


def canonical_team(label: str) -> Optional[str]:
    if not label:
        return None
    low = label.lower()
    for name, kw in TEAM_KEYWORDS:
        if kw in low:
            return name
    return None


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _html_to_text(html: str, limit: int = 320) -> str:
    if not html:
        return ""
    text = BeautifulSoup(html, "html.parser").get_text(" ")
    # taglia i trailer tipici dei feed WordPress
    text = re.split(r"\bThe post\b", text)[0]
    text = re.split(r"\bLeggi di più\b", text)[0]
    text = re.split(r"\bContinue reading\b", text)[0]
    text = _clean(text)
    if limit and len(text) > limit:
        text = text[: limit - 3].rsplit(" ", 1)[0] + "..."
    return text


# --------------------------------------------------------------------------- #
#  News feeds
# --------------------------------------------------------------------------- #
def _parse_feed_entry(entry) -> dict:
    title = _clean(getattr(entry, "title", ""))
    link = getattr(entry, "link", "")
    published = ""
    for key in ("published", "updated"):
        val = getattr(entry, key, None)
        if val:
            published = val
            break
    summary = _html_to_text(getattr(entry, "summary", ""))
    source = ""
    if " - " in title:
        parts = title.rsplit(" - ", 1)
        if len(parts[1]) < 45:
            title, source = parts[0].strip(), parts[1].strip()
    return {
        "title": title,
        "link": link,
        "date": published,
        "summary": summary,
        "source": source or "wpdworld.com",
        "image": "",
        "tags": [],
        "teams": _teams_in(title + " " + summary),
    }


def _teams_in(text: str) -> List[str]:
    low = (text or "").lower()
    return [t["name"] for t in TEAMS if any(k in low for k in t["kw"])]


def fetch_wpd_feed(url: str, limit: int = 25) -> List[dict]:
    r = http_get(url)
    if not r:
        return []
    feed = feedparser.parse(r.content)
    items = []
    for entry in feed.entries[:limit]:
        items.append(_parse_feed_entry(entry))
    return items


def fetch_google_news(query: str, limit: int = 15) -> List[dict]:
    q = _quote(query)
    url = (
        "https://news.google.com/rss/search?q={}&hl=it&gl=IT&ceid=IT:it".format(q)
    )
    r = http_get(url)
    if not r:
        return []
    feed = feedparser.parse(r.content)
    items = []
    for entry in feed.entries[:limit]:
        items.append(_parse_feed_entry(entry))
    return items


def fetch_prorecco_news(limit: int = 15) -> List[dict]:
    url = RECCO_REST + "posts?per_page={}&_embed=1".format(limit + 5)
    r = http_get(url, referer="https://prorecco.com/")
    if not r:
        raise RuntimeError("prorecco.com non raggiungibile")
    try:
        data = r.json()
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("risposta Pro Recco non valida: %s" % exc)
    out = []
    for post in data[:limit]:
        image = ""
        try:
            image = post["_embedded"]["wp:featuredmedia"][0]["source_url"]
        except Exception:  # noqa: BLE001
            image = ""
        excerpt = _clean(BeautifulSoup(post.get("excerpt", {}).get("rendered", ""), "html.parser").get_text(" "))
        if len(excerpt) > 320:
            excerpt = excerpt[:317].rsplit(" ", 1)[0] + "..."
        out.append(
            {
                "title": _clean(post.get("title", {}).get("rendered", "")),
                "link": post.get("link", ""),
                "date": post.get("date", ""),
                "summary": excerpt,
                "source": "prorecco.com",
                "image": image,
                "tags": ["Pro Recco"],
                "teams": ["Pro Recco"],
            }
        )
    return out


def fetch_prorecco_roster() -> dict:
    roster = {}
    ok = 0
    for cat_id, role in RECCO_CATS:
        url = RECCO_REST + "posts?categories={}&per_page=30&_fields=title".format(cat_id)
        r = http_get(url, referer="https://prorecco.com/")
        players = []
        if r:
            try:
                for p in r.json():
                    name = _clean(p.get("title", {}).get("rendered", ""))
                    if name and (name.islower() or name.isupper()):
                        name = name.title()
                    if name:
                        players.append(name)
                ok += 1
            except Exception:  # noqa: BLE001
                pass
        roster[role] = players
        time.sleep(1.0)  # pausa tra richieste successive allo stesso host
    if ok == 0:
        raise RuntimeError("prorecco.com non raggiungibile (rosa)")
    return roster


# --------------------------------------------------------------------------- #
#  Giornata (risultati, tabellini, classifica) da wpdworld
# --------------------------------------------------------------------------- #
MATCH_RE = re.compile(
    r"^(?P<home>[A-Za-zÀ-ÖØ-öø-ÿ0-9.' ]+?)\s*[-–]\s*"
    r"(?P<away>[A-Za-zÀ-ÖØ-öø-ÿ0-9.' ]+?)\s+"
    r"(?P<hs>\d{1,2})\s*[-–]\s*(?P<as>\d{1,2})"
    r"(?:\s*\((?P<partials>[^)]*)\))?\s*$"
)
GIORNATA_RE = re.compile(r"(\d+)\s*(?:ª|a|ª|ᵃ)?\s*giornata", re.I)
NOISE_LABELS = {"arbitri", "note", "note varie", "arbitri e note", "allenatore"}

_NOISE_TITLE = (
    "presentazione",
    "programma",
    "diretta",
    "streaming",
    "arbitri",
    "schedina",
    "pronostic",
)


def _looks_like_giornata(title: str, link: str = "") -> bool:
    """Riconosce gli articoli-recap di giornata (risultati + tabellini + classifica)."""
    t = (title or "").lower()
    l = (link or "").lower()
    blob = t + " " + l

    # escludi anteprime / programmi / streaming / direct
    if any(n in blob for n in _NOISE_TITLE):
        return False

    # segnale forte: slug tipo a1-m-2a-giornata-2026-27-risultati-tabellini-classifica
    if "giornata" in l and ("risultati" in l or "tabellini" in l or "classific" in l):
        return True
    if "risultati" in l or "tabellini" in l:
        return True

    # fallback sul titolo
    if "giornata" in t and any(k in t for k in ("risultat", "tabellin", "classifica")):
        return True
    if re.search(r"\d{1,2}\s*[-–]\s*\d{1,2}", t) and "giornata" in t:
        return True
    return False


def _parse_standings(table) -> List[dict]:
    header = [th.get_text(" ", strip=True) for th in table.find_all("th")]
    body = table.find("tbody") or table
    rows = []
    for tr in body.find_all("tr"):
        cells = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
        if len(cells) < 10:
            continue
        try:
            rows.append(
                {
                    "pos": cells[0].rstrip("."),
                    "team": cells[1],
                    "g": cells[2],
                    "pts": cells[3],
                    "v": cells[4],
                    "vr": cells[5],
                    "p": cells[6],
                    "pr": cells[7],
                    "gf": cells[8],
                    "gs": cells[9],
                    "canon": canonical_team(cells[1]) or cells[1],
                }
            )
        except Exception:  # noqa: BLE001
            continue
    if not rows:
        return []
    return rows


def _parse_tabellino(label: str, body: str) -> List[dict]:
    body = re.split(r"\bAll\.", body)[0]
    body = re.split(r"\bAllen\.", body)[0]
    team = canonical_team(label)
    out = []
    for chunk in body.split(","):
        token = _clean(chunk).strip(" .;,")
        if not token or len(token) < 2:
            continue
        m = re.match(r"^(?P<name>.+?)\s+(?P<g>\d{1,2})$", token)
        if m:
            name, goals = _clean(m.group("name")), int(m.group("g"))
        else:
            name, goals = token, 0
        if not name or name.lower().startswith("all"):
            continue
        out.append({"player": name, "goals": goals, "team": team, "team_label": label})
    return out


_EXP_MARKER = "limite di falli"
_TIME_RE = re.compile(r"(?:nel|in|all['’]?inizio|ad\s+inizio)\s+\w+\s+(?:tempo|quarto)", re.I)
_EXP_STOP = re.compile(
    r"superiorit|spettator|espulso per proteste|subentra|fallisce|para un rigore|"
    r"rimont|in porta|parate|\bnumerich",
    re.I,
)


def _resolve_team(code, labels):
    """Risolve una sigla squadra ('R','B',...) su una delle etichette del match."""
    if not code:
        return None, None
    code = code.upper()
    hits = [
        lbl for lbl in labels
        if code in {w[0].upper() for w in re.findall(r"[A-Za-zÀ-ÿ]+", lbl)}
    ]
    if len(hits) == 1:
        return hits[0], canonical_team(hits[0])
    hits2 = [lbl for lbl in labels if (canonical_team(lbl) or lbl)[:1].upper() == code]
    if len(hits2) == 1:
        return hits2[0], canonical_team(hits2[0])
    return None, None


def _parse_expulsions(text: str, labels: List[str]) -> List[dict]:
    """Estrae gli 'usciti per limite di falli' da una nota, associandoli alle squadre."""
    low = text.lower()
    if _EXP_MARKER not in low:
        return []
    rest = text[low.find(_EXP_MARKER) + len(_EXP_MARKER):]
    rest = rest.lstrip(": ").strip()
    stop = _EXP_STOP.search(rest)
    if stop:
        rest = rest[: stop.start()]
    out = []
    for seg in _TIME_RE.split(rest):
        seg = seg.strip(" .;,")
        if not seg:
            continue
        for part in re.split(r"\s*,\s*|\s+e\s+", seg):
            part = _clean(re.sub(r"^e\s+", "", part.strip(" .;,")))
            if not part:
                continue
            m = re.match(r"^(?P<name>.+?)\s*\((?P<code>[A-Za-z])\)$", part)
            if m:
                name, code = _clean(m.group("name")), m.group("code")
            else:
                name, code = part, None
            name = name.strip(" .;,")
            if not name or name.lower() in NOISE_LABELS:
                continue
            if re.search(r"\d|/|numerich|superior|spettator|allen|tecnico", name, re.I):
                continue
            team_label, team = _resolve_team(code, labels)
            out.append({"player": name, "team_code": code, "team_label": team_label, "team": team})
    return out


def parse_giornata(html: str, link: str, title: str, date: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    content = soup.select_one(".the_content_wrapper") or soup

    standings_rows = []
    table = content.find("table", class_=lambda c: c and "tablepress" in str(c))
    if table:
        standings_rows = _parse_standings(table)

    matches = []
    players = []
    expulsions = []
    labels: List[str] = []

    for p in content.find_all("p"):
        text = _clean(p.get_text(" ", strip=True))
        if not text or len(text) < 5:
            continue

        low = text.lower()

        # Note di partita -> espulsioni (associate alle ultime due squadre viste)
        if low.startswith("note") and _EXP_MARKER in low:
            expulsions.extend(_parse_expulsions(text, labels[-2:]))
            continue

        m = MATCH_RE.match(text)
        if m:
            home_raw = _clean(m.group("home"))
            away_raw = _clean(m.group("away"))
            if home_raw.lower() in NOISE_LABELS or away_raw.lower() in NOISE_LABELS:
                continue
            hs, as_ = int(m.group("hs")), int(m.group("as"))
            partials = _clean(m.group("partials") or "")
            partial_list = [x.strip() for x in partials.split(",") if x.strip()]
            matches.append(
                {
                    "home": canonical_team(home_raw) or home_raw,
                    "away": canonical_team(away_raw) or away_raw,
                    "home_label": home_raw,
                    "away_label": away_raw,
                    "home_score": hs,
                    "away_score": as_,
                    "partials": partial_list,
                }
            )
            continue

        # Tabellino: "<strong>Team:</strong> giocatore 1, giocatore 2 ..."
        if ":" in text and "," in text:
            label, _, rest = text.partition(":")
            label = _clean(label)
            if label.lower() in NOISE_LABELS:
                continue
            if len(label) > 60:
                continue
            if canonical_team(label) is None and not re.search(r"[A-Za-z]", label):
                continue
            parsed = _parse_tabellino(label, rest)
            if parsed:
                players.extend(parsed)
                labels.append(label)

    numero = GIORNATA_RE.search(title) or GIORNATA_RE.search(html or "")
    return {
        "title": _clean(title),
        "link": link,
        "date": date,
        "giornata": numero.group(1) if numero else None,
        "matches": matches,
        "standings": standings_rows,
        "players": players,
        "expulsions": expulsions,
    }


def fetch_giornate(max_articles: int = 8) -> dict:
    """Scarica e parsifica gli ultimi articoli con risultati/tabellini A1M."""
    r = http_get(WPD_A1M_FEED)
    if not r:
        cached = CACHE.get("giornate")
        if cached:
            return cached
        return {"updated": None, "giornate": []}

    feed = feedparser.parse(r.content)
    candidates = []
    for entry in feed.entries:
        title = _clean(getattr(entry, "title", ""))
        link = getattr(entry, "link", "")
        date = getattr(entry, "published", "") or getattr(entry, "updated", "")
        if _looks_like_giornata(title, link):
            candidates.append((title, link, date))

    # Ordina per data decrescente (i feed sono già ordinati, ma assicuriamoci)
    parsed = []
    for title, link, date in candidates[:max_articles]:
        cache_key = "giornata::" + link
        data = CACHE.get(cache_key)
        if data is None:
            rr = http_get(link)
            if not rr:
                continue
            data = parse_giornata(rr.text, link, title, date)
            CACHE.set(cache_key, data)
        parsed.append(data)

    parsed.sort(key=lambda g: (g.get("date") or ""), reverse=True)
    result = {"updated": time.time(), "giornate": parsed}
    CACHE.set("giornate", result)
    return result


# --------------------------------------------------------------------------- #
#  Derivati (live, classifica, marcatori)
# --------------------------------------------------------------------------- #
def build_live(giornate: dict) -> dict:
    gs = giornate.get("giornate") or []
    if not gs:
        return {"updated": giornate.get("updated"), "matches": [], "source": None, "label": None}
    latest = gs[0]
    return {
        "updated": giornate.get("updated"),
        "source": latest.get("link"),
        "label": latest.get("title"),
        "date": latest.get("date"),
        "giornata": latest.get("giornata"),
        "matches": latest.get("matches", []),
    }


def build_standings(giornate: dict) -> dict:
    gs = giornate.get("giornate") or []
    for g in gs:
        if g.get("standings"):
            return {
                "updated": g.get("date"),
                "source": g.get("link"),
                "label": g.get("title"),
                "rows": g.get("standings"),
            }
    return {"updated": None, "source": None, "label": None, "rows": []}


def build_scorers(giornate: dict) -> dict:
    agg: Dict[str, dict] = {}
    for g in giornate.get("giornate", []):
        glabel = g.get("giornata")
        for pl in g.get("players", []):
            key = (pl["player"].lower(), pl.get("team"))
            cur = agg.get(key)
            if cur is None:
                cur = {
                    "player": pl["player"],
                    "team": pl.get("team"),
                    "team_label": pl.get("team_label"),
                    "goals": 0,
                    "apps": 0,
                    "matches": [],
                }
                agg[key] = cur
            cur["goals"] += pl.get("goals", 0)
            cur["apps"] += 1
            cur["matches"].append(
                {
                    "giornata": glabel,
                    "goals": pl.get("goals", 0),
                    "link": g.get("link"),
                    "title": g.get("title"),
                }
            )
    scorers = sorted(agg.values(), key=lambda x: (-x["goals"], x["player"].lower()))
    return {"updated": giornate.get("updated"), "scorers": scorers}


def search_player(giornate: dict, name: str) -> dict:
    if not name:
        return {"query": name, "total": 0, "entries": [], "news": []}
    q = name.lower()
    found: Dict[str, dict] = {}
    for g in giornate.get("giornate", []):
        for pl in g.get("players", []):
            low = pl["player"].lower()
            if q in low or low in q or _fuzzy_name(low, q):
                key = (pl["player"].lower(), pl.get("team"))
                cur = found.get(key)
                if cur is None:
                    cur = {
                        "player": pl["player"],
                        "team": pl.get("team"),
                        "team_label": pl.get("team_label"),
                        "goals": 0,
                        "matches": [],
                    }
                    found[key] = cur
                cur["goals"] += pl.get("goals", 0)
                cur["matches"].append(
                    {
                        "giornata": g.get("giornata"),
                        "goals": pl.get("goals", 0),
                        "title": g.get("title"),
                        "link": g.get("link"),
                    }
                )
    entries = sorted(found.values(), key=lambda x: -x["goals"])
    total = sum(e["goals"] for e in entries)
    news = fetch_google_news("{} pallanuoto".format(name), limit=12)
    return {"query": name, "total": total, "entries": entries, "news": news}


def _fuzzy_name(a: str, b: str) -> bool:
    """Confronto tollerante: iniziale + cognome, o sottostringa sensata."""
    if a == b:
        return True
    ta = a.replace(".", "").split()
    tb = b.replace(".", "").split()
    if len(ta) < 2 or len(tb) < 2:
        return False
    if ta[-1] == tb[-1] and (ta[0][:1] == tb[0][:1] or ta[0][:3] == tb[0][:3]):
        return True
    return False


def build_team_news(team: str, limit: int = 20) -> List[dict]:
    items = fetch_google_news("{} pallanuoto".format(team), limit=limit)
    if team.lower() == "pro recco":
        items = fetch_prorecco_news(limit=limit) + items
    # de-dup per titolo
    seen, out = set(), []
    for it in items:
        key = it["title"].lower()[:60]
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out[:limit]
