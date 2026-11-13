"""
EXIF-DATUM NACHTRAGEN
=====================

Eigenständiges Programm (unabhängig von der Gesichtserkennung). Durchsucht
einen Ordner bzw. eine ganze Festplatte samt ALLER Unterordner nach Fotos
(.jpg, .jpeg, .png) und trägt bei Fotos OHNE EXIF-Aufnahmedatum das Datum
ein, das im Dateinamen steht (z.B. IMG-20170124-WA0000.jpg -> 24.01.2017).

Ablauf pro Foto (Reihenfolge = Priorität):
    1. Hat das Foto bereits ein EXIF-Datum?        -> nichts ändern.
    Volles Datum (JJJJ-MM-TT, JJJJ_MM_TT oder JJJJMMTT) im ...
    2. ... Dateinamen?                              -> ins EXIF schreiben.
    3. ... Namen des Ordners, in dem das Foto liegt? -> ins EXIF schreiben.
    4. ... Namen des Ordners darüber?               -> ins EXIF schreiben.
    Nur Jahr und Monat (JJJJ-MM, JJJJ_MM oder JJJJMM) im ... (Tag = 1.)
    5. ... Dateinamen, dann Ordnernamen, dann Namen des Ordners darüber
                                                    -> ins EXIF schreiben.
    6. Sonst: letztes Änderungsdatum der Datei     -> ins EXIF schreiben
       (nur, wenn plausibel: Jahr ab 1990, nicht in der Zukunft).
    7. Sonst                                        -> nichts ändern.
Bei den Punkten 2-5 wird die Uhrzeit 00:00:00 eingetragen (dort steht keine
Uhrzeit), bei Punkt 6 die Uhrzeit des Änderungsdatums. Bei Punkt 5 steht der
1. des Monats als Tag im EXIF - das ist nur eine grobe Annäherung.
Es zählen nur gültige, plausible Daten (Jahr ab 1990, nicht in der Zukunft,
Monat 01-12). Der Ordnername "2017-09 Fotos" ergibt also den 01.09.2017,
"2001" (nur ein Jahr) wird nicht erkannt.
Die Ordnernamen werden über den vollständigen Pfad ermittelt - also auch
dann, wenn der Ordner mit dem Datum oberhalb des angegebenen Startordners
liegt.

ACHTUNG zu den Punkten 3-6: Ein Ordnername (z.B. ein Ereignis-Datum) oder das
Änderungsdatum (z.B. Kopierdatum) ist nicht immer das echte Aufnahmedatum, und
alle Fotos eines Ordners bekommen dasselbe Datum. Nach dem Eintragen gilt es
als "echtes" EXIF-Datum und ist später nicht mehr von einem Aufnahmedatum zu
unterscheiden. In der Protokolldatei steht deshalb pro Foto die Quelle.

Wie die Dateien verändert werden:
    - Die Bilddaten (Pixel) werden NICHT angefasst und nicht neu
      komprimiert. Bei JPEG wird nur der EXIF-Block ersetzt, bei PNG ein
      EXIF-Block (eXIf) ergänzt.
    - Es wird zuerst eine Kopie mit dem neuen Inhalt erzeugt und geprüft
      (Bild öffnet sich, gleiche Größe, Datum lesbar). Erst dann ersetzt
      sie atomar das Original. Schlägt etwas fehl, bleibt das Original
      unverändert.
    - Änderungsdatum und Zugriffsrechte der Datei bleiben erhalten.

Ausgeschlossen werden typische System-/Papierkorb-Ordner (siehe
AUSGESCHLOSSENE_ORDNER), z.B. die Vorschaubilder-Ordner eines Synology-NAS.

Aufruf:
    python exif_datum_nachtragen.py <ordner_oder_laufwerk> [--trockenlauf]

Beispiele:
    python exif_datum_nachtragen.py D:\\Fotos --trockenlauf
    python exif_datum_nachtragen.py E:\\
    python exif_datum_nachtragen.py /volume1/photo

--trockenlauf: nur anzeigen/protokollieren, was geändert WÜRDE - es wird
               nichts verändert. Empfohlen als erster Lauf!

Es wird eine Protokolldatei (Tab-getrennt) im aktuellen Verzeichnis
angelegt: exif_protokoll_<Datum>_<Uhrzeit>.txt
"""

