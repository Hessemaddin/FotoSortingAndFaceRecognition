"""
PERSÖNLICHE EINSTELLUNGEN - VORLAGE
===================================

So benutzen:
    1. Diese Datei kopieren und die Kopie "einstellungen_privat.py" nennen
       (im selben Ordner wie die Programme).
    2. In der Kopie die eigenen Namen und Geburtsdaten eintragen.

einstellungen_privat.py wird über .gitignore von GitHub ausgeschlossen und
bleibt nur auf dem eigenen Rechner. Diese Vorlage enthält nur Beispielwerte.
"""

from datetime import datetime

# Personen = Ordnernamen in "referenzfotos" (Groß-/Kleinschreibung beachten).
# Reihenfolge = Spalten der Ergebnistabelle foto_tags_tabelle.txt.
TABELLEN_PERSONEN = ["Anna", "Ben", "Clara", "David"]

# Geburtsdaten: Für Fotos von VOR diesem Datum wird die Person nicht berücksichtigt.
# Nur für Personen eintragen, bei denen das sinnvoll ist (z.B. Kinder).
GEBURTSDATEN = {
    "Anna": datetime(2020, 1, 31),
}

# Nur für auswertung_erkennung.py: Name im Programm -> Name in der Excel-Tabelle,
# falls sie sich unterscheiden (z.B. Spitzname in der Tabelle).
NAMENS_ZUORDNUNG = {"Clara": "Cla"}
