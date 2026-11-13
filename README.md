# Fotoverwaltung – lokal und privat

Eigene Python-Programme, um Familienfotos **ohne Cloud** zu verwalten: Personen
per Gesichtserkennung erkennen (komplett offline), fehlende Aufnahmedaten
nachtragen und alle Fotos nach `Jahr\Monat\Tag` sortieren.

> Dieses Repository enthält **nur Programmcode**. Fotos, Referenzfotos,
> Ergebnisdateien und persönliche Einstellungen bleiben auf dem eigenen Rechner
> (siehe `.gitignore`).

## Programme

| Datei | Zweck | Stand |
|---|---|---|
| `exif_datum_nachtragen.py` | Trägt fehlende Aufnahmedaten ins EXIF ein (aus Dateiname, Ordner, Änderungsdatum) | in Arbeit, noch nicht auf echten Daten getestet |
| `modell_insightface.py` | Modernes Gesichtserkennungs-Modell (SCRFD + ArcFace), wird vom Tagging-Programm benutzt | einsatzbereit |
| `auswertung_erkennung.py` | Vergleicht manuelle Angaben (Excel) mit dem Programmergebnis | veraltet: kennt das aktuelle Excel-Layout und Markierungen pro Person noch nicht |
| `einstellungen_privat_vorlage.py` | Vorlage für die persönlichen Einstellungen | – |
| `foto_pipeline_mit_gesichtserkennung.py` | Erkennt Personen auf Fotos, schreibt Tags (Personen, Datum) und Auswertungsdateien | **einsatzbereit** (Test: 550 von 562 Fotos komplett richtig) |
| `fotos_sortieren.py` | Sortiert Fotos/Videos nach Jahr\Monat\Tag, löst Duplikate auf | noch nicht auf echten Daten getestet |

## Einrichtung (Windows)

1. Python 3.14 installieren. Immer `python -m pip` statt `pip` verwenden.
2. Pakete installieren:
   ```
   python -m pip install onnxruntime pillow numpy openpyxl
   ```
   Nur für das alte Modell (`MODELL = "face_recognition"`) zusätzlich `face_recognition`
   (braucht cmake, Visual Studio Build Tools und Git).
3. InsightFace-Modelle (ca. 280 MB) herunterladen:
   `https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_l.zip`
   und so entpacken, dass `modelle\buffalo_l\det_10g.onnx` und
   `modelle\buffalo_l\w600k_r50.onnx` im Programmordner liegen.
   (Die Modelle sind laut InsightFace nur für nicht-kommerzielle Zwecke freigegeben.)
4. `einstellungen_privat_vorlage.py` kopieren, als `einstellungen_privat.py`
   speichern und eigene Namen/Geburtsdaten eintragen.
5. Referenzfotos anlegen: `referenzfotos\<Name>\*.jpg` – ein Ordner pro Person
   (Ordnername = Name aus `TABELLEN_PERSONEN`), mehrere Fotos aus verschiedenen
   Jahren, am besten nur eine Person pro Foto, Gesicht gut sichtbar.

## Benutzung

Empfohlene Reihenfolge:

```
python exif_datum_nachtragen.py "<Fotoordner>" --trockenlauf
python exif_datum_nachtragen.py "<Fotoordner>"
python foto_pipeline_mit_gesichtserkennung.py referenzfotos "<Fotoordner>"
```

Pfade mit Leerzeichen in Anführungszeichen, **ohne** Backslash vor dem
schließenden Anführungszeichen.

## Wichtige Einstellungen (oben in `foto_pipeline_mit_gesichtserkennung.py`)

| Einstellung | Empfehlung | Bedeutung |
|---|---|---|
| `MODELL` | `"insightface"` | `"face_recognition"` = altes Modell |
| `TOLERANZ_INSIGHTFACE` | `0.68` | gut zwischen 0.66 und 0.72 |
| `REFERENZ_MODUS` | `"gewichtet"` | Referenzfoto davor und danach, das zeitlich nähere zählt stärker |
| `REFERENZ_MEHRERE_GESICHTER` | `"groesstes"` | bei mehreren Gesichtern im Referenzfoto das deutlich größte nehmen |
| `TOLERANZ`, `NACHSUCHE` | `0.54`, `"immer"` | nur für das alte Modell |

## Testergebnisse (562 Fotos, manuell geprüft)

| Variante | komplett richtig | Zeit für 562 Fotos |
|---|---|---|
| altes Modell, Toleranz 0.6 | ca. 77 % (kleinere Stichprobe) | – |
| altes Modell, optimiert (Nachsuche, gewichtet, 0.54) | 505 (89,9 %) | ca. 7,5 Std. |
| **InsightFace, gewichtet, 0.68** | **550 (97,9 %)**, keine Falschmeldungen | **ca. 8 Min.** |

## Nächste Schritte

- `fotos_sortieren.py` auf dem echten Bestand testen (erst Trockenlauf).
- `exif_datum_nachtragen.py`: Datum aus Pfaden wie `…\2019\03\02\` lesen.
- `auswertung_erkennung.py` an das aktuelle Excel-Layout anpassen.
- Später: Umzug auf NAS + Mini-PC (Linux).
