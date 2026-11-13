"""
AUSWERTUNG DER GESICHTSERKENNUNG
================================

Vergleicht in einer Excel-Tabelle die manuellen Angaben ("so ist es wirklich")
mit dem Ergebnis des Programms und zeigt, wo und warum es noch hakt.

ERWARTETES TABELLENLAYOUT (wie in 'Export_für_Claude.xlsx'):
    Zeile 1: Überschriften "Manuell" (über den Personenspalten der manuellen
             Angaben) und "Programm" (über den Personenspalten des Programms)
    Zeile 2: Personennamen unter "Manuell" und unter "Programm" - in gleicher
             Reihenfolge und Schreibweise
    ab Zeile 3: Spalte A = Fotoname, Spalte B = Aufnahmedatum (optional),
             in den Personenspalten ein "X" = Person ist drauf / wurde erkannt
    Eingefärbte Zellen in Spalte A = "Person schwer erkennbar": Diese Fotos
    werden bei der Hauptauswertung ("bewertbar") nicht mitgezählt, aber
    zusätzlich getrennt ausgewiesen.

Ein Foto zählt als "komplett richtig", wenn für JEDE Person Programm und
manuelle Angabe übereinstimmen - auch "niemand von uns drauf" und "Programm
findet niemanden" ist also richtig.

Aufruf:
    python auswertung_erkennung.py Export_für_Claude.xlsx
    python auswertung_erkennung.py Export_für_Claude.xlsx foto_abstaende.txt

Mit der zweiten Datei (foto_abstaende.txt, wird vom Tagging-Programm erzeugt)
erklärt der Bericht zusätzlich, WARUM Personen verpasst oder falsch erkannt
wurden, und probiert aus, welche Einstellungen (Schwellenwert je Person,
Mindestabstand zum Zweitbesten, Mindestgröße eines Gesichts) mehr Fotos
komplett richtig machen würden - ohne die Gesichtserkennung neu laufen zu
lassen. Die Abstandsdatei muss aus dem Lauf stammen, dessen Ergebnis in den
"Programm"-Spalten der Tabelle steht.

Voraussetzung (einmalig):
    python -m pip install openpyxl

Ausgabe: Bildschirm sowie die Dateien
    auswertung_bericht.txt       (derselbe Bericht als Text)
    auswertung_fehlerliste.txt   (Tab-getrennt: jedes nicht komplett richtige Foto;
                                  mit Abstandsdatei zusätzlich mit Erklärung)
"""

import re
import statistics
import sys
from collections import Counter
from datetime import datetime

try:
    from openpyxl import load_workbook
except ImportError:
    print("Das Paket 'openpyxl' fehlt. Installieren mit:  python -m pip install openpyxl")
    sys.exit(1)


# -----------------------------------------------------------------
# EINSTELLUNGEN
# -----------------------------------------------------------------
# Epochen für die Auswertung nach Aufnahmejahr: (Bezeichnung, von_Jahr, bis_Jahr)
EPOCHEN = [
    ("bis 2012", 0, 2012),
    ("2013-2019", 2013, 2019),
    ("ab 2020", 2020, 9999),
]

# Namen im Tagging-Programm -> Namen in der Excel-Tabelle (falls abweichend)
try:
    from einstellungen_privat import NAMENS_ZUORDNUNG   # z.B. {"Clara": "Cla"}
except ImportError:
    NAMENS_ZUORDNUNG = {}

# Werte, die bei der Simulation ausprobiert werden
TOLERANZ_GITTER = [round(0.40 + 0.02 * i, 2) for i in range(16)]   # 0,40 ... 0,70
MARGIN_GITTER = [0.0, 0.02, 0.04, 0.06, 0.08, 0.10]
HOEHE_GITTER = [0, 40, 60, 80, 100, 120, 150]
STANDARD_TOLERANZ = 0.52       # Annahme für den ausgewerteten Lauf
MAX_GESICHTER_EXAKT = 12       # wie im Tagging-Programm

# Datum im Dateinamen: JJJJ-MM-TT, JJJJ_MM_TT oder JJJJMMTT
DATUM_IM_NAMEN = re.compile(r"(?<!\d)(\d{4})([-_]?)(\d{2})\2(\d{2})")


# -----------------------------------------------------------------
# EINLESEN
# -----------------------------------------------------------------
def finde_block(zeile1, stichwort):
    """Spalte (1-basiert), unter der der Block mit der Überschrift 'stichwort' beginnt."""
    for zelle in zeile1:
        if isinstance(zelle.value, str) and zelle.value.strip().lower() == stichwort:
            return zelle.column
    raise ValueError(f"In Zeile 1 wurde keine Überschrift '{stichwort}' gefunden.")