import os
import re
import sys
import time
import shutil
import struct
import zlib
from datetime import datetime

from PIL import Image


# -----------------------------------------------------------------
# EINSTELLUNGEN
# -----------------------------------------------------------------
ERLAUBTE_ENDUNGEN = (".jpg", ".jpeg", ".png")

# Diese Ordner werden samt Inhalt übersprungen (Groß-/Kleinschreibung egal).
AUSGESCHLOSSENE_ORDNER = {
    "@eadir",                    # Synology: automatisch erzeugte Vorschaubilder
    "#recycle", "@recycle",      # Papierkörbe von NAS-Systemen
    "$recycle.bin",              # Windows-Papierkorb
    "system volume information",  # Windows-Systemordner
    ".trash", ".trashes", ".thumbnails", ".@__thumb",
}

# --- Muster für Daten in Datei- und Ordnernamen ---------------------
# Alle Muster liefern die benannten Gruppen j (Jahr), m (Monat) und, beim
# vollen Datum, t (Tag). (?<!\d) und (?!\d) sorgen dafür, dass die Ziffern
# nicht Teil einer längeren Zahl sind. (?P=s) erzwingt ein einheitliches
# Trennzeichen (also nicht "2017-01_24").

# Volles Datum: JJJJ-MM-TT bzw. JJJJ_MM_TT
VOLLES_DATUM_GETRENNT = re.compile(
    r"(?<!\d)(?P<j>\d{4})(?P<s>[-_])(?P<m>\d{2})(?P=s)(?P<t>\d{2})(?!\d)")
# Volles Datum: JJJJMMTT
VOLLES_DATUM_KOMPAKT = re.compile(r"(?<!\d)(?P<j>\d{4})(?P<m>\d{2})(?P<t>\d{2})(?!\d)")
# JJJJMMTT im Dateinamen: dahinter dürfen weitere Ziffern folgen
# (z.B. 20230629110729 = JJJJMMTThhmmss)
VOLLES_DATUM_KOMPAKT_OFFEN = re.compile(r"(?<!\d)(?P<j>\d{4})(?P<m>\d{2})(?P<t>\d{2})")

# Nur Jahr und Monat: JJJJ-MM bzw. JJJJ_MM. Direkt dahinter darf kein
# weiteres Trennzeichen mit Ziffer folgen (sonst wäre es ein unvollständig/
# fehlerhaft geschriebenes volles Datum).
MONAT_GETRENNT = re.compile(r"(?<!\d)(?P<j>\d{4})(?P<s>[-_])(?P<m>\d{2})(?![-_]?\d)")
# Nur Jahr und Monat: JJJJMM (genau 6 Ziffern)
MONAT_KOMPAKT = re.compile(r"(?<!\d)(?P<j>\d{4})(?P<m>\d{2})(?!\d)")

# Soll JJJJMM (6 Ziffern ohne Trennzeichen) als Jahr+Monat gelten? Auf False
# stellen, falls Dateinamen oft andere 6-stellige Zahlen enthalten (z.B.
# Zähler wie 201505), die sonst fälschlich als Datum erkannt würden.
JAHR_MONAT_OHNE_TRENNZEICHEN = True

# EXIF-Feldnummern
TAG_DATETIME = 306            # im Hauptteil (IFD0)
TAG_EXIF_IFD = 0x8769         # Verweis auf den EXIF-Unterbereich
TAG_DATETIME_ORIGINAL = 36867  # eigentliches Aufnahmedatum
TAG_DATETIME_DIGITIZED = 36868

