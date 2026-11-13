"""
FOTO-PIPELINE MIT ECHTER GESICHTSERKENNUNG (zeitlich gewichtet)
==================================================================

Datumsermittlung mit Prioritäten:
    1. Echtes EXIF-Aufnahmedatum
    2. Datums-String im Dateinamen, z.B. "IMG-20170124-WA0000.jpg"
    3. Dateisystem-Änderungsdatum (letzter Rückfall)

Gesichtserkennung:
    - Zwei Modelle wählbar (MODELL): "face_recognition" (bisher) oder
      "insightface" (modern, siehe modell_insightface.py).
    - Pro Person wird das zeitlich nächstgelegene Referenzfoto herangezogen
      (einstellbar: REFERENZ_MODUS, auch Referenzfoto davor + danach).
    - Gesichtssuche in zwei Stufen (einstellbar: NACHSUCHE): erst die schnelle
      Suche wie bisher, dann bei Bedarf eine gründlichere ("cnn").
    - Geburtsdaten-Ausschluss: Personen werden für Fotos von vor ihrem
      Geburtsdatum nicht berücksichtigt.
    - Eine Person pro Gesicht, ein Gesicht pro Person: Innerhalb eines
      Fotos wird jede Person höchstens einem Gesicht zugeordnet, und
      jedes Gesicht höchstens einer Person. Gesucht wird die beste
      Gesamtzuordnung (nicht nur der beste Einzelwert).
    - Einstellbar (siehe EINSTELLUNGEN): Schwellenwert je Person, Mindestabstand
      zum zweitbesten Kandidaten, Mindestgröße eines Gesichts. Mit den Standard-
      werten verhält sich das Programm wie bisher.

Ausgabe (im Fotoordner):
    - foto_tags.json            (alle Details pro Foto)
    - foto_tags_tabelle.txt     (Tab-getrennte Tabelle: X = Treffer, - = kein Treffer)
    - foto_zeiten.txt           (Tab-getrennt: Berechnungszeiten der Referenzfotos
                                 und jedes einzelnen Fotos)
    - foto_abstaende.txt        (Tab-getrennt: pro gefundenem Gesicht Größe und Abstand
                                 zu jeder Person - Grundlage für auswertung_erkennung.py)
Ist eine dieser Dateien gesperrt (z.B. in Excel geöffnet), wird das Ergebnis unter
einem Ausweichnamen mit Uhrzeit gespeichert, statt abzubrechen.

Wie man es ausführt:
    python foto_pipeline_mit_gesichtserkennung.py referenzfotos meine_fotos [--debug]

Hinweis: Das Nachtragen fehlender EXIF-Daten (Datum aus dem Dateinamen) übernimmt das
separate Programm exif_datum_nachtragen.py - am besten VOR diesem Programm einmal
über den Fotobestand laufen lassen.
"""

import os
import re
import sys
import json
import time
from datetime import datetime

import numpy as np
from PIL import Image, ImageOps


# -----------------------------------------------------------------
# EINSTELLUNGEN
# -----------------------------------------------------------------
# Welches Gesichtserkennungs-Modell verwendet wird:
#   "face_recognition" = bisheriges Modell (dlib)
#   "insightface"      = modernes Modell (SCRFD + ArcFace). Braucht die Datei
#                        modell_insightface.py im selben Ordner, das Paket
#                        onnxruntime und die Modelldateien (siehe dort).
MODELL = "insightface"

# Toleranz für das bisherige Modell (face_recognition)
TOLERANZ = 0.54
# Toleranz für InsightFace - die Abstände haben dort eine andere Skala
# (1 - Kosinus-Ähnlichkeit). 0.68 ergab im Test mit 562 Fotos das beste
# Ergebnis (gut zwischen 0.66 und 0.72).
TOLERANZ_INSIGHTFACE = 0.68

# Eigene Schwellenwerte je Person (überschreiben TOLERANZ für diese Person).
# Beispiel: TOLERANZ_JE_PERSON = {"Anna": 0.52, "Ben": 0.62}
# Die Namen müssen den Ordnernamen in 'referenzfotos' entsprechen.
TOLERANZ_JE_PERSON = {}

# Mindestabstand zum zweitbesten Kandidaten: Liegen die beiden ähnlichsten
# Personen für ein Gesicht um weniger als diesen Wert auseinander, gilt das
# Gesicht als uneindeutig und wird KEINER Person zugeordnet. 0.0 = aus.
MIN_ABSTAND_ZUM_ZWEITBESTEN = 0.0

# Gesichter, die in dieser Höhe (Pixel) kleiner sind, werden keiner Person
# zugeordnet. 0 = aus.
MIN_GESICHTSHOEHE_PX = 0

# --- GESICHTSSUCHE (neu) ---
# Erste Suche, wie bisher: "hog" (schnell), 1x vergrößert.
GESICHTSSUCHE_MODELL = "hog"
GESICHTSSUCHE_UPSAMPLE = 1

# Zweite, gründlichere Suche mit einem anderen Verfahren ("cnn" findet auch
# schräge, seitliche und unscharfe Gesichter, ist aber deutlich langsamer).
#   "aus"           = keine zweite Suche (Verhalten wie bisher)
#   "kein_gesicht"  = nur bei Fotos, in denen die erste Suche nichts gefunden hat
#   "immer"         = bei jedem Foto; neue Gesichter kommen dazu, doppelt gefundene
#                     werden aussortiert (langsam, gut für einen Testlauf)
NACHSUCHE = "immer"   # wird bei MODELL = "insightface" nicht benutzt
NACHSUCHE_MODELL = "cnn"
NACHSUCHE_UPSAMPLE = 1
# Für die zweite Suche wird das Bild auf diese längste Kante verkleinert
# (Pixel), damit "cnn" ohne Grafikkarte nicht ewig rechnet. Größer = gründlicher,
# aber langsamer und speicherhungriger (1600 führte im Test zu MemoryError).
NACHSUCHE_MAX_KANTE = 1000
# Reicht der Arbeitsspeicher nicht, verkleinert das Programm schrittweise weiter,
# aber nicht unter diesen Wert; darunter wird die Nachsuche für das Foto übersprungen.
NACHSUCHE_MIN_KANTE = 400
# Ab diesem Überlappungsanteil gilt ein Gesicht der zweiten Suche als schon
# gefunden (0.3 = 30 % des kleineren Rahmens überlappen).
NACHSUCHE_UEBERLAPPUNG = 0.3