def lese_namen(ws, startspalte):
    """Personennamen in Zeile 2, ab 'startspalte' bis zur ersten leeren Zelle."""
    namen = []
    spalte = startspalte
    while ws.cell(row=2, column=spalte).value not in (None, ""):
        namen.append(str(ws.cell(row=2, column=spalte).value).strip())
        spalte += 1
    return namen


def datum_aus_namen(name):
    for jahr, _trenner, monat, tag in DATUM_IM_NAMEN.findall(name):
        try:
            datum = datetime(int(jahr), int(monat), int(tag))
        except ValueError:
            continue
        if 1990 <= datum.year <= datetime.now().year:
            return datum
    return None


def datum_aus_zelle(wert):
    if isinstance(wert, datetime):
        return wert
    if isinstance(wert, str):
        for muster in ("%d.%m.%Y", "%Y-%m-%d", "%d.%m.%y"):
            try:
                return datetime.strptime(wert.strip(), muster)
            except ValueError:
                pass
    return None


def lese_tabelle(pfad):
    # Nicht 'read_only', weil die Zellfarben (Markierungen) gebraucht werden.
    ws = load_workbook(pfad).active

    start_soll = finde_block(ws[1], "manuell")
    start_ist = finde_block(ws[1], "programm")
    namen = lese_namen(ws, start_soll)
    if namen != lese_namen(ws, start_ist):
        raise ValueError("Die Personennamen unter 'Manuell' und 'Programm' stimmen nicht überein: "
                         f"{namen} <-> {lese_namen(ws, start_ist)}")

    fotos = []
    for zeile in range(3, ws.max_row + 1):
        name = ws.cell(row=zeile, column=1).value
        if name is None or str(name).strip() == "":
            continue
        name = str(name).strip()
        datum = datum_aus_zelle(ws.cell(row=zeile, column=2).value) or datum_aus_namen(name)
        fotos.append({
            "zeile": zeile,
            "name": name,
            "datum": datum,
            "markiert": ws.cell(row=zeile, column=1).fill.fill_type is not None,
            "soll": {p: ws.cell(row=zeile, column=start_soll + i).value == "X"
                     for i, p in enumerate(namen)},
            "ist": {p: ws.cell(row=zeile, column=start_ist + i).value == "X"
                    for i, p in enumerate(namen)},
        })
    return namen, fotos


# -----------------------------------------------------------------
# KENNZAHLEN
# -----------------------------------------------------------------
def ist_komplett_richtig(foto):
    return foto["soll"] == foto["ist"]


def verpasst(foto, namen):
    return [p for p in namen if foto["soll"][p] and not foto["ist"][p]]


def falsch_dazu(foto, namen):
    return [p for p in namen if foto["ist"][p] and not foto["soll"][p]]


def fehlerart(foto, namen):
    v, f = verpasst(foto, namen), falsch_dazu(foto, namen)
    if not v and not f:
        return "komplett richtig"
    if v and not f:
        return "nur verpasst (Person im Foto, nicht gefunden)"
    if f and not v:
        return "nur falsch dazu (Person erkannt, die nicht drauf ist)"
    return "verpasst UND falsch dazu"


def prozent(teil, gesamt):
    if gesamt == 0:
        return "   -  "
    return f"{100 * teil / gesamt:5.1f} %".replace(".", ",")


def dateiart(name):
    if re.match(r"IMG-\d{8}-WA", name):
        return "WhatsApp (IMG-JJJJMMTT-WA…)"
    if re.match(r"\d{8}_\d{6}", name):
        return "Handy-Zeitstempel (JJJJMMTT_hhmmss)"
    return "Kamera/sonstige Namen"


def epoche_von(foto):
    if foto["datum"] is None:
        return "Datum unbekannt"
    for bezeichnung, von, bis in EPOCHEN:
        if von <= foto["datum"].year <= bis:
            return bezeichnung
    return "Datum unbekannt"


# -----------------------------------------------------------------
# BERICHT
# -----------------------------------------------------------------
def tabelle_je_person(fotos, namen, titel):
    zeilen = [f"{titel} ({len(fotos)} Fotos)",
              f"   {'Person':<10}{'im Foto':>8}{'gefunden':>9}{'verpasst':>9}{'falsch dazu':>12}"
              f"{'Recall':>10}{'Precision':>11}"]
    for p in namen:
        tp = sum(f["soll"][p] and f["ist"][p] for f in fotos)
        fn = sum(f["soll"][p] and not f["ist"][p] for f in fotos)
        fp = sum(f["ist"][p] and not f["soll"][p] for f in fotos)
        zeilen.append(f"   {p:<10}{tp + fn:>8}{tp:>9}{fn:>9}{fp:>12}"
                      f"{prozent(tp, tp + fn):>10}{prozent(tp, tp + fp):>11}")
    return zeilen


