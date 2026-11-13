"""
FOTOS UND VIDEOS NACH DATUM SORTIEREN
=====================================

Sortiert alle Fotos und Videos aus einem Quellordner (samt Unterordnern) in die
Struktur
        <Zielordner>\\JJJJ\\MM\\TT\\Dateiname        z.B. D:\\Fotos\\2026\\10\\09\\IMG_0001.jpg
und löst dabei Mehrfachkopien auf, sodass jedes Foto nur einmal vorkommt.

STANDARD = KOPIEREN: Die Originale bleiben unangetastet, solange du nicht
ausdrücklich --verschieben angibst. Es wird NIE etwas gelöscht.

DUPLIKATE
    1. Exakte Kopien (Datei-Inhalt identisch, egal wie die Datei heißt oder wo sie
       liegt): Es wird nur eine Datei übernommen.
    2. Dieselbe Aufnahme in schlechterer Qualität (z.B. die über WhatsApp
       verschickte, verkleinerte Fassung): Es gewinnt die Fassung mit der höheren
       Auflösung. Erkannt wird das nur, wenn die Auflösung der Fassungen
       UNTERSCHIEDLICH ist; Fotos mit gleicher Auflösung (z.B. Serienbilder)
       bleiben beide erhalten, auch wenn sie sich sehr ähnlich sehen.
    Videos werden nur auf exakte Kopien geprüft.
    Bei --verschieben werden die aussortierten Duplikate nicht gelöscht, sondern
    in <Zielordner>\\_Duplikate verschoben.

DATUM (Priorität, die beste Quelle unter ALLEN Kopien/Fassungen eines Fotos gewinnt)
    1. EXIF-Aufnahmedatum (Fotos)
    2. volles Datum im Dateinamen (JJJJ-MM-TT, JJJJ_MM_TT, JJJJMMTT)
    3. Aufnahmedatum in den Video-Metadaten (mp4/mov/m4v/3gp)
    4. volles Datum im Ordnernamen, dann im Namen des Ordners darüber
    5. nur Jahr und Monat (Tag = 01) im Dateinamen, Ordner, Ordner darüber
    6. Änderungsdatum der Datei (nur wenn plausibel)
    Ohne verwendbares Datum landet die Datei in <Zielordner>\\_Datum_unbekannt.
    (Die Datums-Funktionen stammen aus exif_datum_nachtragen.py; diese Datei muss
    im selben Ordner liegen.)

Aufruf:
    python fotos_sortieren.py <quellordner> <zielordner> [--trockenlauf] [--verschieben]
                              [--pruefen] [--nur-exakte-duplikate]

    --trockenlauf           nur planen und protokollieren, nichts verändern (zuerst machen!)
    --verschieben           Dateien verschieben statt kopieren
    --pruefen               nach dem Kopieren zusätzlich die Prüfsumme der Kopie
                            kontrollieren (sicherer, aber langsamer)
    --nur-exakte-duplikate  keine Erkennung "gleiche Aufnahme in schlechterer Qualität"

Der Zielordner darf auch innerhalb des Quellordners liegen (er wird dann nicht
mit durchsucht). Ein erneuter Lauf ist ungefährlich: Dateien, die schon identisch
am Ziel liegen, werden übersprungen.

Ausgabe: Protokoll (Tab-getrennt) im aktuellen Verzeichnis:
    sortierung_protokoll_<Datum>_<Uhrzeit>.txt
"""

import hashlib
import os
import shutil
import struct
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime

try:
    from PIL import Image, ImageOps
    import exif_datum_nachtragen as datum_werkzeug
except ImportError as fehler:
    print(f"FEHLER: {fehler}")
    print("Benötigt werden Pillow (python -m pip install pillow) und die Datei "
          "exif_datum_nachtragen.py im selben Ordner wie dieses Programm.")
    sys.exit(1)


# -----------------------------------------------------------------
# EINSTELLUNGEN
# -----------------------------------------------------------------
FOTO_ENDUNGEN = (".jpg", ".jpeg", ".png", ".heic", ".webp", ".gif", ".bmp", ".tif", ".tiff")
VIDEO_ENDUNGEN = (".mp4", ".mov", ".m4v", ".3gp", ".avi", ".mkv", ".wmv",
                  ".mpg", ".mpeg", ".mts", ".m2ts")