# --- REFERENZFOTOS (neu) ---
# Welche Referenzfotos pro Person verglichen werden:
#   "naechstes"  = nur das zeitlich nächstgelegene (Verhalten wie bisher)
#   "min"        = das letzte Referenzfoto VOR und das erste NACH dem Aufnahmedatum;
#                  es zählt der kleinere Abstand
#   "mittel"     = davor und danach; es zählt der Mittelwert
#   "gewichtet"  = davor und danach; das zeitlich nähere zählt stärker
# Unabhängig von dieser Einstellung schreibt das Programm die Abstände zu
# "davor" und "danach" immer in foto_abstaende.txt. Damit lassen sich alle
# Varianten nachträglich auswerten, ohne das Programm erneut laufen zu lassen.
REFERENZ_MODUS = "gewichtet"


# Referenzfoto mit mehreren Gesichtern:
#   "ueberspringen" = Foto nicht verwenden (bisheriges Verhalten)
#   "groesstes"     = das größte Gesicht verwenden, wenn es mindestens doppelt so
#                     groß ist wie das zweitgrößte (z.B. Person vorne, Leute im
#                     Hintergrund); sonst überspringen
REFERENZ_MEHRERE_GESICHTER = "groesstes"


def toleranz_fuer(person):
    standard = TOLERANZ_INSIGHTFACE if MODELL == "insightface" else TOLERANZ
    return TOLERANZ_JE_PERSON.get(person, standard)


# Modell laden: Beide Varianten bieten dieselben Funktionen
# (face_locations, face_encodings, face_distance).
if MODELL == "insightface":
    import modell_insightface as face_recognition
elif MODELL == "face_recognition":
    import face_recognition
else:
    raise SystemExit(f"Unbekanntes MODELL: {MODELL}")
# EXIF-Feldnummern, in dieser Reihenfolge wird nach einem Datum gesucht:
# 36867 = DateTimeOriginal (Aufnahmedatum), 36868 = DateTimeDigitized, 306 = DateTime
EXIF_DATUMSFELDER = (36867, 36868, 306)
EXIF_UNTERBEREICH = 0x8769  # Verweis auf den EXIF-Unterbereich (dort liegen 36867/36868)
DATEINAME_DATUM_REGEX = re.compile(r"(\d{8})")

# num_jitters: Wie oft ein Gesicht beim Berechnen des "Fingerabdrucks"
# leicht verändert und neu berechnet wird; aus allen Durchgängen wird
# der Mittelwert gebildet. Höhere Werte = genauer, aber proportional
# langsamer.
JITTERS_REFERENZ = 10
JITTERS_FOTOS = 1

# PERSÖNLICHE EINSTELLUNGEN (Namen, Geburtsdaten) stehen in einer eigenen Datei
# einstellungen_privat.py, die NICHT auf GitHub hochgeladen wird.
# Vorlage: einstellungen_privat_vorlage.py -> kopieren nach einstellungen_privat.py
try:
    from einstellungen_privat import TABELLEN_PERSONEN, GEBURTSDATEN
except ImportError:
    raise SystemExit(
        "FEHLER: Die Datei einstellungen_privat.py fehlt.\n"
        "Bitte einstellungen_privat_vorlage.py kopieren, in einstellungen_privat.py\n"
        "umbenennen und die eigenen Namen und Geburtsdaten eintragen.")

# Bis zu dieser Zahl an Gesichtern (mit mindestens einem Kandidaten)
# in einem Foto wird die optimale Zuordnung exakt berechnet.
MAX_GESICHTER_EXAKT = 12

# Dezimaltrennzeichen in der Zeitdatei. Komma passt zu einem deutschen
# Excel; für ein "." (z.B. für Programme/Skripte) hier ändern.
DEZIMALTRENNER = ","


# -----------------------------------------------------------------
# DATUM AUSLESEN: EXIF > Dateiname > Dateisystem
# -----------------------------------------------------------------
def lese_exif_datum(bildpfad):
    """Liest das EXIF-Datum (JPEG und PNG). Gibt None zurück, wenn keines vorhanden/gültig ist."""
    try:
        with Image.open(bildpfad) as bild:
            exif = bild.getexif()
            felder = dict(exif)
            felder.update(exif.get_ifd(EXIF_UNTERBEREICH))
    except Exception:
        return None

    for tag in EXIF_DATUMSFELDER:
        wert = felder.get(tag)
        if not wert:
            continue
        if isinstance(wert, bytes):
            wert = wert.decode("ascii", errors="ignore")
        try:
            return datetime.strptime(str(wert).strip("\x00 "), "%Y:%m:%d %H:%M:%S")
        except ValueError:
            continue  # z.B. "0000:00:00 00:00:00" zählt als kein Datum
    return None


def lese_datum_aus_dateiname(dateiname):
    treffer = DATEINAME_DATUM_REGEX.findall(dateiname)
    for ziffernfolge in treffer:
        try:
            datum = datetime.strptime(ziffernfolge, "%Y%m%d")
            if 1990 <= datum.year <= datetime.now().year:
                return datum
        except ValueError:
            continue
    return None


def lade_bild_korrekt_ausgerichtet(bildpfad):
    """Lädt ein Bild und korrigiert die Ausrichtung anhand des EXIF-Orientation-Tags."""
    bild = Image.open(bildpfad)
    bild = ImageOps.exif_transpose(bild)
    bild = bild.convert("RGB")
    return np.array(bild)


def lese_dateisystem_datum(bildpfad):
    zeitstempel = os.path.getmtime(bildpfad)
    return datetime.fromtimestamp(zeitstempel)