def gruppiere(fotos, schluessel_funktion, reihenfolge=None):
    gruppen = {}
    for foto in fotos:
        gruppen.setdefault(schluessel_funktion(foto), []).append(foto)
    schluessel = reihenfolge if reihenfolge else sorted(gruppen)
    return [(s, gruppen[s]) for s in schluessel if s in gruppen]


def baue_bericht(pfad, namen, fotos):
    bewertbar = [f for f in fotos if not f["markiert"]]
    markiert = [f for f in fotos if f["markiert"]]
    ok_bew = sum(ist_komplett_richtig(f) for f in bewertbar)
    ok_alle = sum(ist_komplett_richtig(f) for f in fotos)
    ok_mark = sum(ist_komplett_richtig(f) for f in markiert)

    z = []
    z.append(f"AUSWERTUNG der Gesichtserkennung   (Datei: {pfad})")
    z.append(f"{len(fotos)} Fotos, davon {len(markiert)} als 'schwer erkennbar' markiert "
             f"-> {len(bewertbar)} bewertbar")
    z.append("")

    z.append("1. GESAMT: Foto komplett richtig (alle Personen stimmen)")
    z.append(f"   bewertbare Fotos (ohne Markierte): {ok_bew:>4} von {len(bewertbar):<4} = {prozent(ok_bew, len(bewertbar)).strip()}")
    z.append(f"   alle Fotos:                        {ok_alle:>4} von {len(fotos):<4} = {prozent(ok_alle, len(fotos)).strip()}")
    z.append(f"   nur die markierten Fotos:          {ok_mark:>4} von {len(markiert):<4} = {prozent(ok_mark, len(markiert)).strip()}")
    z.append("")

    z.append("2. JE PERSON")
    z.append("   (Recall = Anteil der tatsächlich abgebildeten Person, die gefunden wurde;")
    z.append("    Precision = Anteil der Programm-Treffer, die stimmen)")
    z += tabelle_je_person(bewertbar, namen, "   a) bewertbare Fotos")
    z.append("")
    z += tabelle_je_person(fotos, namen, "   b) alle Fotos")
    z.append("")
    z += tabelle_je_person(markiert, namen, "   c) nur markierte (schwer erkennbare) Fotos")
    z.append("")

    z.append("3. FEHLERARTEN (bewertbare Fotos)")
    arten = Counter(fehlerart(f, namen) for f in bewertbar)
    for art, anzahl in arten.most_common():
        z.append(f"   {art:<58}{anzahl:>4}  ({prozent(anzahl, len(bewertbar)).strip()})")
    z.append("")

    z.append("4. NACH AUFNAHMEJAHR (bewertbare Fotos): Foto komplett richtig")
    reihenfolge = [e[0] for e in EPOCHEN] + ["Datum unbekannt"]
    for epoche, gruppe in gruppiere(bewertbar, epoche_von, reihenfolge):
        ok = sum(ist_komplett_richtig(f) for f in gruppe)
        mit = [f for f in gruppe if any(f["soll"].values())]
        ok_mit = sum(ist_komplett_richtig(f) for f in mit)
        z.append(f"   {epoche:<16}{ok:>4} von {len(gruppe):<4}= {prozent(ok, len(gruppe)).strip():<9}"
                 f" (nur Fotos mit Familienmitglied: {ok_mit} von {len(mit)})")
    z.append("   Anteil markierter (schwer erkennbarer) Fotos je Epoche:")
    for epoche, gruppe in gruppiere(fotos, epoche_von, reihenfolge):
        anzahl_mark = sum(f["markiert"] for f in gruppe)
        z.append(f"   {epoche:<16}{anzahl_mark:>4} von {len(gruppe):<4}= {prozent(anzahl_mark, len(gruppe)).strip()}")
    z.append("")

    z.append("5. NACH DATEIART (bewertbare Fotos): Foto komplett richtig")
    for art, gruppe in gruppiere(bewertbar, lambda f: dateiart(f["name"])):
        ok = sum(ist_komplett_richtig(f) for f in gruppe)
        mit_fp = sum(bool(falsch_dazu(f, namen)) for f in gruppe)
        mit_fn = sum(bool(verpasst(f, namen)) for f in gruppe)
        z.append(f"   {art:<38}{ok:>4} von {len(gruppe):<4}= {prozent(ok, len(gruppe)).strip():<9}"
                 f" (Fotos mit falsch Dazu: {mit_fp}, mit Verpasstem: {mit_fn})")
    z.append("")

    leer = [f for f in bewertbar if not any(f["soll"].values())]
    leer_fehl = [f for f in leer if any(f["ist"].values())]
    z.append("6. FOTOS OHNE FAMILIENMITGLIED (bewertbare Fotos)")
    z.append(f"   {len(leer)} Fotos, auf denen manuell niemand von uns zu sehen ist; "
             f"das Programm fand dort fälschlich jemanden bei {len(leer_fehl)} "
             f"({prozent(len(leer_fehl), len(leer)).strip()})")
    z.append("")

    fehlerhaft = [f for f in bewertbar if not ist_komplett_richtig(f)]
    fehlerhaft_alle = [f for f in fotos if not ist_komplett_richtig(f)]
    z.append(f"7. FEHLERLISTE: {len(fehlerhaft)} bewertbare Fotos nicht komplett richtig")
    z.append(f"   Die Datei auswertung_fehlerliste.txt enthält alle {len(fehlerhaft_alle)} nicht komplett richtigen")
    z.append("   Fotos; die markierten sind in der Spalte 'Markiert' mit 'ja' gekennzeichnet.")
    return z