# Reihenfolge, in der nach einem vorhandenen Datum gesucht wird
EXIF_DATUMSFELDER = (TAG_DATETIME_ORIGINAL, TAG_DATETIME_DIGITIZED, TAG_DATETIME)

EXIF_DATUMSFORMAT = "%Y:%m:%d %H:%M:%S"
PNG_SIGNATUR = b"\x89PNG\r\n\x1a\n"


# -----------------------------------------------------------------
# DATUM LESEN
# -----------------------------------------------------------------
def lese_exif_datum(pfad):
    """Liefert das EXIF-Datum der Datei oder None (kein/unlesbares Datum)."""
    try:
        with Image.open(pfad) as bild:
            exif = bild.getexif()
            felder = dict(exif)                       # Hauptteil
            felder.update(exif.get_ifd(TAG_EXIF_IFD))  # EXIF-Unterbereich
    except Exception:
        return None

    for tag in EXIF_DATUMSFELDER:
        wert = felder.get(tag)
        if not wert:
            continue
        if isinstance(wert, bytes):
            wert = wert.decode("ascii", errors="ignore")
        try:
            return datetime.strptime(str(wert).strip("\x00 "), EXIF_DATUMSFORMAT)
        except ValueError:
            continue  # z.B. "0000:00:00 00:00:00" -> zählt als kein Datum
    return None


def _pruefe_datum(jahr, monat, tag=1):
    """Liefert ein gültiges, plausibles Datum (ab 1990, nicht in der Zukunft) oder None."""
    try:
        datum = datetime(int(jahr), int(monat), int(tag))
    except ValueError:
        return None  # z.B. Monat 13 oder Tag 45
    if 1990 <= datum.year <= datetime.now().year and datum <= datetime.now():
        return datum
    return None


def _erstes_gueltiges_datum(text, muster_liste):
    """
    Durchsucht 'text' mit allen Mustern und liefert das gültige Datum, dessen
    Fundstelle im Text am weitesten vorne steht. Fehlt die Gruppe t (Tag),
    gilt der 1. des Monats.
    """
    funde = []
    for muster in muster_liste:
        for treffer in muster.finditer(text):
            tag = treffer.groupdict().get("t") or 1
            funde.append((treffer.start(), treffer.group("j"), treffer.group("m"), tag))

    for _stelle, jahr, monat, tag in sorted(funde, key=lambda fund: fund[0]):
        datum = _pruefe_datum(jahr, monat, tag)
        if datum is not None:
            return datum
    return None


def lese_datum_aus_dateiname(dateiname):
    """
    Volles Datum im Dateinamen: JJJJ-MM-TT, JJJJ_MM_TT oder JJJJMMTT,
    z.B. "2017-01-24_Foto.jpg" oder "IMG-20170124-WA0000.jpg".
    """
    return _erstes_gueltiges_datum(dateiname, [VOLLES_DATUM_GETRENNT, VOLLES_DATUM_KOMPAKT_OFFEN])


def lese_monat_aus_text(text):
    """
    Nur Jahr und Monat (JJJJ-MM, JJJJ_MM, optional JJJJMM) in einem Datei- oder
    Ordnernamen, z.B. "2017-09 Fotos". Ergebnis: der 1. dieses Monats.
    """
    muster = [MONAT_GETRENNT]
    if JAHR_MONAT_OHNE_TRENNZEICHEN:
        muster.append(MONAT_KOMPAKT)
    return _erstes_gueltiges_datum(text, muster)


def lese_aenderungsdatum(pfad):
    """
    Letztes Änderungsdatum der Datei (mit Uhrzeit, ohne Mikrosekunden).
    Gibt None zurück, wenn es nicht lesbar oder unplausibel ist
    (vor 1990, z.B. 01.01.1970/1980 als Platzhalter, oder in der Zukunft).
    """
    try:
        datum = datetime.fromtimestamp(os.path.getmtime(pfad)).replace(microsecond=0)
    except (OSError, OverflowError, ValueError):
        return None
    if 1990 <= datum.year <= datetime.now().year and datum <= datetime.now():
        return datum
    return None