def lese_aufnahmedatum(bildpfad, debug=False):
    """Datum mit Priorität: EXIF > Dateiname > Dateisystem."""
    exif_datum = lese_exif_datum(bildpfad)
    if exif_datum is not None:
        if debug:
            print(f"    Datum-Quelle: EXIF ({exif_datum.strftime('%Y-%m-%d %H:%M')})")
        return exif_datum

    dateiname_datum = lese_datum_aus_dateiname(os.path.basename(bildpfad))
    if dateiname_datum is not None:
        if debug:
            print(f"    Datum-Quelle: Dateiname ({dateiname_datum.strftime('%Y-%m-%d')})")
        return dateiname_datum

    dateisystem_datum = lese_dateisystem_datum(bildpfad)
    if debug:
        print(f"    Datum-Quelle: Dateisystem, weder EXIF noch Dateiname verfügbar "
              f"({dateisystem_datum.strftime('%Y-%m-%d %H:%M')})")
    return dateisystem_datum


# -----------------------------------------------------------------
# SCHRITT A: REFERENZGESICHTER EINLESEN (inkl. Datum, Dateiname, Zeiten)
# -----------------------------------------------------------------
def lade_referenzgesichter(referenzordner, debug=False):
    """
    Rückgabe: (referenz_daten, referenz_zeiten)

    referenz_daten: {"Anna": [(datum, encoding, dateiname), ...], ...}
    referenz_zeiten: {"eintraege": [ {person, datei, gesamt, laden, gesichter, hinweis}, ... ],
                      "gesamt": Sekunden für den gesamten Ladevorgang}
    """
    start_gesamt = time.perf_counter()
    referenz_daten = {}
    zeiten_eintraege = []

    personen_ordner = [
        p for p in os.listdir(referenzordner)
        if os.path.isdir(os.path.join(referenzordner, p))
    ]

    for person in personen_ordner:
        ordner_pfad = os.path.join(referenzordner, person)
        bilddateien = [
            f for f in os.listdir(ordner_pfad)
            if f.lower().endswith((".jpg", ".jpeg", ".png"))
        ]

        print(f"Lade Referenzfotos für '{person}' ({len(bilddateien)} Bild(er))...")

        eintraege = []
        for bilddatei in bilddateien:
            t_start = time.perf_counter()

            bildpfad = os.path.join(ordner_pfad, bilddatei)
            bild = lade_bild_korrekt_ausgerichtet(bildpfad)
            t_geladen = time.perf_counter()

            orte_ref = face_recognition.face_locations(bild)
            hinweis_groesstes = ""
            if len(orte_ref) > 1 and REFERENZ_MEHRERE_GESICHTER == "groesstes":
                nach_groesse = sorted(orte_ref, key=lambda o: (o[2] - o[0]) * (o[1] - o[3]), reverse=True)
                f1 = (nach_groesse[0][2] - nach_groesse[0][0]) * (nach_groesse[0][1] - nach_groesse[0][3])
                f2 = (nach_groesse[1][2] - nach_groesse[1][0]) * (nach_groesse[1][1] - nach_groesse[1][3])
                if f1 >= 2 * f2:
                    orte_ref = nach_groesse[:1]
                    hinweis_groesstes = f"größtes von {len(nach_groesse)} Gesichtern verwendet"
            encodings_in_diesem_bild = face_recognition.face_encodings(
                bild, known_face_locations=orte_ref, num_jitters=JITTERS_REFERENZ
            )
            t_berechnet = time.perf_counter()

            hinweis = hinweis_groesstes
            if hinweis_groesstes:
                print(f"  HINWEIS: {bilddatei}: {hinweis_groesstes}.")
            if len(encodings_in_diesem_bild) == 0:
                print(f"  WARNUNG: Kein Gesicht gefunden in {bilddatei} - übersprungen.")
                hinweis = "übersprungen: kein Gesicht gefunden"
            elif len(encodings_in_diesem_bild) > 1:
                print(f"  WARNUNG: Mehrere Gesichter in {bilddatei} - übersprungen.")
                hinweis = "übersprungen: mehrere Gesichter"
            else:
                datum = lese_aufnahmedatum(bildpfad, debug=debug)
                eintraege.append((datum, encodings_in_diesem_bild[0], bilddatei))

            zeiten_eintraege.append({
                "person": person,
                "datei": bilddatei,
                "gesamt": time.perf_counter() - t_start,
                "laden": t_geladen - t_start,
                "gesichter": t_berechnet - t_geladen,
                "hinweis": hinweis,
            })

        referenz_daten[person] = eintraege

    gesamt_anzahl = sum(len(v) for v in referenz_daten.values())
    gesamtdauer = time.perf_counter() - start_gesamt
    print(f"\nInsgesamt {gesamt_anzahl} Referenz-Gesichter für "
          f"{len(referenz_daten)} Person(en) geladen ({gesamtdauer:.1f} s).\n")

    return referenz_daten, {"eintraege": zeiten_eintraege, "gesamt": gesamtdauer}


def waehle_naechstgelegenes_referenzfoto(eintraege, zieldatum):
    """Wählt aus (datum, encoding, dateiname)-Einträgen den zeitlich nächstgelegenen."""
    return min(eintraege, key=lambda eintrag: abs((eintrag[0] - zieldatum).days))


def waehle_referenz_davor_danach(eintraege, zieldatum):
    """
    Rückgabe: (davor, danach)
      davor:  das letzte Referenzfoto mit Datum <= Aufnahmedatum (oder None)
      danach: das erste Referenzfoto mit Datum >  Aufnahmedatum (oder None)
    """
    frueher = [e for e in eintraege if e[0] <= zieldatum]
    spaeter = [e for e in eintraege if e[0] > zieldatum]
    davor = max(frueher, key=lambda e: e[0]) if frueher else None
    danach = min(spaeter, key=lambda e: e[0]) if spaeter else None
    return davor, danach


def tage_zwischen(a, b):
    return abs((a - b).total_seconds()) / 86400


def kombiniere_abstaende(naechst, davor, danach):
    """
    Berechnet den Abstand, mit dem entschieden wird (je nach REFERENZ_MODUS).
    naechst: Abstand zum nächstgelegenen Referenzfoto
    davor / danach: (abstand, tage) oder None
    """
    if REFERENZ_MODUS == "naechstes":
        return naechst
    vorhanden = [x for x in (davor, danach) if x is not None]
    if len(vorhanden) == 1:
        return vorhanden[0][0]
    (a_davor, t_davor), (a_danach, t_danach) = davor, danach
    if REFERENZ_MODUS == "min":
        return min(a_davor, a_danach)
    if REFERENZ_MODUS == "mittel":
        return (a_davor + a_danach) / 2
    if REFERENZ_MODUS == "gewichtet":
        if t_davor + t_danach == 0:
            return (a_davor + a_danach) / 2
        # Je näher ein Referenzfoto zeitlich liegt, desto stärker zählt es.
        gewicht_davor = t_danach / (t_davor + t_danach)
        return gewicht_davor * a_davor + (1 - gewicht_davor) * a_danach
    raise ValueError(f"Unbekannter REFERENZ_MODUS: {REFERENZ_MODUS}")