# -----------------------------------------------------------------
# ABSTANDSDATEI (foto_abstaende.txt) EINLESEN
# -----------------------------------------------------------------
def stamm(dateiname):
    """Dateiname ohne Bildendung, kleingeschrieben - zum Zuordnen von Tabelle und Abstandsdatei."""
    name = str(dateiname).strip()
    if name.lower().endswith((".jpg", ".jpeg", ".png")):
        name = name.rsplit(".", 1)[0]
    return name.strip().lower()


def lese_abstandsdatei(pfad, namen):
    """
    Rückgabe: (gesichter, rueck, hinweis)
      gesichter: {foto_stamm: [ {"hoehe", "bildhoehe", "abstaende": {Excel-Name: Abstand|None}} ]}
      rueck:     {Excel-Name: Name im Tagging-Programm} (für die Empfehlung am Ende)
      hinweis:   Text, falls die Namen nur der Position nach zugeordnet werden konnten
    """
    with open(pfad, encoding="utf-8") as f:
        zeilen = [z.rstrip("\r\n") for z in f if z.strip()]
    kopf = zeilen[0].split("\t")
    spalten = [(i, k[len("Abstand_"):]) for i, k in enumerate(kopf) if k.startswith("Abstand_")]
    zuordnung = {i: NAMENS_ZUORDNUNG.get(n, n) for i, n in spalten}

    hinweis = None
    if sorted(zuordnung.values()) != sorted(namen):
        if len(spalten) != len(namen):
            raise ValueError("Die Personen in der Abstandsdatei "
                             f"({[n for _, n in spalten]}) passen nicht zur Tabelle ({namen}). "
                             "Ggf. NAMENS_ZUORDNUNG oben im Programm anpassen.")
        zuordnung = {i: namen[position] for position, (i, _n) in enumerate(spalten)}
        hinweis = ("Hinweis: Die Namen in der Abstandsdatei passen nicht zur Tabelle und wurden "
                   "der Reihenfolge nach zugeordnet. Ggf. NAMENS_ZUORDNUNG oben im Programm anpassen.")
    rueck = {zuordnung[i]: n for i, n in spalten}

    idx_name = kopf.index("Dateiname")
    idx_hoehe = kopf.index("Gesichtshoehe_px")
    idx_bild = kopf.index("Bildhoehe_px")
    gesichter = {}
    for zeile in zeilen[1:]:
        z = zeile.split("\t")
        abstaende = {}
        for i, _n in spalten:
            wert = z[i].strip() if i < len(z) else ""
            abstaende[zuordnung[i]] = float(wert.replace(",", ".")) if wert else None
        gesichter.setdefault(stamm(z[idx_name]), []).append(
            {"hoehe": int(z[idx_hoehe]), "bildhoehe": int(z[idx_bild]), "abstaende": abstaende})
    return gesichter, rueck, hinweis


