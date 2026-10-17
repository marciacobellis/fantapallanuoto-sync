"""Sincronizza i dati reali della Serie A1 (gol, espulsioni, voti) nel database
FantaPallanuoto su Supabase.

Flusso:
 1. legge i giocatori dal DB (tabella `giocatori`);
 2. legge i tabellini reali (wpdworld) con gol e espulsioni ("usciti per limite di falli");
 3. abbina i nomi reali ai giocatori del fantacalcio;
 4. accumula lo storico per giornata (file locale fanta_history.json);
 5. calcola voto di giornata, ultimo voto e media voto;
 6. aggiorna Supabase (gol, espulsioni, ultimo_voto, media_voto).

Uso:
    python fanta.py            # sincronizza una volta
    python fanta.py --loop     # sincronizza ogni N minuti (loop)
    python fanta.py --dry-run  # mostra cosa farebbe, senza scrivere
"""

import argparse
import json
import os
import re
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from typing import Dict, List, Optional

import sources

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

_SYNC_LOCK = threading.Lock()

SUPABASE_URL = "https://kyayztcemfzvymhospew.supabase.co"
SUPABASE_KEY = "sb_publishable_iFDGFRxemZDbvrA8sWfd7A_vHfVkCHr"
TABLE = "giocatori"

# ---- formula voto (modificabile) ----
VOTO_BASE = 6.0     # voto base a partita
VOTO_GOL = 1.0      # bonus per gol
VOTO_ESP = 0.5      # malus per espulsione
VOTO_MIN = 1.0
VOTO_MAX = 10.0

HISTORY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fanta_history.json")


# --------------------------------------------------------------------------- #
#  Supabase (Data API REST via urllib)
# --------------------------------------------------------------------------- #
def _sb_request(method: str, path: str, body=None, prefer: Optional[str] = None, timeout: int = 40):
    url = SUPABASE_URL + "/rest/v1/" + path
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": "Bearer " + SUPABASE_KEY,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError:
        return None


def fetch_players() -> List[dict]:
    cols = "id,nome,ruolo,squadra_reale,calotta,parate,quotazione,ultimo_voto,media_voto,gol,espulsioni"
    return _sb_request("GET", "{t}?select={c}&order=id".format(t=TABLE, c=cols)) or []


def update_player(pid: int, fields: dict) -> None:
    _sb_request("PATCH", "{t}?id=eq.{p}".format(t=TABLE, p=pid), body=fields, prefer="return=minimal")


# --------------------------------------------------------------------------- #
#  Matching nomi (tabellino -> giocatore fantacalcio)
# --------------------------------------------------------------------------- #
def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower()
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _tokens(text: str) -> List[str]:
    return _norm(text).split()


def nome_match(db_nome: str, real: str) -> bool:
    """True se un nome reale (es. 'N. Presciutti', 'Iocchi Gratta') è il giocatore db_nome."""
    dn, rt = _tokens(db_nome), _tokens(real)
    if not dn or not rt:
        return False
    db_full, real_full = " ".join(dn), " ".join(rt)
    if real_full in db_full or db_full in real_full:
        return True
    if dn[-1] == rt[-1]:
        if len(rt) == 1:
            return True
        if len(rt[0]) == 1 and dn[0][:1] == rt[0]:
            return True
    return False


def _db_team(player: dict) -> Optional[str]:
    return sources.canonical_team(player.get("squadra_reale") or "")


def player_matches(player: dict, real: str, real_team: Optional[str]) -> bool:
    """Match su nome + squadra (per evitare omonimi di squadre diverse)."""
    if real_team:
        team = _db_team(player)
        if not team or team != real_team:
            return False
    return nome_match(player["nome"], real)