def lese_datum_aus_ordnername(ordnername):
    """
    Volles Datum im Namen eines Ordners: JJJJ-MM-TT, JJJJ_MM_TT oder JJJJMMTT,
    z.B. "2017-01-24 Urlaub".
    """
    return _erstes_gueltiges_datum(ordnername, [VOLLES_DATUM_GETRENNT, VOLLES_DATUM_KOMPAKT])


def ermittle_neues_datum(pfad):
    """
    Bestimmt das einzutragende Datum nach Priorität:
      volles Datum:  Dateiname > Ordner > Ordner darüber
      nur Jahr/Monat: Dateiname > Ordner > Ordner darüber
      zuletzt: Änderungsdatum
    Rückgabe: (datum, quelle, detail) oder (None, None, None);
    'detail' ist der Name, aus dem das Datum stammt (leer beim Änderungsdatum).
    """
    absoluter_pfad = os.path.abspath(pfad)
    dateiname = os.path.basename(absoluter_pfad)
    ordner = os.path.dirname(absoluter_pfad)
    ordnername = os.path.basename(ordner)
    oberordnername = os.path.basename(os.path.dirname(ordner))

    suchschritte = [
        ("Dateiname", dateiname, lese_datum_aus_dateiname),
        ("Ordner", ordnername, lese_datum_aus_ordnername),
        ("Ordner darüber", oberordnername, lese_datum_aus_ordnername),
        ("Dateiname (nur Jahr/Monat)", dateiname, lese_monat_aus_text),
        ("Ordner (nur Jahr/Monat)", ordnername, lese_monat_aus_text),
        ("Ordner darüber (nur Jahr/Monat)", oberordnername, lese_monat_aus_text),
    ]
    for quelle, text, suchfunktion in suchschritte:
        datum = suchfunktion(text)
        if datum is not None:
            return datum, quelle, text

    datum = lese_aenderungsdatum(pfad)
    if datum is not None:
        return datum, "Änderungsdatum", ""

    return None, None, None


# -----------------------------------------------------------------
# EXIF-DATEN ZUSAMMENBAUEN
# -----------------------------------------------------------------
def baue_exif_bytes(pfad, datum):
    """
    Liest vorhandene EXIF-Daten der Datei (falls welche da sind), setzt
    die drei Datumsfelder und liefert alles als EXIF-Bytes.
    """
    text = datum.strftime(EXIF_DATUMSFORMAT)
    with Image.open(pfad) as bild:
        exif = bild.getexif()
        exif[TAG_DATETIME] = text
        exif_bereich = exif.get_ifd(TAG_EXIF_IFD)
        exif_bereich[TAG_DATETIME_ORIGINAL] = text
        exif_bereich[TAG_DATETIME_DIGITIZED] = text
        return exif.tobytes()