# Erkennung "gleiche Aufnahme in anderer Qualität":
DHASH_MAX = 6          # max. Unterschied (von 64 Bit) des Bildabdrucks
MAD_MAX = 10.0         # max. mittlere Helligkeitsabweichung (0-255) auf 32x32 Pixeln
RATIO_TOLERANZ = 0.02  # max. relativer Unterschied des Seitenverhältnisses
MAX_EIMER = 300        # größere Gruppen "gleicher Abdrücke" (z.B. schwarze Bilder) werden übersprungen
MAX_GESICHTER_EXAKT = 12
BAENDER = [(0, 9), (9, 18), (18, 27), (27, 36), (36, 45), (45, 54), (54, 64)]  # 7 Bänder: Abstand <= 6 -> 1 Band gleich

KOPIE_MARKIERUNGEN = (" - kopie", " - copy", "(1)", "(2)", "(3)", "kopie von", "copy of", "_copy", "-copy")

# Rangfolge der Datumsquellen (kleiner = besser)
RANG = {"EXIF": 0, "Dateiname": 1, "Video-Metadaten": 2, "Ordner": 3, "Ordner darüber": 4,
        "Dateiname (nur Jahr/Monat)": 5, "Ordner (nur Jahr/Monat)": 6,
        "Ordner darüber (nur Jahr/Monat)": 7, "Änderungsdatum": 8}
KEIN_DATUM = 99


# -----------------------------------------------------------------
# DATUM BESTIMMEN
# -----------------------------------------------------------------
def _plausibel(datum):
    jetzt = datetime.now()
    return datum is not None and 1990 <= datum.year <= jetzt.year and datum <= jetzt


def _mvhd_datum(f, start, ende):
    position = start
    while position + 8 <= ende:
        f.seek(position)
        groesse, typ = struct.unpack(">I4s", f.read(8))
        if groesse < 8:
            return None
        if typ == b"mvhd":
            version = f.read(1)[0]
            f.read(3)
            sekunden = struct.unpack(">Q", f.read(8))[0] if version == 1 else struct.unpack(">I", f.read(4))[0]
            if sekunden == 0:
                return None
            try:
                # Sekunden seit 1904 (UTC) -> lokale Zeit dieses Rechners
                datum = datetime.fromtimestamp(sekunden - 2082844800)
            except (OverflowError, OSError, ValueError):
                return None
            return datum if _plausibel(datum) else None
        position += groesse
    return None


def lese_video_datum(pfad):
    """Aufnahmedatum aus den Metadaten einer mp4/mov-Datei (ohne Zusatzprogramme), sonst None."""
    try:
        with open(pfad, "rb") as f:
            f.seek(0, 2)
            dateigroesse = f.tell()
            position = 0
            while position + 8 <= dateigroesse:
                f.seek(position)
                kopf = f.read(8)
                if len(kopf) < 8:
                    return None
                groesse, typ = struct.unpack(">I4s", kopf)
                kopflaenge = 8
                if groesse == 1:
                    groesse = struct.unpack(">Q", f.read(8))[0]
                    kopflaenge = 16
                elif groesse == 0:
                    groesse = dateigroesse - position
                if groesse < kopflaenge:
                    return None
                if typ == b"moov":
                    return _mvhd_datum(f, position + kopflaenge, position + groesse)
                position += groesse
    except (OSError, struct.error, IndexError):
        return None
    return None


def bestimme_datum(pfad, ist_video):
    """Rückgabe: (datum, quelle, rang) - die beste verfügbare Datumsquelle für diese Datei."""
    w = datum_werkzeug
    absolut = os.path.abspath(pfad)
    name = os.path.basename(absolut)
    ordner = os.path.dirname(absolut)
    ordnername = os.path.basename(ordner)
    oberordnername = os.path.basename(os.path.dirname(ordner))

    if not ist_video:
        datum = w.lese_exif_datum(pfad)
        if datum is not None:
            return datum, "EXIF", RANG["EXIF"]

    suche = [("Dateiname", lambda: w.lese_datum_aus_dateiname(name))]
    if ist_video:
        suche.append(("Video-Metadaten", lambda: lese_video_datum(pfad)))
    suche += [
        ("Ordner", lambda: w.lese_datum_aus_ordnername(ordnername)),
        ("Ordner darüber", lambda: w.lese_datum_aus_ordnername(oberordnername)),
        ("Dateiname (nur Jahr/Monat)", lambda: w.lese_monat_aus_text(name)),
        ("Ordner (nur Jahr/Monat)", lambda: w.lese_monat_aus_text(ordnername)),
        ("Ordner darüber (nur Jahr/Monat)", lambda: w.lese_monat_aus_text(oberordnername)),
        ("Änderungsdatum", lambda: w.lese_aenderungsdatum(pfad)),
    ]
    for quelle, finde in suche:
        datum = finde()
        if datum is not None:
            return datum, quelle, RANG[quelle]
    return None, "kein Datum", KEIN_DATUM