# --------------------------------------------------------------------------- #
#  Storico per giornata
# --------------------------------------------------------------------------- #
def load_history() -> dict:
    if os.path.exists(HISTORY_PATH):
        try:
            with open(HISTORY_PATH, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            pass
    return {"giornate": {}}


def save_history(hist: dict) -> None:
    tmp = HISTORY_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(hist, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, HISTORY_PATH)


def _giornata_stats(g: dict, players: List[dict]) -> Dict[str, dict]:
    stats: Dict[str, dict] = {}

    def touch(pid):
        return stats.setdefault(str(pid), {"gol": 0, "esp": 0, "played": False})

    for pl in g.get("players", []):
        for p in players:
            if player_matches(p, pl.get("player", ""), pl.get("team")):
                s = touch(p["id"])
                s["played"] = True
                s["gol"] += int(pl.get("goals") or 0)
    for ex in g.get("expulsions", []):
        for p in players:
            if player_matches(p, ex.get("player", ""), ex.get("team")):
                s = touch(p["id"])
                s["played"] = True
                s["esp"] += 1
    return stats


def _aggregate(hist: dict) -> Dict[str, dict]:
    per: Dict[str, list] = {}
    for gnum, pmap in hist.get("giornate", {}).items():
        try:
            gn = int(gnum)
        except (TypeError, ValueError):
            continue
        for pid, s in pmap.items():
            if not s.get("played"):
                continue
            per.setdefault(pid, []).append((gn, int(s.get("gol") or 0), int(s.get("esp") or 0)))

    result: Dict[str, dict] = {}
    for pid, rows in per.items():
        rows.sort(key=lambda r: r[0])
        voti = []
        for _gn, gol, esp in rows:
            v = VOTO_BASE + VOTO_GOL * gol - VOTO_ESP * esp
            voti.append(max(VOTO_MIN, min(VOTO_MAX, v)))
        result[pid] = {
            "gol": sum(r[1] for r in rows),
            "espulsioni": sum(r[2] for r in rows),
            "media_voto": round(sum(voti) / len(voti), 1),
            "ultimo_voto": round(voti[-1], 1),
            "giornate": len(rows),
        }
    return result


# --------------------------------------------------------------------------- #
#  Sync
# --------------------------------------------------------------------------- #
def sync(dry_run: bool = False, reset_missing: bool = False) -> None:
    with _SYNC_LOCK:
        _sync(dry_run=dry_run, reset_missing=reset_missing)


def _sync(dry_run: bool = False, reset_missing: bool = False) -> None:
    players = fetch_players()
    if not players:
        print("[fanta] nessun giocatore letto da Supabase")
        return

    giornate = sources.fetch_giornate(max_articles=12)
    gs = giornate.get("giornate") or []
    print("[fanta] giocatori db: {n} | giornate trovate: {g}".format(n=len(players), g=len(gs)))

    hist = load_history()
    for g in gs:
        gn = g.get("giornata")
        if gn is None:
            continue
        hist["giornate"][str(gn)] = _giornata_stats(g, players)
    if not dry_run:
        save_history(hist)
    print("[fanta] giornate in storico: {k} ({v})".format(
        k=len(hist["giornate"]), v=",".join(sorted(hist["giornate"], key=lambda x: int(x)))))

    agg = _aggregate(hist)
    by_id = {str(p["id"]): p for p in players}
    updated, missing = 0, []
    for pid_str, p in by_id.items():
        data = agg.get(pid_str)
        if data is None:
            missing.append(p["nome"])
            continue
        fields = {
            "gol": data["gol"],
            "espulsioni": data["espulsioni"],
            "ultimo_voto": data["ultimo_voto"],
            "media_voto": data["media_voto"],
        }
        print("  {nome:<24} gol:{gol:<3} esp:{esp:<3} uv:{uv:<4} mv:{mv}  ({nj} gare)".format(
            nome=p["nome"][:24], gol=fields["gol"], esp=fields["espulsioni"],
            uv=fields["ultimo_voto"], mv=fields["media_voto"], nj=data["giornate"]))
        if not dry_run:
            try:
                update_player(int(pid_str), fields)
                updated += 1
            except urllib.error.HTTPError as exc:
                print("    ! errore update {n}: HTTP {c}".format(n=p["nome"], c=exc.code))

    for p in players:
        if str(p["id"]) in agg:
            continue
        if reset_missing and not dry_run:
            update_player(p["id"], {"gol": 0, "espulsioni": 0})
    if missing:
        print("[fanta] senza dati A1 (non aggiornati): " + ", ".join(missing))
    print("[fanta] aggiornati: {u}/{t}{d}".format(u=updated, t=len(players), d="  (DRY-RUN)" if dry_run else ""))


def main() -> None:
    ap = argparse.ArgumentParser(description="Sync dati reali A1 -> FantaPallanuoto")
    ap.add_argument("--loop", action="store_true", help="ripeti ogni N minuti")
    ap.add_argument("--every", type=int, default=10, help="minuti tra un sync e l'altro (default 10)")
    ap.add_argument("--dry-run", action="store_true", help="mostra senza scrivere")
    ap.add_argument("--reset-missing", action="store_true", help="azzera i giocatori senza dati A1")
    args = ap.parse_args()

    if not args.loop:
        try:
            sync(dry_run=args.dry_run, reset_missing=args.reset_missing)
        except Exception as exc:  # noqa: BLE001
            print("[fanta] errore: {e}".format(e=exc))
        return

    print("[fanta] loop attivo, ogni {n} min. CTRL+C per fermare.".format(n=args.every))
    while True:
        try:
            sync(dry_run=args.dry_run, reset_missing=args.reset_missing)
        except Exception as exc:  # noqa: BLE001
            print("[fanta] errore: {e}".format(e=exc))
        time.sleep(max(60, args.every * 60))


if __name__ == "__main__":
    main()