# -----------------------------------------------------------------
# SIMULATION: dieselbe Entscheidungslogik wie im Tagging-Programm
# -----------------------------------------------------------------
def finde_beste_zuordnung(kandidaten_je_gesicht):
    """
    Wie im Tagging-Programm: jede Person höchstens einem Gesicht, jedes Gesicht
    höchstens einer Person; möglichst viele Zuordnungen, bei Gleichstand die
    kleinste Abstandssumme. Rückgabe: pro Gesicht (person, abstand) oder None.
    """
    anzahl_gesichter = len(kandidaten_je_gesicht)
    aktive = [i for i, kandidaten in enumerate(kandidaten_je_gesicht) if kandidaten]

    if len(aktive) > MAX_GESICHTER_EXAKT:
        zuordnung = [None] * anzahl_gesichter
        alle_paare = sorted((abstand, gesicht, person) for gesicht in aktive
                            for person, abstand in kandidaten_je_gesicht[gesicht])
        vergeben = set()
        for abstand, gesicht, person in alle_paare:
            if zuordnung[gesicht] is None and person not in vergeben:
                zuordnung[gesicht] = (person, abstand)
                vergeben.add(person)
        return zuordnung

    beste = {"anzahl": -1, "summe": float("inf"), "zuordnung": [None] * anzahl_gesichter}

    def suche(position, vergeben, zuordnung, anzahl, summe):
        if position == len(aktive):
            if anzahl > beste["anzahl"] or (anzahl == beste["anzahl"] and summe < beste["summe"]):
                beste.update(anzahl=anzahl, summe=summe, zuordnung=list(zuordnung))
            return
        gesicht = aktive[position]
        suche(position + 1, vergeben, zuordnung, anzahl, summe)
        for person, abstand in kandidaten_je_gesicht[gesicht]:
            if person not in vergeben:
                zuordnung[gesicht] = (person, abstand)
                suche(position + 1, vergeben | {person}, zuordnung, anzahl + 1, summe + abstand)
                zuordnung[gesicht] = None

    suche(0, frozenset(), [None] * anzahl_gesichter, 0, 0.0)
    return beste["zuordnung"]


def standard_einstellung(namen):
    return {"tol": {p: STANDARD_TOLERANZ for p in namen}, "margin": 0.0, "min_hoehe": 0}


def zuordnungen(gesichter, einstellung):
    """
    Simuliert die Entscheidung für ein Foto. Rückgabe: Liste (person, abstand, gesichtshoehe)
    der Gesichter, die einer Person zugeordnet würden.
    """
    kandidaten_je_gesicht = []
    for gesicht in gesichter:
        werte = {p: v for p, v in gesicht["abstaende"].items() if v is not None}
        if not werte or gesicht["hoehe"] < einstellung["min_hoehe"]:
            kandidaten_je_gesicht.append([])
            continue
        if einstellung["margin"] > 0 and len(werte) >= 2:
            sortiert = sorted(werte.values())
            if sortiert[1] - sortiert[0] < einstellung["margin"]:
                kandidaten_je_gesicht.append([])
                continue
        kandidaten_je_gesicht.append([(p, v) for p, v in werte.items() if v <= einstellung["tol"][p]])

    ergebnis = finde_beste_zuordnung(kandidaten_je_gesicht)
    return [(z[0], z[1], gesichter[i]["hoehe"]) for i, z in enumerate(ergebnis) if z is not None]


def bewerte(fotos, namen, einstellung):
    """Rückgabe: (komplett richtige Fotos, verpasste Personen, falsch erkannte Personen)."""
    richtig = verpasst_summe = falsch_summe = 0
    for foto in fotos:
        vorher = {p for p, _a, _h in zuordnungen(foto["gesichter"], einstellung)}
        soll = {p for p in namen if foto["soll"][p]}
        richtig += vorher == soll
        verpasst_summe += len(soll - vorher)
        falsch_summe += len(vorher - soll)
    return richtig, verpasst_summe, falsch_summe


def mit_toleranz(einstellung, person, wert):
    neu = {**einstellung, "tol": dict(einstellung["tol"])}
    neu["tol"][person] = wert
    return neu


def optimiere(fotos, namen):
    """
    Sucht schrittweise gute Einstellungen: erst eine gemeinsame Toleranz, dann
    je Person feinjustieren (zwei Durchgänge), dann Mindestabstand zum
    Zweitbesten, dann Mindestgröße. Bei Gleichstand gewinnt die unauffälligere Einstellung.
    """
    einstellung = standard_einstellung(namen)

    def richtige(e):
        return bewerte(fotos, namen, e)[0]

    gemeinsam = max(TOLERANZ_GITTER, key=lambda t: (
        richtige({**einstellung, "tol": {p: t for p in namen}}), -abs(t - STANDARD_TOLERANZ)))
    einstellung["tol"] = {p: gemeinsam for p in namen}

    for _durchgang in range(2):
        for person in namen:
            aktuell = einstellung["tol"][person]
            einstellung["tol"][person] = max(TOLERANZ_GITTER, key=lambda t: (
                richtige(mit_toleranz(einstellung, person, t)), -abs(t - aktuell)))

    einstellung["margin"] = max(MARGIN_GITTER, key=lambda m: (richtige({**einstellung, "margin": m}), -m))
    einstellung["min_hoehe"] = max(HOEHE_GITTER, key=lambda h: (richtige({**einstellung, "min_hoehe": h}), -h))
    return einstellung


def kreuzvalidierung(fotos, namen):
    """
    Ehrlichere Schätzung: Die Einstellung wird an der einen Hälfte der Fotos gesucht
    und an der anderen Hälfte geprüft (und umgekehrt). Rückgabe: richtige Fotos insgesamt.
    """
    haelfte_a, haelfte_b = fotos[0::2], fotos[1::2]
    richtig = 0
    for lernen, pruefen in ((haelfte_a, haelfte_b), (haelfte_b, haelfte_a)):
        richtig += bewerte(pruefen, namen, optimiere(lernen, namen))[0]
    return richtig