# -----------------------------------------------------------------
# JPEG: EXIF-Block ersetzen, Bilddaten unverändert lassen
# -----------------------------------------------------------------
def jpeg_mit_exif(daten, exif_bytes):
    if not daten.startswith(b"\xff\xd8"):
        raise ValueError("keine gültige JPEG-Datei")
    if not exif_bytes.startswith(b"Exif\x00\x00"):
        exif_bytes = b"Exif\x00\x00" + exif_bytes
    if len(exif_bytes) + 2 > 65535:
        raise ValueError("EXIF-Block zu groß für eine JPEG-Datei")

    neues_segment = b"\xff\xe1" + struct.pack(">H", len(exif_bytes) + 2) + exif_bytes

    # Kopfbereich in einzelne Segmente zerlegen (bis die Bilddaten beginnen)
    segmente = []
    pos = 2
    while pos + 4 <= len(daten):
        if daten[pos] != 0xFF:
            raise ValueError("JPEG-Struktur nicht lesbar")
        marker = daten[pos + 1]
        if marker == 0xFF:      # Füllbyte
            pos += 1
            continue
        if marker in (0xDA, 0xD9):  # Bilddaten bzw. Ende beginnen
            break
        laenge = struct.unpack(">H", daten[pos + 2:pos + 4])[0]
        segmente.append((marker, daten[pos:pos + 2 + laenge]))
        pos += 2 + laenge
    rest = daten[pos:]

    # Alte EXIF-Segmente entfernen (APP1 mit "Exif"-Kennung)
    behalten = [(m, s) for m, s in segmente
                if not (m == 0xE1 and s[4:10] == b"Exif\x00\x00")]

    ausgabe = [b"\xff\xd8"]
    eingefuegt = False
    for index, (marker, segment) in enumerate(behalten):
        ausgabe.append(segment)
        if index == 0 and marker == 0xE0:  # JFIF-Kopf bleibt ganz vorne
            ausgabe.append(neues_segment)
            eingefuegt = True
    if not eingefuegt:
        ausgabe.insert(1, neues_segment)
    ausgabe.append(rest)
    return b"".join(ausgabe)


# -----------------------------------------------------------------
# PNG: EXIF-Block (eXIf) ersetzen/ergänzen, Bilddaten unverändert
# -----------------------------------------------------------------
def _png_chunk(typ, inhalt):
    pruefsumme = zlib.crc32(typ + inhalt) & 0xFFFFFFFF
    return struct.pack(">I", len(inhalt)) + typ + inhalt + struct.pack(">I", pruefsumme)


def png_mit_exif(daten, exif_bytes):
    if not daten.startswith(PNG_SIGNATUR):
        raise ValueError("keine gültige PNG-Datei")
    if exif_bytes.startswith(b"Exif\x00\x00"):
        exif_bytes = exif_bytes[6:]  # PNG speichert EXIF ohne diese Kennung

    neuer_chunk = _png_chunk(b"eXIf", exif_bytes)

    ausgabe = [PNG_SIGNATUR]
    eingefuegt = False
    pos = len(PNG_SIGNATUR)
    while pos < len(daten):
        laenge = struct.unpack(">I", daten[pos:pos + 4])[0]
        typ = daten[pos + 4:pos + 8]
        ende = pos + 12 + laenge
        if ende > len(daten):
            raise ValueError("PNG-Datei ist abgeschnitten")
        chunk = daten[pos:ende]
        pos = ende

        if typ == b"eXIf":  # vorhandenen EXIF-Block ersetzen
            continue
        if typ == b"IDAT" and not eingefuegt:
            ausgabe.append(neuer_chunk)
            eingefuegt = True
        ausgabe.append(chunk)

    if not eingefuegt:
        raise ValueError("PNG enthält keine Bilddaten")
    return b"".join(ausgabe)


# -----------------------------------------------------------------
# DATEI SICHER ÜBERSCHREIBEN
# -----------------------------------------------------------------
def schreibe_datum_in_datei(pfad, datum):
    """
    Schreibt das Datum in die EXIF-Daten der Datei. Das Original wird erst
    ersetzt, nachdem eine Kopie erfolgreich erstellt und geprüft wurde.
    Löst bei jedem Problem eine Exception aus - das Original bleibt dann
    unverändert.
    """
    with open(pfad, "rb") as f:
        daten = f.read()

    exif_bytes = baue_exif_bytes(pfad, datum)

    if pfad.lower().endswith((".jpg", ".jpeg")):
        neue_daten = jpeg_mit_exif(daten, exif_bytes)
    else:
        neue_daten = png_mit_exif(daten, exif_bytes)

    temp_pfad = pfad + ".exif_tmp"
    try:
        with open(temp_pfad, "wb") as f:
            f.write(neue_daten)

        # Kontrolle: Bild öffnet sich, gleiche Größe, Datum jetzt lesbar
        with Image.open(pfad) as alt, Image.open(temp_pfad) as neu:
            if alt.size != neu.size:
                raise ValueError("Kontrolle fehlgeschlagen: Bildgröße weicht ab")
        if lese_exif_datum(temp_pfad) != datum:
            raise ValueError("Kontrolle fehlgeschlagen: Datum nicht lesbar")

        shutil.copystat(pfad, temp_pfad)  # Änderungsdatum + Zugriffsrechte übernehmen
        os.replace(temp_pfad, pfad)
    except Exception:
        if os.path.exists(temp_pfad):
            os.remove(temp_pfad)
        raise