# -----------------------------------------------------------------
# GESICHTSSUCHE: erste Suche + optionale zweite (gründlichere) Suche
# -----------------------------------------------------------------
def ueberlappungsanteil(a, b):
    """Anteil des kleineren Rahmens, der vom anderen überdeckt wird. Rahmen: (oben, rechts, unten, links)."""
    oben, unten = max(a[0], b[0]), min(a[2], b[2])
    links, rechts = max(a[3], b[3]), min(a[1], b[1])
    if unten <= oben or rechts <= links:
        return 0.0
    schnitt = (unten - oben) * (rechts - links)
    flaeche_a = (a[2] - a[0]) * (a[1] - a[3])
    flaeche_b = (b[2] - b[0]) * (b[1] - b[3])
    return schnitt / max(1, min(flaeche_a, flaeche_b))


def suche_gesichter_verkleinert(bild, debug=False):
    """
    Zweite Suche auf einer verkleinerten Kopie; Rahmen werden aufs Originalbild umgerechnet.
    Reicht der Arbeitsspeicher nicht (MemoryError), wird das Bild weiter verkleinert
    (bis NACHSUCHE_MIN_KANTE). Klappt es auch dann nicht, wird die Nachsuche für
    dieses Foto übersprungen, statt das ganze Programm abzubrechen.
    """
    hoehe, breite = bild.shape[:2]
    kante = NACHSUCHE_MAX_KANTE
    while True:
        faktor = min(1.0, kante / max(hoehe, breite))
        if faktor < 1.0:
            klein = np.array(Image.fromarray(bild).resize(
                (max(1, round(breite * faktor)), max(1, round(hoehe * faktor))), Image.LANCZOS))
        else:
            klein = bild
        try:
            orte = face_recognition.face_locations(
                klein, number_of_times_to_upsample=NACHSUCHE_UPSAMPLE, model=NACHSUCHE_MODELL)
            break
        except MemoryError:
            del klein
            neue_kante = int(min(kante, max(hoehe, breite)) * 0.7)
            if neue_kante < NACHSUCHE_MIN_KANTE:
                print("    WARNUNG: Nachsuche übersprungen - zu wenig Arbeitsspeicher.")
                return []
            print(f"    HINWEIS: Zu wenig Arbeitsspeicher für die Nachsuche bei {kante} px - "
                  f"neuer Versuch mit {neue_kante} px.")
            kante = neue_kante
    zurueck = []
    for oben, rechts, unten, links in orte:
        zurueck.append((
            max(0, int(oben / faktor)), min(breite, int(rechts / faktor)),
            min(hoehe, int(unten / faktor)), max(0, int(links / faktor)),
        ))
    return zurueck


def finde_gesichter(bild, debug=False):
    """
    Rückgabe: (orte, fundwege, dauer_nachsuche)
      orte:     Liste von Rahmen (oben, rechts, unten, links)
      fundwege: pro Rahmen "normal" (erste Suche) oder "nachsuche"
    """
    orte = face_recognition.face_locations(
        bild, number_of_times_to_upsample=GESICHTSSUCHE_UPSAMPLE, model=GESICHTSSUCHE_MODELL)
    fundwege = ["normal"] * len(orte)

    dauer = 0.0
    if MODELL == "insightface":
        pass  # InsightFace findet Gesichter bereits gründlich; keine zweite Suche nötig
    elif NACHSUCHE == "immer" or (NACHSUCHE == "kein_gesicht" and not orte):
        t0 = time.perf_counter()
        neue = 0
        for ort in suche_gesichter_verkleinert(bild):
            if all(ueberlappungsanteil(ort, alt) < NACHSUCHE_UEBERLAPPUNG for alt in orte):
                orte.append(ort)
                fundwege.append("nachsuche")
                neue += 1
        dauer = time.perf_counter() - t0
        if debug:
            print(f"    Nachsuche ({NACHSUCHE_MODELL}): {neue} zusätzliche(s) Gesicht(er) "
                  f"({dauer:.1f} s)")
    elif NACHSUCHE not in ("aus", "kein_gesicht", "immer"):
        raise ValueError(f"Unbekannte Einstellung NACHSUCHE: {NACHSUCHE}")
    return orte, fundwege, dauer


# -----------------------------------------------------------------
# ZUORDNUNG: eine Person pro Gesicht, ein Gesicht pro Person
# -----------------------------------------------------------------
def finde_beste_zuordnung(kandidaten_je_gesicht):
    """
    Eingabe: pro Gesicht eine Liste von (person, abstand)-Paaren, die
    unter der Toleranz liegen. Gesucht wird die Zuordnung, bei der
      1. möglichst viele Gesichter einer Person zugeordnet werden und
      2. (bei gleicher Anzahl) die Summe der Abstände möglichst klein ist,
    wobei jede Person höchstens einmal vergeben wird.

    Rückgabe: Liste mit einem Eintrag pro Gesicht: (person, abstand)
    oder None, wenn das Gesicht keiner Person zugeordnet wurde.
    """
    anzahl_gesichter = len(kandidaten_je_gesicht)
    aktive = [i for i, kandidaten in enumerate(kandidaten_je_gesicht) if kandidaten]

    # Sehr große Gruppenfotos: einfache Näherung (kleinster Abstand zuerst)
    if len(aktive) > MAX_GESICHTER_EXAKT:
        zuordnung = [None] * anzahl_gesichter
        alle_paare = sorted(
            (abstand, gesicht, person)
            for gesicht in aktive
            for person, abstand in kandidaten_je_gesicht[gesicht]
        )
        vergebene_personen = set()
        for abstand, gesicht, person in alle_paare:
            if zuordnung[gesicht] is None and person not in vergebene_personen:
                zuordnung[gesicht] = (person, abstand)
                vergebene_personen.add(person)
        return zuordnung

    beste = {"anzahl": -1, "summe": float("inf"), "zuordnung": [None] * anzahl_gesichter}

    def suche(position, vergeben, zuordnung, anzahl, summe):
        if position == len(aktive):
            if anzahl > beste["anzahl"] or (anzahl == beste["anzahl"] and summe < beste["summe"]):
                beste["anzahl"] = anzahl
                beste["summe"] = summe
                beste["zuordnung"] = list(zuordnung)
            return

        gesicht = aktive[position]

        # Variante 1: dieses Gesicht bleibt ohne Person
        suche(position + 1, vergeben, zuordnung, anzahl, summe)

        # Variante 2: dieses Gesicht bekommt eine noch freie Person
        for person, abstand in kandidaten_je_gesicht[gesicht]:
            if person not in vergeben:
                zuordnung[gesicht] = (person, abstand)
                suche(position + 1, vergeben | {person}, zuordnung, anzahl + 1, summe + abstand)
                zuordnung[gesicht] = None

    suche(0, frozenset(), [None] * anzahl_gesichter, 0, 0.0)
    return beste["zuordnung"]


