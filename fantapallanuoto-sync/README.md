# FantaPallanuoto — Sync automatico (cloud, senza PC)

Questo repository aggiorna **da solo**, ogni 30 minuti, i dati reali della Serie A1
(gol, espulsioni, ultimo voto, media voto) nel database Supabase di FantaPallanuoto.

Gira su **GitHub Actions** (gratis): non serve tenere acceso il PC e non serve
il server locale.

## Come funziona

- `fanta.py` legge i tabellini reali da wpdworld, li abbina ai giocatori su Supabase
  e aggiorna gol / espulsioni / ultimo_voto / media_voto.
- `fanta_history.json` conserva lo storico per giornata (viene risalvato nel repo
  a ogni esecuzione utile, così i dati si accumulano nel tempo).
- `.github/workflows/sync.yml` esegue `python fanta.py` ogni 30 minuti
  (e anche a mano con "Run workflow").

## Setup (una volta sola)

1. Crea un repository nuovo su GitHub, ad esempio `fantapallanuoto-sync`.
2. Da questa cartella (`D:\Desktop\fantapallanuoto-sync`) esegui:

   ```
   git init
   git add .
   git commit -m "sync fantapallanuoto"
   git branch -M main
   git remote add origin https://github.com/TUO-UTENTE/fantapallanuoto-sync.git
   git push -u origin main
   ```

3. Vai nella scheda **Actions** del repository e abilita i workflow.

Fatto. Da quel momento il database Supabase si aggiorna da solo, senza PC acceso.

## Note

- Frequenza: ogni 30 minuti (GitHub può ritardarla di qualche minuto).
- GitHub disattiva i workflow programmati dopo 60 giorni di inattività del repo:
  qui il workflow riscrive `last_run.txt` (e lo committa) a ogni esecuzione, quindi
  il repo resta attivo.
- Il file `fanta.py` contiene solo la *publishable key* di Supabase, che è pubblica
  (è la stessa già usata dall'app), quindi non è un segreto.
- La formula del voto è in cima a `fanta.py`: base 6.0, +1.0 per gol, −0.5 per espulsione.

## Eseguire a mano (opzionale, dal PC)

```
python fanta.py            # una sincronizzazione
python fanta.py --dry-run  # mostra senza scrivere
python fanta.py --loop     # in loop continuo
```