# -----------------------------------------------------------------
# BERICHTSTEIL MIT ABSTANDSDATEI
# -----------------------------------------------------------------
def z3(wert):
    return f"{wert:.3f}".replace(".", ",")


def z2(wert):
    return f"{wert:.2f}".replace(".", ",")


def grund_verpasst(foto, person):
    """Erklärt anhand der Abstandsdatei, warum 'person' in 'foto' nicht gefunden wurde."""
    gesichter = foto["gesichter"]
    if not gesichter:
        return "kein Gesicht im Foto gefunden", None
    werte = [g["abstaende"].get(person) for g in gesichter if g["abstaende"].get(person) is not None]
    if not werte:
        return "Person kam für das Foto nicht in Frage (vor Geburt oder ohne Referenzfoto)", None
    bester = min(werte)
    if bester > STANDARD_TOLERANZ:
        return f"bester Abstand {z3(bester)} liegt über der Toleranz", bester
    return (f"Abstand {z3(bester)} wäre ok, aber das Gesicht ging an eine andere Person "
            f"bzw. wurde nicht zugeordnet"), bester


def erklaere_fehler(foto, namen):
    """Texte für die Fehlerliste: (warum verpasst, Details zu falsch erkannten)."""
    standard = standard_einstellung(namen)
    gefunden = {p: (a, h) for p, a, h in zuordnungen(foto["gesichter"], standard)}
    verpasst_text = "; ".join(f"{p}: {grund_verpasst(foto, p)[0]}" for p in verpasst(foto, namen))
    falsch_text = "; ".join(
        f"{p}: Abstand {z3(gefunden[p][0])}, Gesicht {gefunden[p][1]} px hoch"
        for p in falsch_dazu(foto, namen) if p in gefunden)
    return verpasst_text, falsch_text


def tabelle_nach_klassen(paare, grenzen, einheit, beschriftung):
    """Zeigt je Klasse, wie viele Zuordnungen es gab und wie viele davon falsch waren."""
    zeilen = [f"   {beschriftung:<24}{'Zuordnungen':>12}{'davon falsch':>14}"]
    for i, (von, bis) in enumerate(grenzen):
        gruppe = [richtig for wert, richtig in paare if von <= wert < bis]
        if not gruppe:
            continue
        falsch = sum(not r for r in gruppe)
        if bis >= 9999:
            name = f"ab {von:g} {einheit}".strip().replace(".", ",")
        else:
            name = f"{von:g} bis unter {bis:g} {einheit}".strip().replace(".", ",")
        zeilen.append(f"   {name:<24}{len(gruppe):>12}{falsch:>9}  ({prozent(falsch, len(gruppe)).strip()})")
    return zeilen