# -----------------------------------------------------------------
# SCHRITT B: GESICHTER IN EINEM NEUEN FOTO ERKENNEN
# -----------------------------------------------------------------
def erkenne_personen_im_bild(bildpfad, referenz_daten, zieldatum, debug=False,
                              num_jitters=JITTERS_FOTOS):
    """
    Rückgabe: (erkannte_namen, zeiten, gesichter)
    zeiten:    {"laden", "gesichter", "zuordnung" (jeweils Sekunden), "anzahl_gesichter"}
    gesichter: pro gefundenem Gesicht {"hoehe": Gesichtshöhe in Pixel,
               "bildhoehe": Bildhöhe in Pixel, "abstaende": {Person: Abstand}}.
               In "abstaende" stehen nur Personen, die für dieses Foto in Frage
               kamen (nicht vor der Geburt, Referenzfoto vorhanden). Das ist die
               Grundlage für foto_abstaende.txt und die Auswertung/Simulation.
    """
    zeiten = {"laden": 0.0, "gesichter": 0.0, "nachsuche": 0.0, "zuordnung": 0.0,
              "anzahl_gesichter": 0, "anzahl_nachsuche": 0}

    t_start = time.perf_counter()
    bild = lade_bild_korrekt_ausgerichtet(bildpfad)
    t_geladen = time.perf_counter()
    gesichtsorte, fundwege, dauer_nachsuche = finde_gesichter(bild, debug=debug)
    gefundene_encodings = face_recognition.face_encodings(
        bild, known_face_locations=gesichtsorte, num_jitters=num_jitters
    )
    t_berechnet = time.perf_counter()

    zeiten["laden"] = t_geladen - t_start
    zeiten["gesichter"] = t_berechnet - t_geladen
    zeiten["nachsuche"] = dauer_nachsuche
    zeiten["anzahl_gesichter"] = len(gefundene_encodings)
    zeiten["anzahl_nachsuche"] = fundwege.count("nachsuche")

    bildhoehe = int(bild.shape[0])
    gesichter = [{"hoehe": int(ort[2] - ort[0]), "bildhoehe": bildhoehe, "abstaende": {},
                  "davor": {}, "danach": {}, "fundweg": weg}
                 for ort, weg in zip(gesichtsorte, fundwege)]

    def fertig(namen):
        zeiten["zuordnung"] = time.perf_counter() - t_berechnet
        return namen, zeiten, gesichter

    if len(gefundene_encodings) == 0:
        if debug:
            print("    Kein Gesicht im Bild gefunden (auch nach Ausrichtungskorrektur).")
        return fertig([])

    # Pro Person das zeitlich nächstgelegene Referenzfoto bestimmen
    # (zusätzlich: das letzte davor und das erste danach)
    kandidaten_namen = []
    kandidaten_encodings = []
    kandidaten_dateien = []
    kandidaten_davor = []    # (encoding, tage) oder None
    kandidaten_danach = []

    for person, eintraege in referenz_daten.items():
        if not eintraege:
            continue

        geburtsdatum = GEBURTSDATEN.get(person)
        if geburtsdatum is not None and zieldatum < geburtsdatum:
            if debug:
                print(f"    {person}: ausgeschlossen (Foto vom "
                      f"{zieldatum.strftime('%Y-%m-%d')} liegt vor Geburtsdatum "
                      f"{geburtsdatum.strftime('%Y-%m-%d')})")
            continue

        datum, encoding_ref, dateiname = waehle_naechstgelegenes_referenzfoto(eintraege, zieldatum)
        kandidaten_namen.append(person)
        kandidaten_encodings.append(encoding_ref)
        kandidaten_dateien.append(dateiname)

        davor, danach = waehle_referenz_davor_danach(eintraege, zieldatum)
        kandidaten_davor.append(None if davor is None else (davor[1], tage_zwischen(davor[0], zieldatum)))
        kandidaten_danach.append(None if danach is None else (danach[1], tage_zwischen(danach[0], zieldatum)))

        if debug:
            tage_abstand = abs((datum - zieldatum).days)
            print(f"    {person}: nächstgelegenes Referenzfoto {dateiname} vom "
                  f"{datum.strftime('%Y-%m-%d')} ({tage_abstand} Tage Abstand)")
            if REFERENZ_MODUS != "naechstes":
                for wort, eintrag in (("davor", davor), ("danach", danach)):
                    if eintrag is not None:
                        print(f"      Referenz {wort}: {eintrag[2]} vom {eintrag[0]:%Y-%m-%d}")

    if not kandidaten_encodings:
        return fertig([])

    # Pro Gesicht: Abstand zu jedem Kandidaten berechnen und Kandidaten bestimmen
    kandidaten_je_gesicht = []
    bester_je_gesicht = []   # (person, abstand) - nur für die Debug-Ausgabe
    verworfen_grund = []     # None oder Text, warum das Gesicht keiner Person zugeordnet wird

    for face_index, encoding in enumerate(gefundene_encodings):
        abstaende_naechst = face_recognition.face_distance(kandidaten_encodings, encoding)

        # Abstände zum Referenzfoto davor und danach (immer berechnet, für die Auswertung)
        kombiniert = []
        for i, person in enumerate(kandidaten_namen):
            werte = {}
            for wort, liste in (("davor", kandidaten_davor), ("danach", kandidaten_danach)):
                if liste[i] is None:
                    werte[wort] = None
                else:
                    enc_ref, tage = liste[i]
                    werte[wort] = (float(face_recognition.face_distance([enc_ref], encoding)[0]), tage)
                gesichter[face_index][wort][person] = werte[wort]
            kombiniert.append(kombiniere_abstaende(float(abstaende_naechst[i]),
                                                   werte["davor"], werte["danach"]))
        abstaende = np.array(kombiniert)

        gesichter[face_index]["abstaende"] = {
            kandidaten_namen[i]: float(abstaende[i]) for i in range(len(kandidaten_namen))
        }

        bester_index = int(abstaende.argmin())
        bester_je_gesicht.append((kandidaten_namen[bester_index], float(abstaende[bester_index])))

        grund = None
        hoehe = gesichter[face_index]["hoehe"]
        if hoehe < MIN_GESICHTSHOEHE_PX:
            grund = f"Gesicht zu klein ({hoehe} px < {MIN_GESICHTSHOEHE_PX} px)"
        elif MIN_ABSTAND_ZUM_ZWEITBESTEN > 0 and len(abstaende) >= 2:
            sortiert = sorted(float(a) for a in abstaende)
            luecke = sortiert[1] - sortiert[0]
            if luecke < MIN_ABSTAND_ZUM_ZWEITBESTEN:
                grund = (f"uneindeutig (Abstand zum Zweitbesten nur {luecke:.3f} "
                         f"< {MIN_ABSTAND_ZUM_ZWEITBESTEN})")
        verworfen_grund.append(grund)

        if grund is not None:
            kandidaten_je_gesicht.append([])
        else:
            kandidaten_je_gesicht.append([
                (kandidaten_namen[i], float(abstaende[i]))
                for i in range(len(kandidaten_namen))
                if abstaende[i] <= toleranz_fuer(kandidaten_namen[i])
            ])

        if debug:
            print(f"    Gesicht {face_index + 1} ({hoehe} px hoch): Ähnlichkeiten (kleiner = ähnlicher):")
            for i in abstaende.argsort():
                print(f"      {kandidaten_namen[i]} ({kandidaten_dateien[i]}): {abstaende[i]:.3f}")

    zuordnung = finde_beste_zuordnung(kandidaten_je_gesicht)

    erkannte_namen = []
    for face_index, ergebnis in enumerate(zuordnung):
        if ergebnis is not None:
            person, abstand = ergebnis
            erkannte_namen.append(person)
            if debug:
                print(f"    -> Gesicht {face_index + 1} = {person} (Abstand {abstand:.3f})")
        elif debug:
            beste_person, bester_abstand = bester_je_gesicht[face_index]
            if verworfen_grund[face_index] is not None:
                print(f"    -> Gesicht {face_index + 1}: nicht zugeordnet - {verworfen_grund[face_index]}")
            elif bester_abstand <= toleranz_fuer(beste_person):
                print(f"    -> Gesicht {face_index + 1}: nicht zugeordnet - die passende Person "
                      f"('{beste_person}', {bester_abstand:.3f}) ist bereits einem anderen "
                      f"Gesicht zugeordnet")
            else:
                print(f"    -> Gesicht {face_index + 1}: kein Treffer unter Toleranz "
                      f"{toleranz_fuer(beste_person)} (bester Wert: {bester_abstand:.3f} "
                      f"von '{beste_person}')")

    return fertig(erkannte_namen)