# -----------------------------------------------------------------
# DATEIEN FINDEN, EXAKTE DUPLIKATE
# -----------------------------------------------------------------
def sammle_dateien(quelle, ziel):
    fotos, videos, andere = [], [], 0
    ausgeschlossen = datum_werkzeug.AUSGESCHLOSSENE_ORDNER
    ziel_norm = os.path.normcase(os.path.abspath(ziel))

    def nicht_lesbar(fehler):
        print(f"HINWEIS: Ordner nicht lesbar und übersprungen: {fehler.filename}")

    for ordner, unterordner, dateien in os.walk(quelle, onerror=nicht_lesbar):
        unterordner[:] = [
            u for u in unterordner
            if u.lower() not in ausgeschlossen
            and os.path.normcase(os.path.abspath(os.path.join(ordner, u))) != ziel_norm
        ]
        for name in dateien:
            if name.startswith("._"):  # macOS-Begleitdateien
                continue
            endung = os.path.splitext(name)[1].lower()
            pfad = os.path.join(ordner, name)
            if endung in FOTO_ENDUNGEN:
                fotos.append(pfad)
            elif endung in VIDEO_ENDUNGEN:
                videos.append(pfad)
            else:
                andere += 1
    return fotos, videos, andere


def sha256_datei(pfad):
    pruefsumme = hashlib.sha256()
    with open(pfad, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            pruefsumme.update(block)
    return pruefsumme.hexdigest()


def identisch(pfad_a, pfad_b):
    try:
        return os.path.getsize(pfad_a) == os.path.getsize(pfad_b) and sha256_datei(pfad_a) == sha256_datei(pfad_b)
    except OSError:
        return False


def kopie_wertung(pfad):
    """Welche von mehreren identischen Dateien als 'Original' gilt (kleiner = besser)."""
    name = os.path.basename(pfad).lower()
    markiert = any(m in name for m in KOPIE_MARKIERUNGEN)
    return (markiert, len(pfad), pfad.lower())


def finde_exakte_gruppen(pfade, art, fehlerliste):
    """Gruppiert Dateien mit identischem Inhalt. Rückgabe: Liste von Einträgen (Dictionaries)."""
    nach_groesse = defaultdict(list)
    for pfad in pfade:
        try:
            nach_groesse[os.path.getsize(pfad)].append(pfad)
        except OSError as fehler:
            fehlerliste.append((pfad, f"nicht lesbar: {fehler}"))

    eintraege = []
    zu_pruefen = sum(len(p) for p in nach_groesse.values() if len(p) > 1)
    geprueft = 0
    for groesse, gleich_grosse in nach_groesse.items():
        if len(gleich_grosse) == 1:
            eintraege.append({"art": art, "groesse": groesse, "pfade": gleich_grosse})
            continue
        nach_hash = defaultdict(list)
        for pfad in gleich_grosse:
            try:
                nach_hash[sha256_datei(pfad)].append(pfad)
            except OSError as fehler:
                fehlerliste.append((pfad, f"nicht lesbar: {fehler}"))
            geprueft += 1
            if geprueft % 200 == 0:
                print(f"   ... {geprueft}/{zu_pruefen} Prüfsummen berechnet")
        for pfade_gleich in nach_hash.values():
            eintraege.append({"art": art, "groesse": groesse, "pfade": pfade_gleich})

    for eintrag in eintraege:
        eintrag["pfade"].sort(key=kopie_wertung)  # erstes Element = Vertreter ("Original")
    return eintraege


# -----------------------------------------------------------------
# "GLEICHE AUFNAHME IN ANDERER QUALITÄT" (nur Fotos)
# -----------------------------------------------------------------
def berechne_merkmale(pfad):
    """Auflösung, Seitenverhältnis, Bildabdruck (dHash) und 32x32-Miniatur - oder None."""
    try:
        with Image.open(pfad) as bild:
            breite, hoehe = bild.size
            if breite < 1 or hoehe < 1:
                return None
            bild.draft("L", (128, 128))  # JPEG: schneller, verkleinert dekodieren
            bild = ImageOps.exif_transpose(bild)  # Drehung laut EXIF berücksichtigen
            grau = bild.convert("L")
            neun_mal_acht = grau.resize((9, 8), Image.Resampling.LANCZOS).tobytes()  # 72 Helligkeitswerte
            abdruck = 0
            for zeile in range(8):
                for spalte in range(8):
                    abdruck = (abdruck << 1) | (neun_mal_acht[zeile * 9 + spalte] > neun_mal_acht[zeile * 9 + spalte + 1])
            miniatur = grau.resize((32, 32), Image.Resampling.LANCZOS).tobytes()
            return {"pixel": breite * hoehe, "ratio": max(breite, hoehe) / min(breite, hoehe),
                    "dhash": abdruck, "miniatur": miniatur}
    except Exception:
        return None


def hamming(a, b):
    return bin(a ^ b).count("1")


def ist_aehnlich(m1, m2):
    """Gleiche Aufnahme (unabhängig von Auflösung/Kompression)? Auflösung wird hier NICHT verglichen."""
    if hamming(m1["dhash"], m2["dhash"]) > DHASH_MAX:
        return False
    if abs(m1["ratio"] - m2["ratio"]) / max(m1["ratio"], m2["ratio"]) > RATIO_TOLERANZ:
        return False
    abweichung = sum(abs(x - y) for x, y in zip(m1["miniatur"], m2["miniatur"])) / len(m1["miniatur"])
    return abweichung <= MAD_MAX


def finde_aehnliche(eintraege):
    """
    Rückgabe: (verlierer, gleiche_aufloesung_paare)
      verlierer: {Index des schlechteren Eintrags: Index des Gewinners}
      gleiche_aufloesung_paare: Anzahl ähnlicher Paare MIT GLEICHER Auflösung (bleiben beide erhalten)
    """
    kandidaten = [i for i, e in enumerate(eintraege) if e.get("merkmale")]
    eimer = defaultdict(list)
    for i in kandidaten:
        abdruck = eintraege[i]["merkmale"]["dhash"]
        for nr, (von, bis) in enumerate(BAENDER):
            eimer[(nr, (abdruck >> von) & ((1 << (bis - von)) - 1))].append(i)

    paare = set()
    uebersprungen = 0
    for liste in eimer.values():
        if len(liste) > MAX_EIMER:
            uebersprungen += 1
        elif len(liste) >= 2:
            for x in range(len(liste)):
                for y in range(x + 1, len(liste)):
                    paare.add((liste[x], liste[y]))
    if uebersprungen:
        print(f"    HINWEIS: {uebersprungen} Gruppen mit sehr vielen gleich aussehenden Bildern (z.B. fast "
              f"einfarbige oder schwarze Aufnahmen) wurden nicht auf schlechtere Fassungen geprüft.")

    verbunden = defaultdict(set)
    gleiche_aufloesung = 0
    for i, j in paare:
        m1, m2 = eintraege[i]["merkmale"], eintraege[j]["merkmale"]
        if ist_aehnlich(m1, m2):
            if m1["pixel"] == m2["pixel"]:
                gleiche_aufloesung += 1
            else:
                verbunden[i].add(j)
                verbunden[j].add(i)

    verlierer = {}
    gesehen = set()
    for start in verbunden:
        if start in gesehen:
            continue
        gruppe, stapel = set(), [start]
        while stapel:
            aktuell = stapel.pop()
            if aktuell in gruppe:
                continue
            gruppe.add(aktuell)
            stapel.extend(verbunden[aktuell] - gruppe)
        gesehen |= gruppe

        # Gewinner: höchste Auflösung, dann größte Datei; jedes Mitglied muss direkt zum Gewinner passen
        gewinner = max(gruppe, key=lambda i: (eintraege[i]["merkmale"]["pixel"], eintraege[i]["groesse"],
                                              [-ord(c) for c in eintraege[i]["pfade"][0].lower()]))
        for i in gruppe:
            if i != gewinner and eintraege[i]["merkmale"]["pixel"] < eintraege[gewinner]["merkmale"]["pixel"] \
                    and ist_aehnlich(eintraege[gewinner]["merkmale"], eintraege[i]["merkmale"]):
                verlierer[i] = gewinner
    return verlierer, gleiche_aufloesung


# -----------------------------------------------------------------
# ZIELPFADE PLANEN
# -----------------------------------------------------------------
def zielverzeichnis_fuer(ziel, datum):
    if datum is None:
        return os.path.join(ziel, "_Datum_unbekannt")
    return os.path.join(ziel, f"{datum.year:04d}", f"{datum.month:02d}", f"{datum.day:02d}")


def ermittle_zielpfad(verzeichnis, name, quelle, belegt, auf_identisches_pruefen=True):
    """
    Wählt einen freien Zielpfad (bei Namensgleichheit mit anderem Inhalt: _2, _3, ...).
    Rückgabe: (pfad, bereits_vorhanden) - bereits_vorhanden=True, wenn dort schon eine
    identische Datei liegt.
    """
    stamm, endung = os.path.splitext(name)
    nummer = 1
    while True:
        kandidat = os.path.join(verzeichnis, name if nummer == 1 else f"{stamm}_{nummer}{endung}")
        schluessel = os.path.normcase(kandidat)
        if schluessel in belegt:
            nummer += 1
            continue
        if os.path.exists(kandidat):
            if auf_identisches_pruefen and identisch(quelle, kandidat):
                return kandidat, True
            nummer += 1
            continue
        return kandidat, False


# -----------------------------------------------------------------
# DATEIEN KOPIEREN / VERSCHIEBEN
# -----------------------------------------------------------------
def kopiere_sicher(quelle, ziel, pruefsumme_pruefen):
    os.makedirs(os.path.dirname(ziel), exist_ok=True)
    temp = ziel + ".kopie_tmp"
    try:
        shutil.copy2(quelle, temp)
        if os.path.getsize(temp) != os.path.getsize(quelle):
            raise OSError("Größe der Kopie weicht vom Original ab")
        if pruefsumme_pruefen and sha256_datei(temp) != sha256_datei(quelle):
            raise OSError("Prüfsumme der Kopie weicht vom Original ab")
        os.replace(temp, ziel)
    except Exception:
        if os.path.exists(temp):
            os.remove(temp)
        raise


def verschiebe_sicher(quelle, ziel):
    """Gleiches Laufwerk: sofortiges Umbenennen. Sonst: kopieren, Prüfsumme kontrollieren, dann Original löschen."""
    os.makedirs(os.path.dirname(ziel), exist_ok=True)
    try:
        os.rename(quelle, ziel)
        return
    except OSError:
        if os.path.exists(ziel):
            raise
    # Umbenennen nicht möglich (z.B. anderes Laufwerk): kopieren, prüfen, dann Original entfernen
    kopiere_sicher(quelle, ziel, pruefsumme_pruefen=True)
    try:
        os.remove(quelle)
    except OSError as fehler:
        raise OSError(f"Kopie liegt am Ziel, das Original konnte aber nicht entfernt werden: {fehler}")


# -----------------------------------------------------------------
# HAUPTABLAUF
# -----------------------------------------------------------------
def freier_speicher(pfad):
    ordner = os.path.abspath(pfad)
    while not os.path.exists(ordner):
        uebergeordnet = os.path.dirname(ordner)
        if uebergeordnet == ordner:
            break
        ordner = uebergeordnet
    return shutil.disk_usage(ordner).free


def protokollname():
    basis = f"sortierung_protokoll_{datetime.now():%Y%m%d_%H%M%S}"
    name, nummer = f"{basis}.txt", 2
    while os.path.exists(name):
        name = f"{basis}_{nummer}.txt"
        nummer += 1
    return name


def mb(byte_zahl):
    return f"{byte_zahl / (1024 * 1024):,.0f} MB".replace(",", ".")


def sortiere(quelle, ziel, trockenlauf, verschieben, pruefen, aehnliche_suchen):
    start = time.perf_counter()
    quelle, ziel = os.path.abspath(quelle), os.path.abspath(ziel)
    fehlerliste = []
    protokoll_pfad = protokollname()

    # --- 1. Suchen ----------------------------------------------------
    print(f"1/5 Suche Fotos und Videos in: {quelle}")
    fotos, videos, andere = sammle_dateien(quelle, ziel)
    print(f"    {len(fotos)} Fotos, {len(videos)} Videos gefunden "
          f"({andere} andere Dateien werden ignoriert und nicht angefasst)\n")

    # --- 2. Exakte Duplikate ---------------------------------------------
    print("2/5 Suche exakte Kopien (gleicher Dateiinhalt)...")
    eintraege = (finde_exakte_gruppen(fotos, "foto", fehlerliste)
                 + finde_exakte_gruppen(videos, "video", fehlerliste))
    exakte_kopien = sum(len(e["pfade"]) - 1 for e in eintraege)
    print(f"    {exakte_kopien} Dateien sind exakte Kopien einer anderen Datei\n")

    # --- 3. Gleiche Aufnahme in anderer Qualität --------------------------
    verlierer, gleiche_aufloesung = {}, 0
    if aehnliche_suchen:
        foto_indizes = [i for i, e in enumerate(eintraege) if e["art"] == "foto"]
        print(f"3/5 Suche dieselben Aufnahmen in unterschiedlicher Qualität ({len(foto_indizes)} Fotos)...")
        for nr, i in enumerate(foto_indizes, start=1):
            eintraege[i]["merkmale"] = berechne_merkmale(eintraege[i]["pfade"][0])
            if nr % 200 == 0:
                print(f"   ... {nr}/{len(foto_indizes)} Fotos analysiert")
        verlierer, gleiche_aufloesung = finde_aehnliche(eintraege)
        print(f"    {len(verlierer)} Fotos sind die schlechtere Fassung einer anderen Aufnahme; "
              f"{gleiche_aufloesung} ähnliche Paare mit gleicher Auflösung bleiben beide erhalten\n")
    else:
        print("3/5 Erkennung unterschiedlicher Qualität übersprungen (--nur-exakte-duplikate)\n")

    # Gewinner -> ihre Verlierer
    verlierer_je_gewinner = defaultdict(list)
    for i_verlierer, i_gewinner in verlierer.items():
        verlierer_je_gewinner[i_gewinner].append(i_verlierer)

    # --- 4. Datum bestimmen und planen -----------------------------------
    plaene = []
    gewinner_indizes = [i for i in range(len(eintraege)) if i not in verlierer]
    print(f"4/5 Bestimme das Datum und plane die Ziele ({len(gewinner_indizes)} Dateien)...")
    datumscache = {}

    def datum_von(pfad, art):
        if pfad not in datumscache:
            datumscache[pfad] = bestimme_datum(pfad, art == "video")
        return datumscache[pfad]

    for nr, i in enumerate(gewinner_indizes, start=1):
        eintrag = eintraege[i]
        alle_pfade = list(eintrag["pfade"])
        for i_verlierer in verlierer_je_gewinner[i]:
            alle_pfade += eintraege[i_verlierer]["pfade"]
        # beste Datumsquelle unter allen Kopien/Fassungen (bei Gleichstand: der Vertreter selbst)
        kandidaten = [datum_von(p, eintrag["art"]) for p in alle_pfade]
        datum, quelle_text, rang = min(kandidaten, key=lambda k: k[2])
        plaene.append({"index": i, "eintrag": eintrag, "datum": datum, "datumsquelle": quelle_text, "rang": rang})
        if nr % 500 == 0:
            print(f"   ... {nr}/{len(gewinner_indizes)}")

    plaene.sort(key=lambda p: (p["datum"] or datetime.max, os.path.basename(p["eintrag"]["pfade"][0]).lower(),
                               p["eintrag"]["pfade"][0].lower()))
    belegt = set()
    for plan in plaene:
        vertreter = plan["eintrag"]["pfade"][0]
        verzeichnis = zielverzeichnis_fuer(ziel, plan["datum"])
        plan["ziel"], plan["vorhanden"] = ermittle_zielpfad(verzeichnis, os.path.basename(vertreter), vertreter, belegt)
        belegt.add(os.path.normcase(plan["ziel"]))

    zu_uebernehmen = [p for p in plaene if not p["vorhanden"]]
    benoetigt = sum(p["eintrag"]["groesse"] for p in zu_uebernehmen)
    frei = freier_speicher(ziel)
    print(f"    {len(zu_uebernehmen)} Dateien ({mb(benoetigt)}) sind neu am Ziel, "
          f"{len(plaene) - len(zu_uebernehmen)} liegen dort schon identisch\n")
    if not verschieben and benoetigt * 1.02 > frei:
        meldung = (f"Zu wenig freier Speicher am Ziel: benötigt ca. {mb(benoetigt)}, frei {mb(frei)}. "
                   f"Alternativ mit --verschieben arbeiten (dort wird nichts zusätzlich belegt, "
                   f"sofern Quelle und Ziel auf demselben Laufwerk liegen).")
        if not trockenlauf:
            print(f"ABBRUCH: {meldung}")
            return
        print(f"WARNUNG: {meldung}\n")

    # --- 5. Ausführen -----------------------------------------------------
    aktion_text = {"neu": ("WÜRDE VERSCHIEBEN" if verschieben else "WÜRDE KOPIEREN") if trockenlauf
                   else ("VERSCHOBEN" if verschieben else "KOPIERT")}
    modus = "TROCKENLAUF - es wird nichts verändert" if trockenlauf else ("VERSCHIEBEN" if verschieben else "KOPIEREN")
    print(f"5/5 Ausführen ({modus})...")
    gesamt = len(plaene)
    breite = len(str(gesamt))
    zaehler = Counter()
    datumsquellen = Counter()

    with open(protokoll_pfad, "w", encoding="utf-8") as protokoll:
        protokoll.write("Aktion\tQuelle\tZiel\tDatum\tDatumsquelle\tHinweis\n")

        def schreibe(aktion, quelle_pfad, ziel_pfad, datum, datumsquelle, hinweis=""):
            datum_text = f"{datum:%Y-%m-%d %H:%M:%S}" if datum else ""
            protokoll.write(f"{aktion}\t{quelle_pfad}\t{ziel_pfad}\t{datum_text}\t{datumsquelle}\t{hinweis}\n")
            protokoll.flush()

        for nr, plan in enumerate(plaene, start=1):
            eintrag = plan["eintrag"]
            vertreter = eintrag["pfade"][0]
            datumsquellen[plan["datumsquelle"]] += 1

            if plan["vorhanden"]:
                zaehler["bereits_vorhanden"] += 1
                print(f"{nr:>{breite}}/{gesamt} ÜBERSPRUNGEN (liegt schon identisch am Ziel): {vertreter}")
                schreibe("ÜBERSPRUNGEN (bereits vorhanden)", vertreter, plan["ziel"], plan["datum"], plan["datumsquelle"])
            else:
                try:
                    if not trockenlauf:
                        if verschieben:
                            verschiebe_sicher(vertreter, plan["ziel"])
                        else:
                            kopiere_sicher(vertreter, plan["ziel"], pruefen)
                    zaehler["uebernommen"] += 1
                    print(f"{nr:>{breite}}/{gesamt} {aktion_text['neu']}: {vertreter} -> {plan['ziel']}")
                    schreibe(aktion_text["neu"], vertreter, plan["ziel"], plan["datum"], plan["datumsquelle"])
                except Exception as fehler:
                    zaehler["fehler"] += 1
                    print(f"{nr:>{breite}}/{gesamt} FEHLER: {vertreter} ({fehler}) - Datei unverändert")
                    schreibe("FEHLER", vertreter, plan["ziel"], plan["datum"], plan["datumsquelle"], str(fehler))
                    continue  # bei Fehler: Duplikate dieser Datei nicht anfassen

            # Duplikate dieses Eintrags: exakte Kopien und schlechtere Fassungen
            duplikate = [(p, "Exakt", "DUPLIKAT (exakte Kopie)", f"identisch mit {vertreter}")
                         for p in eintrag["pfade"][1:]]
            for i_verlierer in verlierer_je_gewinner[plan["index"]]:
                v = eintraege[i_verlierer]
                hinweis = (f"schlechtere Fassung von {vertreter} "
                           f"({v['merkmale']['pixel']:,} statt {eintrag['merkmale']['pixel']:,} Pixel)").replace(",", ".")
                for pfad_dup in v["pfade"]:
                    duplikate.append((pfad_dup, "Aehnlich_geringere_Qualitaet",
                                      "DUPLIKAT (schlechtere Qualität)", hinweis))

            for pfad_dup, unterordner, aktion, hinweis in duplikate:
                zaehler["duplikate_exakt" if unterordner == "Exakt" else "duplikate_aehnlich"] += 1
                if verschieben and not trockenlauf:
                    try:
                        dup_ziel, _ = ermittle_zielpfad(os.path.join(ziel, "_Duplikate", unterordner),
                                                        os.path.basename(pfad_dup), pfad_dup, belegt,
                                                        auf_identisches_pruefen=False)
                        belegt.add(os.path.normcase(dup_ziel))
                        verschiebe_sicher(pfad_dup, dup_ziel)
                        schreibe(aktion + " -> _Duplikate", pfad_dup, dup_ziel, None, "", hinweis)
                    except Exception as fehler:
                        zaehler["fehler"] += 1
                        print(f"      FEHLER beim Aussortieren von {pfad_dup}: {fehler}")
                        schreibe("FEHLER", pfad_dup, "", None, "", str(fehler))
                else:
                    schreibe(aktion + " (nicht übernommen)", pfad_dup, "", None, "", hinweis)

        for pfad_fehler, text in fehlerliste:
            zaehler["fehler"] += 1
            schreibe("FEHLER", pfad_fehler, "", None, "", text)

        dauer = time.perf_counter() - start
        zusammenfassung = [
            "",
            f"ZUSAMMENFASSUNG ({modus})",
            f"Gefundene Fotos / Videos\t{len(fotos)} / {len(videos)}",
            f"Dateien im Ergebnis (jede Aufnahme nur einmal)\t{len(plaene)}",
            f"   {'würden übernommen' if trockenlauf else ('verschoben' if verschieben else 'kopiert')}\t{zaehler['uebernommen']}",
            f"   lagen schon identisch am Ziel (übersprungen)\t{zaehler['bereits_vorhanden']}",
            f"Aufgelöste Duplikate: exakte Kopien\t{zaehler['duplikate_exakt']}",
            f"Aufgelöste Duplikate: schlechtere Qualität einer Aufnahme\t{zaehler['duplikate_aehnlich']}",
            f"Ähnliche Fotos mit gleicher Auflösung (beide behalten)\t{gleiche_aufloesung}",
            f"Fehler (Datei unverändert)\t{zaehler['fehler']}",
            "Datum stammt aus ...",
        ]
        for quelle_text, anzahl in sorted(datumsquellen.items(), key=lambda x: RANG.get(x[0], KEIN_DATUM)):
            zusammenfassung.append(f"   {quelle_text}\t{anzahl}")
        zusammenfassung.append(f"Laufzeit in Sekunden\t{dauer:.1f}")
        protokoll.write("\n".join(zusammenfassung) + "\n")

    print("\n" + "\n".join(z.replace("\t", ": ") for z in zusammenfassung).strip())
    print(f"\nProtokoll gespeichert in: {protokoll_pfad}")
    if not trockenlauf and not verschieben:
        print("Die Originale wurden nicht verändert. Erst nach einer Stichprobe der neuen Ordnerstruktur "
              "den alten Ordner (von Hand) entfernen.")


# -----------------------------------------------------------------
# STARTPUNKT
# -----------------------------------------------------------------
if __name__ == "__main__":
    argumente = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    unbekannt = flags - {"--trockenlauf", "--verschieben", "--pruefen", "--nur-exakte-duplikate"}

    if len(argumente) != 2 or unbekannt:
        if unbekannt:
            print(f"Unbekannte Option(en): {', '.join(sorted(unbekannt))}")
        print("Aufruf: python fotos_sortieren.py <quellordner> <zielordner> "
              "[--trockenlauf] [--verschieben] [--pruefen] [--nur-exakte-duplikate]")
        sys.exit(1)

    quellordner, zielordner = argumente
    # Windows: "E:" allein bedeutet "aktueller Ordner auf E:" - gemeint ist das ganze Laufwerk
    if len(quellordner) == 2 and quellordner[1] == ":":
        quellordner += os.sep
    if not os.path.isdir(quellordner):
        print(f"FEHLER: Quellordner '{quellordner}' existiert nicht.")
        sys.exit(1)
    if os.path.normcase(os.path.abspath(quellordner)) == os.path.normcase(os.path.abspath(zielordner)):
        print("FEHLER: Quell- und Zielordner dürfen nicht derselbe Ordner sein.")
        sys.exit(1)

    trockenlauf = "--trockenlauf" in flags
    verschieben = "--verschieben" in flags
    if trockenlauf:
        print("TROCKENLAUF: Es wird nichts verändert.\n")
    elif verschieben:
        print("ACHTUNG: --verschieben ist aktiv. Die Originale werden verschoben (Duplikate nach "
              "_Duplikate, nichts wird gelöscht). Empfehlung: erst --trockenlauf und ein Backup.\n")
    else:
        print("Modus: KOPIEREN (Originale bleiben unverändert).\n")

    sortiere(quellordner, zielordner, trockenlauf, verschieben, "--pruefen" in flags,
             "--nur-exakte-duplikate" not in flags)