def baue_simulationsbericht(namen, fotos, rueck, gesichter_je_foto, hinweis):
    bewertbar = [f for f in fotos if not f["markiert"]]
    standard = standard_einstellung(namen)
    z = []

    # -- 8. Basisprüfung ------------------------------------------------
    gleich = sum({p for p, _a, _h in zuordnungen(f["gesichter"], standard)} ==
                 {p for p in namen if f["ist"][p]} for f in fotos)
    unbekannt = len(set(gesichter_je_foto) - {stamm(f["name"]) for f in fotos})
    mit_gesicht = sum(bool(f["gesichter"]) for f in fotos)
    z.append("8. ABSTANDSDATEI: BASISPRÜFUNG")
    if hinweis:
        z.append(f"   {hinweis}")
    z.append(f"   Fotos der Tabelle, zu denen die Abstandsdatei Gesichter enthält: {mit_gesicht} von {len(fotos)}")
    if unbekannt:
        z.append(f"   {unbekannt} Fotos der Abstandsdatei stehen nicht in der Tabelle (werden ignoriert).")
    z.append(f"   Die Simulation mit den Standardwerten (Toleranz {z2(STANDARD_TOLERANZ)} für alle, keine Zusatzregeln)")
    z.append(f"   stimmt bei {gleich} von {len(fotos)} Fotos mit den Programm-Spalten der Tabelle überein.")
    if gleich < 0.95 * len(fotos):
        z.append("   ACHTUNG: Das ist weniger als 95 %. Vermutlich stammt die Abstandsdatei aus einem anderen Lauf")
        z.append("   oder mit anderen Einstellungen als die Programm-Spalten - die folgenden Zahlen sind dann unsicher.")
    z.append("")

    # -- 9. Warum verpasst? ---------------------------------------------
    gruende = Counter()
    knapp = Counter()
    for foto in bewertbar:
        for person in verpasst(foto, namen):
            text, bester = grund_verpasst(foto, person)
            if bester is not None and bester > STANDARD_TOLERANZ:
                klasse = "bester Abstand über der Toleranz"
                if bester <= STANDARD_TOLERANZ + 0.05:
                    knapp["bis 0,05 über der Toleranz (knapp daneben)"] += 1
                elif bester <= STANDARD_TOLERANZ + 0.10:
                    knapp["0,05 bis 0,10 über der Toleranz"] += 1
                else:
                    knapp["mehr als 0,10 über der Toleranz (weit daneben)"] += 1
            elif bester is not None:
                klasse = "Abstand wäre ok, Gesicht ging an andere Person/wurde nicht zugeordnet"
            else:
                klasse = text
            gruende[klasse] += 1
    gesamt_verpasst = sum(gruende.values())
    z.append(f"9. WARUM WURDEN PERSONEN VERPASST? ({gesamt_verpasst} verpasste Personen in bewertbaren Fotos)")
    for klasse, anzahl in gruende.most_common():
        z.append(f"   {klasse:<78}{anzahl:>4}")
    if knapp:
        z.append("   Bei 'bester Abstand über der Toleranz':")
        for klasse in ("bis 0,05 über der Toleranz (knapp daneben)", "0,05 bis 0,10 über der Toleranz",
                       "mehr als 0,10 über der Toleranz (weit daneben)"):
            if klasse in knapp:
                z.append(f"      {klasse:<60}{knapp[klasse]:>4}")
    z.append("")

    # -- 10. Qualität der Zuordnungen -----------------------------------
    paare_abstand, paare_hoehe = [], []
    richtig_werte, falsch_werte = [], []
    for foto in bewertbar:
        for person, abstand, hoehe in zuordnungen(foto["gesichter"], standard):
            richtig = foto["soll"][person]
            paare_abstand.append((abstand, richtig))
            paare_hoehe.append((hoehe, richtig))
            (richtig_werte if richtig else falsch_werte).append((abstand, hoehe))
    z.append("10. QUALITÄT DER ZUORDNUNGEN (bewertbare Fotos, Simulation mit Standardwerten)")
    for titel, werte in (("richtig zugeordnet", richtig_werte), ("falsch zugeordnet", falsch_werte)):
        if werte:
            z.append(f"   {titel:<20}{len(werte):>4}  Abstand (Median) {z3(statistics.median(a for a, _ in werte))}"
                     f"   Gesichtshöhe (Median) {statistics.median(h for _, h in werte):.0f} px")
    z.append("   Wie oft liegt eine Zuordnung falsch - je nach Abstand?")
    z += tabelle_nach_klassen(paare_abstand, [(0, 0.40), (0.40, 0.50), (0.50, 0.55), (0.55, 0.60), (0.60, 9999)],
                              "", "Abstand")
    z.append("   Wie oft liegt eine Zuordnung falsch - je nach Gesichtsgröße (Höhe in Pixel)?")
    z += tabelle_nach_klassen(paare_hoehe, [(0, 60), (60, 100), (100, 150), (150, 10 ** 6)], "px", "Gesichtshöhe")
    z.append("")

    # -- 11. Einstellungen ausprobieren ----------------------------------
    n = len(bewertbar)
    basis = bewerte(bewertbar, namen, standard)
    z.append("11. EINSTELLUNGEN AUSPROBIEREN (bewertbare Fotos, ohne die Gesichtserkennung neu zu rechnen)")
    z.append("   a) Eine gemeinsame Toleranz für alle Personen:")
    z.append(f"      {'Toleranz':<10}{'komplett richtig':>18}{'verpasst':>10}{'falsch dazu':>13}")
    for t in TOLERANZ_GITTER:
        r, v, f = bewerte(bewertbar, namen, {**standard, "tol": {p: t for p in namen}})
        marke = "   <- Standard" if abs(t - STANDARD_TOLERANZ) < 1e-9 else ""
        z.append(f"      {z2(t):<10}{r:>12} von {n:<3}{v:>9}{f:>13}{marke}")
    z.append("")

    beste = optimiere(bewertbar, namen)
    r_beste, v_beste, f_beste = bewerte(bewertbar, namen, beste)
    r_cv = kreuzvalidierung(bewertbar, namen)
    z.append("   b) Beste gefundene Einstellung (Schwellenwert je Person + Zusatzregeln):")
    z.append(f"      Standard:                                   {basis[0]:>4} von {n} komplett richtig ({prozent(basis[0], n).strip()})")
    z.append(f"      Beste Einstellung, an ALLEN Fotos gesucht:  {r_beste:>4} von {n} komplett richtig ({prozent(r_beste, n).strip()})")
    z.append("         -> optimistisch: Die Einstellung wurde an genau diesen Fotos ausgesucht.")
    z.append(f"      Kreuzvalidierung (ehrlichere Schätzung):    {r_cv:>4} von {n} komplett richtig ({prozent(r_cv, n).strip()})")
    z.append("         -> Einstellung jeweils an einer Hälfte der Fotos gesucht, an der anderen geprüft.")
    gewinn = r_cv - basis[0]
    if gewinn >= 3:
        urteil = f"Die Änderung bringt voraussichtlich etwa {gewinn} Fotos mehr komplett richtig - einen Versuch wert."
    elif gewinn >= 1:
        urteil = "Der Gewinn ist klein (1-2 Fotos) und liegt im Bereich des Zufalls - eher nicht umstellen."
    else:
        urteil = "Kein belastbarer Gewinn - die Einstellungen besser nicht ändern."
    z.append(f"      Urteil: {urteil}")
    z.append("")

    z.append("   c) Die gefundene Einstellung (zum Eintragen oben im Tagging-Programm):")
    toleranzen = ", ".join(f'"{rueck[p]}": {z2(beste["tol"][p]).replace(",", ".")}' for p in namen)
    z.append(f"      TOLERANZ_JE_PERSON = {{{toleranzen}}}")
    z.append(f"      MIN_ABSTAND_ZUM_ZWEITBESTEN = {z2(beste['margin']).replace(',', '.')}")
    z.append(f"      MIN_GESICHTSHOEHE_PX = {beste['min_hoehe']}")
    z.append(f"      (Wirkung an diesen Fotos: verpasst {v_beste} statt {basis[1]}, falsch dazu {f_beste} statt {basis[2]})")
    z.append("   Nur übernehmen, wenn das Urteil unter b) positiv ist - und danach mit dem Tagging-Programm neu")
    z.append("   laufen lassen und die Tabelle neu auswerten.")
    return z