# -----------------------------------------------------------------
# ORDNER DURCHSUCHEN
# -----------------------------------------------------------------
def sammle_bilddateien(wurzel):
    """Alle .jpg/.jpeg/.png in 'wurzel' und allen Unterordnern."""
    treffer = []

    def ordner_nicht_lesbar(fehler):
        print(f"HINWEIS: Ordner nicht lesbar und übersprungen: {fehler.filename}")

    for ordner, unterordner, dateien in os.walk(wurzel, onerror=ordner_nicht_lesbar):
        unterordner[:] = [u for u in unterordner if u.lower() not in AUSGESCHLOSSENE_ORDNER]
        for name in dateien:
            if name.startswith("._"):  # macOS-Begleitdateien, keine echten Bilder
                continue
            if name.lower().endswith(ERLAUBTE_ENDUNGEN):
                treffer.append(os.path.join(ordner, name))

    return treffer


QUELLE_ZU_ZAEHLER = {
    "Dateiname": "aus_dateiname",
    "Ordner": "aus_ordner",
    "Ordner darüber": "aus_oberordner",
    "Dateiname (nur Jahr/Monat)": "aus_dateiname_monat",
    "Ordner (nur Jahr/Monat)": "aus_ordner_monat",
    "Ordner darüber (nur Jahr/Monat)": "aus_oberordner_monat",
    "Änderungsdatum": "aus_aenderungsdatum",
}