# -----------------------------------------------------------------
# DATEIEN SICHER SCHREIBEN
# -----------------------------------------------------------------
def schreibe_textdatei_sicher(pfad, inhalt):
    """
    Schreibt 'inhalt' in die Datei 'pfad' und gibt den tatsächlich verwendeten
    Pfad zurück. Ist die Datei gesperrt (typisch: in Excel geöffnet), wird
    stattdessen eine Datei mit Uhrzeit im Namen geschrieben, damit das Ergebnis
    der oft langen Berechnung nicht verloren geht.
    """
    try:
        with open(pfad, "w", encoding="utf-8") as f:
            f.write(inhalt)
        return pfad
    except PermissionError:
        basis, endung = os.path.splitext(pfad)
        ausweich = f"{basis}_{datetime.now():%Y%m%d_%H%M%S}{endung}"
        with open(ausweich, "w", encoding="utf-8") as f:
            f.write(inhalt)
        print(f"HINWEIS: '{pfad}' lässt sich nicht überschreiben (vermutlich in Excel oder "
              f"einem anderen Programm geöffnet). Stattdessen gespeichert: {ausweich}")
        return ausweich


def pruefe_ausgabedateien(fotoordner):
    """Warnt VOR der langen Berechnung, wenn eine Ergebnisdatei gesperrt ist."""
    for name in ("foto_tags.json", "foto_tags_tabelle.txt", "foto_zeiten.txt", "foto_abstaende.txt"):
        pfad = os.path.join(fotoordner, name)
        if os.path.exists(pfad):
            try:
                with open(pfad, "a"):
                    pass
            except PermissionError:
                print(f"HINWEIS: '{name}' ist gesperrt (vermutlich in Excel geöffnet). Am besten jetzt "
                      f"schließen - sonst wird das Ergebnis unter einem Ausweichnamen mit Uhrzeit gespeichert.")
    print()


# -----------------------------------------------------------------
# AUSGABE 1: Ergebnistabelle (Tab-getrennt)
# -----------------------------------------------------------------
def schreibe_ergebnistabelle(alle_tags, zieldatei):
    """
    Dateiname <TAB> Person 1 <TAB> Person 2 <TAB> ...
    Pro Person: "X" = erkannt, "-" = nicht erkannt.
    Zeilen aufsteigend nach Dateiname (ohne Beachtung von Groß-/Kleinschreibung).
    """
    zeilen = ["\t".join(["Dateiname"] + TABELLEN_PERSONEN)]

    for tags in sorted(alle_tags, key=lambda t: t["datei"].lower()):
        zellen = [tags["datei"]]
        for person in TABELLEN_PERSONEN:
            zellen.append("X" if person in tags["personen"] else "-")
        zeilen.append("\t".join(zellen))

    return schreibe_textdatei_sicher(zieldatei, "\n".join(zeilen) + "\n")