def schreibe_fehlerliste(zieldatei, namen, fotos, mit_erklaerung=False):
    kopf = ["Zeile", "FotoName", "Datum", "Markiert", "Manuell", "Programm", "Verpasst", "FalschDazu", "Fehlerart"]
    if mit_erklaerung:
        kopf += ["WarumVerpasst", "DetailsFalschErkannt"]
    zeilen = ["\t".join(kopf)]
    for foto in fotos:
        if ist_komplett_richtig(foto):
            continue
        datum = foto["datum"].strftime("%Y-%m-%d") if foto["datum"] else ""
        manuell = ", ".join(p for p in namen if foto["soll"][p]) or "niemand"
        programm = ", ".join(p for p in namen if foto["ist"][p]) or "niemand"
        zellen = [
            str(foto["zeile"]), foto["name"], datum, "ja" if foto["markiert"] else "nein",
            manuell, programm, ", ".join(verpasst(foto, namen)), ", ".join(falsch_dazu(foto, namen)),
            fehlerart(foto, namen),
        ]
        if mit_erklaerung:
            zellen += list(erklaere_fehler(foto, namen))
        zeilen.append("\t".join(zellen))
    with open(zieldatei, "w", encoding="utf-8") as f:
        f.write("\n".join(zeilen) + "\n")


# -----------------------------------------------------------------
# STARTPUNKT
# -----------------------------------------------------------------
if __name__ == "__main__":
    if len(sys.argv) not in (2, 3):
        print("Aufruf: python auswertung_erkennung.py <Excel-Datei> [foto_abstaende.txt]")
        sys.exit(1)

    pfad = sys.argv[1]
    abstandspfad = sys.argv[2] if len(sys.argv) == 3 else None
    try:
        namen, fotos = lese_tabelle(pfad)
        gesichter_je_foto, rueck, hinweis = ({}, {}, None)
        if abstandspfad:
            gesichter_je_foto, rueck, hinweis = lese_abstandsdatei(abstandspfad, namen)
            for foto in fotos:
                foto["gesichter"] = gesichter_je_foto.get(stamm(foto["name"]), [])
    except (OSError, ValueError, KeyError, IndexError) as fehler:
        print(f"FEHLER: {fehler}")
        sys.exit(1)

    bericht = baue_bericht(pfad, namen, fotos)
    if abstandspfad:
        bericht.append("")
        bericht += baue_simulationsbericht(namen, fotos, rueck, gesichter_je_foto, hinweis)
    print("\n".join(bericht))

    with open("auswertung_bericht.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(bericht) + "\n")
    schreibe_fehlerliste("auswertung_fehlerliste.txt", namen, fotos, mit_erklaerung=bool(abstandspfad))
    print("\nGespeichert: auswertung_bericht.txt, auswertung_fehlerliste.txt")
