# svingen-sperrungen

Öffentliche, aufbereitete Straßensperrungen für die Android-App **Svingen**.

- Quelle: Statens vegvesen, DATEX II (Situationen), Lizenz NLOD 2.0
- `scripts/build_sperrungen.py` wandelt den Feed in `sperrungen.json` um (nur Sperrungen, Wintersperren, Gewichtsgrenzen, Fahrstreifen, Hindernisse).
- Veröffentlichung: Branch `gh-pages` (wird alle 15 Minuten ohne Verlauf überschrieben).
- Zugangsdaten liegen ausschließlich als Repository-Secrets (`DATEX_USER`, `DATEX_PASS`), nie im Code.
- Alle Angaben ohne Gewähr.