# -----------------------------------------------------------------
# AUSGABE 2: Zeitdatei (Tab-getrennt)
# -----------------------------------------------------------------
def _sek(wert):
    """Sekunden mit 3 Nachkommastellen und dem eingestellten Dezimaltrenner."""
    return f"{wert:.3f}".replace(".", DEZIMALTRENNER)


def schreibe_zeitdatei(zieldatei, referenz_zeiten, foto_zeiten, foto_gesamtdauer, debug):
    """
    Schreibt die Berechnungszeiten in drei Abschnitten:
      EINSTELLUNGEN - damit Läufe vergleichbar bleiben
      REFERENZFOTOS - Zeit pro Referenzfoto + Summe/Durchschnitt
      FOTOS         - Zeit pro Foto (Gesamt + Aufschlüsselung) + Summe/Durchschnitt
    Alle Zeiten in Sekunden.
    """
    zeilen = []

    # --- Einstellungen ---
    zeilen.append("EINSTELLUNGEN")
    zeilen.append(f"num_jitters Referenzfotos\t{JITTERS_REFERENZ}")
    zeilen.append(f"num_jitters Fotos\t{JITTERS_FOTOS}")
    zeilen.append(f"Modell\t{MODELL}")
    zeilen.append(f"Toleranz\t{TOLERANZ_INSIGHTFACE if MODELL == 'insightface' else TOLERANZ}"
                  + (f" (je Person: {TOLERANZ_JE_PERSON})" if TOLERANZ_JE_PERSON else ""))
    zeilen.append(f"Gesichtssuche\t{GESICHTSSUCHE_MODELL}, upsample {GESICHTSSUCHE_UPSAMPLE}")
    zeilen.append(f"Nachsuche\t{NACHSUCHE} ({NACHSUCHE_MODELL}, upsample {NACHSUCHE_UPSAMPLE}, "
                  f"max. Kante {NACHSUCHE_MAX_KANTE} px)")
    zeilen.append(f"Referenz-Modus\t{REFERENZ_MODUS}")
    zeilen.append(f"Debug-Modus (Ausgabezeit ist in den Zeiten enthalten)\t{'ja' if debug else 'nein'}")
    zeilen.append("")

    # --- Referenzfotos ---
    ref = referenz_zeiten["eintraege"]
    zeilen.append("REFERENZFOTOS (Sekunden)")
    zeilen.append("\t".join(["Person", "Dateiname", "Gesamt", "Laden", "Gesichter_berechnen", "Hinweis"]))
    for e in sorted(ref, key=lambda e: (e["person"].lower(), e["datei"].lower())):
        zeilen.append("\t".join([
            e["person"], e["datei"], _sek(e["gesamt"]), _sek(e["laden"]),
            _sek(e["gesichter"]), e["hinweis"],
        ]))
    zeilen.append("\t".join(["Summe aller Referenzfotos (inkl. Datumsermittlung)", "",
                              _sek(referenz_zeiten["gesamt"])]))
    if ref:
        durchschnitt = sum(e["gesamt"] for e in ref) / len(ref)
        zeilen.append("\t".join(["Durchschnitt pro Referenzfoto", "", _sek(durchschnitt)]))
    zeilen.append("")

    # --- Fotos ---
    zeilen.append("FOTOS (Sekunden)")
    zeilen.append("\t".join(["Dateiname", "Gesamt", "Datum", "Laden", "Gesichter_berechnen",
                              "Davon_Nachsuche", "Zuordnung", "Anzahl_Gesichter",
                              "Davon_aus_Nachsuche"]))
    for e in sorted(foto_zeiten, key=lambda e: e["datei"].lower()):
        zeilen.append("\t".join([
            e["datei"], _sek(e["gesamt"]), _sek(e["datum"]), _sek(e["laden"]),
            _sek(e["gesichter"]), _sek(e["nachsuche"]), _sek(e["zuordnung"]),
            str(e["anzahl_gesichter"]), str(e["anzahl_nachsuche"]),
        ]))
    summe_fotos = sum(e["gesamt"] for e in foto_zeiten)
    zeilen.append("\t".join(["Summe aller Fotos", _sek(summe_fotos)]))
    if foto_zeiten:
        zeilen.append("\t".join(["Durchschnitt pro Foto", _sek(summe_fotos / len(foto_zeiten))]))
    zeilen.append("")
    zeilen.append("\t".join(["Gesamtlaufzeit (Referenzfotos + Fotos)",
                              _sek(referenz_zeiten["gesamt"] + foto_gesamtdauer)]))

    return schreibe_textdatei_sicher(zieldatei, "\n".join(zeilen) + "\n")


# -----------------------------------------------------------------
# AUSGABE 3: Abstände pro Gesicht (Tab-getrennt) - Grundlage für die Auswertung
# -----------------------------------------------------------------
def schreibe_abstandsdatei(zieldatei, gesichts_zeilen):
    """
    Pro gefundenem Gesicht eine Zeile: Dateiname, Gesichtsnummer, Datum,
    Gesichtshöhe und Bildhöhe (Pixel) sowie der Abstand zu jeder Person
    (kleiner = ähnlicher). Eine leere Zelle heißt: Die Person kam für dieses
    Foto nicht in Frage (z.B. Foto vor ihrer Geburt). Fotos ohne gefundenes
    Gesicht haben keine Zeile.
    """
    kopf = ["Dateiname", "Gesicht", "Datum", "Gesichtshoehe_px", "Bildhoehe_px"]
    kopf += [f"Abstand_{person}" for person in TABELLEN_PERSONEN]
    # Zusatzspalten für die Auswertung (bewusst NICHT mit "Abstand_" beginnend,
    # damit auswertung_erkennung.py sie nicht für Personen hält)
    for wort in ("Davor", "Danach"):
        kopf += [f"{wort}_{person}" for person in TABELLEN_PERSONEN]
        kopf += [f"Tage{wort}_{person}" for person in TABELLEN_PERSONEN]
    kopf += ["Fundweg"]
    zeilen = ["\t".join(kopf)]

    for eintrag in sorted(gesichts_zeilen, key=lambda e: (e["datei"].lower(), e["gesicht"])):
        zellen = [eintrag["datei"], str(eintrag["gesicht"]),
                  f"{eintrag['datum']:%Y-%m-%d %H:%M:%S}",
                  str(eintrag["hoehe"]), str(eintrag["bildhoehe"])]
        for person in TABELLEN_PERSONEN:
            wert = eintrag["abstaende"].get(person)
            zellen.append("" if wert is None else f"{wert:.4f}".replace(".", DEZIMALTRENNER))
        for wort in ("davor", "danach"):
            werte = [eintrag[wort].get(person) for person in TABELLEN_PERSONEN]
            zellen += ["" if w is None else f"{w[0]:.4f}".replace(".", DEZIMALTRENNER) for w in werte]
            zellen += ["" if w is None else f"{w[1]:.1f}".replace(".", DEZIMALTRENNER) for w in werte]
        zellen.append(eintrag["fundweg"])
        zeilen.append("\t".join(zellen))

    return schreibe_textdatei_sicher(zieldatei, "\n".join(zeilen) + "\n")