def verarbeite(wurzel, trockenlauf):
    start = time.perf_counter()
    zeitstempel = datetime.now().strftime("%Y%m%d_%H%M%S")
    protokoll_pfad = f"exif_protokoll_{zeitstempel}.txt"
    laufende_nummer = 2
    while os.path.exists(protokoll_pfad):  # ein vorhandenes Protokoll nie überschreiben
        protokoll_pfad = f"exif_protokoll_{zeitstempel}_{laufende_nummer}.txt"
        laufende_nummer += 1

    print(f"Suche Fotos in: {wurzel}")
    dateien = sammle_bilddateien(wurzel)
    gesamt = len(dateien)
    breite = len(str(gesamt))
    print(f"{gesamt} Foto(s) gefunden.\n")

    zaehler = {"hat_datum": 0, "kein_datum": 0, "fehler": 0}
    for name in QUELLE_ZU_ZAEHLER.values():
        zaehler[name] = 0
    aktion_text = "WÜRDE SCHREIBEN" if trockenlauf else "GESCHRIEBEN"

    with open(protokoll_pfad, "w", encoding="utf-8") as protokoll:
        protokoll.write("Pfad\tErgebnis\tDatum\tQuelle\tHinweis\n")

        for nr, pfad in enumerate(dateien, start=1):
            if lese_exif_datum(pfad) is not None:
                zaehler["hat_datum"] += 1
            else:
                datum, quelle, detail = ermittle_neues_datum(pfad)
                if datum is None:
                    zaehler["kein_datum"] += 1
                else:
                    datum_text = f"{datum:%Y-%m-%d %H:%M:%S}"
                    quelle_text = f"{quelle}: {detail}" if detail else quelle
                    try:
                        if not trockenlauf:
                            schreibe_datum_in_datei(pfad, datum)
                        zaehler[QUELLE_ZU_ZAEHLER[quelle]] += 1
                        print(f"{nr:>{breite}}/{gesamt} {aktion_text}: {pfad} -> {datum_text} [{quelle_text}]")
                        protokoll.write(f"{pfad}\t{aktion_text}\t{datum_text}\t{quelle}\t{detail}\n")
                    except Exception as fehler:
                        zaehler["fehler"] += 1
                        print(f"{nr:>{breite}}/{gesamt} FEHLER: {pfad} ({fehler}) - Datei unverändert")
                        protokoll.write(f"{pfad}\tFEHLER\t{datum_text}\t{quelle}\t{fehler}\n")

            if nr % 1000 == 0:
                print(f"... {nr:>{breite}}/{gesamt} geprüft")
            protokoll.flush()

        dauer = time.perf_counter() - start
        ergaenzt = sum(zaehler[name] for name in QUELLE_ZU_ZAEHLER.values())
        zusammenfassung = [
            "",
            "ZUSAMMENFASSUNG" + (" (TROCKENLAUF - nichts wurde verändert)" if trockenlauf else ""),
            f"Geprüfte Fotos\t{gesamt}",
            f"Hatten bereits ein EXIF-Datum (unverändert)\t{zaehler['hat_datum']}",
            f"Gar kein verwendbares Datum gefunden (unverändert)\t{zaehler['kein_datum']}",
            f"{'Würden ergänzt werden' if trockenlauf else 'Ergänzt'} (gesamt)\t{ergaenzt}",
            f"   davon volles Datum aus dem Dateinamen\t{zaehler['aus_dateiname']}",
            f"   davon volles Datum aus dem Ordnernamen\t{zaehler['aus_ordner']}",
            f"   davon volles Datum aus dem Namen des Ordners darüber\t{zaehler['aus_oberordner']}",
            f"   davon nur Jahr/Monat (Tag = 1) aus dem Dateinamen\t{zaehler['aus_dateiname_monat']}",
            f"   davon nur Jahr/Monat (Tag = 1) aus dem Ordnernamen\t{zaehler['aus_ordner_monat']}",
            f"   davon nur Jahr/Monat (Tag = 1) aus dem Namen des Ordners darüber\t{zaehler['aus_oberordner_monat']}",
            f"   davon Datum aus dem Änderungsdatum\t{zaehler['aus_aenderungsdatum']}",
            f"Fehler (Datei unverändert)\t{zaehler['fehler']}",
            f"Laufzeit in Sekunden\t{dauer:.1f}",
        ]
        protokoll.write("\n".join(zusammenfassung) + "\n")

    print("\n" + "\n".join(z.replace("\t", ": ") for z in zusammenfassung).strip())
    print(f"\nProtokoll gespeichert in: {protokoll_pfad}")


# -----------------------------------------------------------------
# STARTPUNKT
# -----------------------------------------------------------------
if __name__ == "__main__":
    argumente = [a for a in sys.argv[1:] if not a.startswith("--")]
    trockenlauf = "--trockenlauf" in sys.argv

    if len(argumente) != 1:
        print("Aufruf: python exif_datum_nachtragen.py <ordner_oder_laufwerk> [--trockenlauf]")
        sys.exit(1)

    wurzel = argumente[0]
    # Windows: "E:" allein bedeutet "aktueller Ordner auf E:" - gemeint ist das ganze Laufwerk
    if len(wurzel) == 2 and wurzel[1] == ":":
        wurzel += os.sep

    if not os.path.isdir(wurzel):
        print(f"FEHLER: '{wurzel}' ist kein vorhandener Ordner bzw. kein erreichbares Laufwerk.")
        sys.exit(1)

    if trockenlauf:
        print("TROCKENLAUF: Es wird nichts verändert.\n")
    else:
        print("ACHTUNG: Fotos ohne EXIF-Datum werden dauerhaft verändert (EXIF-Datum aus dem "
              "Dateinamen, sonst aus Ordnernamen, sonst aus dem Änderungsdatum der Datei). "
              "Empfehlung: erst mit --trockenlauf prüfen und ein Backup haben.\n")

    verarbeite(wurzel, trockenlauf)