# -----------------------------------------------------------------
# DIE PIPELINE
# -----------------------------------------------------------------
def verarbeite_fotoordner(fotoordner, referenz_daten, referenz_zeiten, debug=False):
    bilddateien = [
        f for f in os.listdir(fotoordner)
        if f.lower().endswith((".jpg", ".jpeg", ".png"))
    ]

    print(f"{len(bilddateien)} Foto(s) zum Taggen gefunden.\n")

    start_fotos = time.perf_counter()
    alle_tags = []
    foto_zeiten = []
    gesichts_zeilen = []

    pruefe_ausgabedateien(fotoordner)

    gesamtzahl = len(bilddateien)
    stellen = len(str(gesamtzahl))  # damit die Nummern bündig untereinander stehen

    for nummer, dateiname in enumerate(bilddateien, start=1):
        bildpfad = os.path.join(fotoordner, dateiname)
        print(f"{nummer:>{stellen}}/{gesamtzahl} Verarbeite: {dateiname}")

        t_start = time.perf_counter()
        datum = lese_aufnahmedatum(bildpfad, debug=debug)
        t_datum = time.perf_counter()

        personen, bild_zeiten, gesichter = erkenne_personen_im_bild(
            bildpfad, referenz_daten, datum, debug=debug
        )
        gesamt = time.perf_counter() - t_start

        for nr_gesicht, gesicht in enumerate(gesichter, start=1):
            gesichts_zeilen.append({
                "datei": dateiname, "gesicht": nr_gesicht, "datum": datum,
                "hoehe": gesicht["hoehe"], "bildhoehe": gesicht["bildhoehe"],
                "abstaende": gesicht["abstaende"],
                "davor": gesicht["davor"], "danach": gesicht["danach"],
                "fundweg": gesicht["fundweg"],
            })

        foto_zeiten.append({
            "datei": dateiname,
            "gesamt": gesamt,
            "datum": t_datum - t_start,
            **bild_zeiten,
        })

        tags = {
            "datei": dateiname,
            "jahr": datum.year,
            "monat": datum.month,
            "tag": datum.day,
            "stunde": datum.hour,
            "minute": datum.minute,
            "personen": sorted(personen),
        }
        alle_tags.append(tags)
        print(f"  -> Erkannt: {tags['personen'] if personen else 'niemand Bekanntes'} "
              f"({gesamt:.2f} s)\n")

    foto_gesamtdauer = time.perf_counter() - start_fotos

    json_text = json.dumps(alle_tags, indent=2, ensure_ascii=False) + "\n"
    json_datei = schreibe_textdatei_sicher(os.path.join(fotoordner, "foto_tags.json"), json_text)
    tabellen_datei = schreibe_ergebnistabelle(
        alle_tags, os.path.join(fotoordner, "foto_tags_tabelle.txt"))
    zeiten_datei = schreibe_zeitdatei(
        os.path.join(fotoordner, "foto_zeiten.txt"), referenz_zeiten, foto_zeiten,
        foto_gesamtdauer, debug)
    abstands_datei = schreibe_abstandsdatei(
        os.path.join(fotoordner, "foto_abstaende.txt"), gesichts_zeilen)

    print(f"Alle Tags gespeichert in: {json_datei}")
    print(f"Ergebnistabelle gespeichert in: {tabellen_datei}")
    print(f"Berechnungszeiten gespeichert in: {zeiten_datei}")
    print(f"Abstände pro Gesicht gespeichert in: {abstands_datei}")
    if bilddateien:
        print(f"\nReferenzfotos: {referenz_zeiten['gesamt']:.1f} s | "
              f"{len(bilddateien)} Fotos: {foto_gesamtdauer:.1f} s "
              f"(Ø {foto_gesamtdauer / len(bilddateien):.2f} s pro Foto)")


# -----------------------------------------------------------------
# STARTPUNKT
# -----------------------------------------------------------------
if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Aufruf: python foto_pipeline_mit_gesichtserkennung.py "
              "<referenzordner> <fotoordner> [--debug]")
        sys.exit(1)

    referenzordner = sys.argv[1]
    fotoordner = sys.argv[2]
    debug_modus = "--debug" in sys.argv
    referenz_daten, referenz_zeiten = lade_referenzgesichter(referenzordner, debug=debug_modus)

    # Plausibilitätsprüfung: passen Tabellenspalten und Referenzordner zusammen?
    for person in TABELLEN_PERSONEN:
        if person not in referenz_daten:
            print(f"HINWEIS: Für '{person}' gibt es keinen Ordner in '{referenzordner}' - "
                  f"die Spalte bleibt komplett bei '-'.")
    for person in referenz_daten:
        if person not in TABELLEN_PERSONEN:
            print(f"HINWEIS: '{person}' hat einen Referenzordner, steht aber nicht in "
                  f"TABELLEN_PERSONEN - wird in der Tabelle nicht aufgeführt.")

    verarbeite_fotoordner(fotoordner, referenz_daten, referenz_zeiten, debug=debug_modus)
