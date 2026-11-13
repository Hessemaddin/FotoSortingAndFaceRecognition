"""
MODELL "INSIGHTFACE" FÜR DIE FOTO-PIPELINE
==========================================

Moderne Gesichtserkennung (InsightFace "buffalo_l": SCRFD zum Finden der
Gesichter, ArcFace zum Berechnen des "Fingerabdrucks"). Läuft komplett offline.

Diese Datei wird von foto_pipeline_mit_gesichtserkennung.py benutzt, wenn dort
MODELL = "insightface" eingestellt ist. Sie bietet dieselben drei Funktionen wie
die Bibliothek face_recognition (face_locations, face_encodings, face_distance),
damit das Hauptprogramm fast unverändert bleibt.

Benötigt (einmalig installieren):
    python -m pip install onnxruntime

Modelldateien (einmalig, ca. 280 MB):
    https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_l.zip
    herunterladen und so entpacken, dass es diese beiden Dateien gibt:
        <Programmordner>\\modelle\\buffalo_l\\det_10g.onnx
        <Programmordner>\\modelle\\buffalo_l\\w600k_r50.onnx
    (Die übrigen Dateien im Zip werden nicht gebraucht.)

Lizenz-Hinweis: Die buffalo_l-Modelle sind laut InsightFace nur für nicht-
kommerzielle Zwecke freigegeben. Für private Familienfotos passt das.

Abstand: 1 - Kosinus-Ähnlichkeit (0 = identisch, ca. 1 = völlig verschieden).
Die Werte sind NICHT mit denen von face_recognition vergleichbar - deshalb gibt
es im Hauptprogramm eine eigene Toleranz für dieses Modell.
"""

import os

import numpy as np
from PIL import Image

try:
    import onnxruntime as ort
except ImportError:
    raise SystemExit(
        "FEHLER: Für MODELL = \"insightface\" fehlt das Paket onnxruntime.\n"
        "Bitte einmalig installieren:  python -m pip install onnxruntime")


# -----------------------------------------------------------------
# EINSTELLUNGEN
# -----------------------------------------------------------------
MODELLORDNER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "modelle", "buffalo_l")
MODELL_URL = "https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_l.zip"

ERKENNUNG_GROESSE = 640        # Bild wird für die Gesichtssuche auf 640x640 eingepasst
ERKENNUNG_SCHWELLE = 0.5       # Mindest-Sicherheit, damit etwas als Gesicht zählt
NMS_SCHWELLE = 0.4             # doppelte Rahmen für dasselbe Gesicht zusammenfassen
# Gesichter kleiner als diese Höhe (Pixel, im Originalbild) werden ignoriert
MIN_GESICHT_PX = 20
# Wird nichts gefunden, wird ein schwarzer Rand dieser Breite (Anteil der
# längsten Bildkante, je Seite) ergänzt und noch einmal gesucht. 0 = aus.
RAND_BEI_NICHTS_GEFUNDEN = 0.5

# Zielpositionen von Augen, Nase und Mundwinkeln im 112x112-Ausschnitt (ArcFace-Standard)
_ARCFACE_ZIEL = np.array([
    [38.2946, 51.6963], [73.5318, 51.5014], [56.0252, 71.7366],
    [41.5493, 92.3655], [70.7299, 92.2041]], dtype=np.float32)


# -----------------------------------------------------------------
# MODELLE LADEN (einmalig, beim ersten Gebrauch)
# -----------------------------------------------------------------
_sitzungen = {}


def _sitzung(dateiname):
    if dateiname not in _sitzungen:
        pfad = os.path.join(MODELLORDNER, dateiname)
        if not os.path.exists(pfad):
            raise SystemExit(
                f"FEHLER: Modelldatei nicht gefunden: {pfad}\n"
                f"Bitte {MODELL_URL} herunterladen und so entpacken, dass die Dateien\n"
                f"det_10g.onnx und w600k_r50.onnx im Ordner {MODELLORDNER} liegen.")
        _sitzungen[dateiname] = ort.InferenceSession(pfad, providers=["CPUExecutionProvider"])
    return _sitzungen[dateiname]


# -----------------------------------------------------------------
# GESICHTER FINDEN (SCRFD)
# -----------------------------------------------------------------
_anker_cache = {}


def _anker(hoehe, breite, schritt, anzahl=2):
    schluessel = (hoehe, breite, schritt)
    if schluessel not in _anker_cache:
        y, x = np.mgrid[:hoehe, :breite]
        zentren = (np.stack([x, y], axis=-1).astype(np.float32) * schritt).reshape(-1, 2)
        _anker_cache[schluessel] = np.repeat(zentren, anzahl, axis=0)
    return _anker_cache[schluessel]


def _nms(rahmen, werte, schwelle):
    x1, y1, x2, y2 = rahmen.T
    flaeche = (x2 - x1 + 1) * (y2 - y1 + 1)
    reihenfolge = werte.argsort()[::-1]
    behalten = []
    while reihenfolge.size > 0:
        i = reihenfolge[0]
        behalten.append(i)
        xx1 = np.maximum(x1[i], x1[reihenfolge[1:]])
        yy1 = np.maximum(y1[i], y1[reihenfolge[1:]])
        xx2 = np.minimum(x2[i], x2[reihenfolge[1:]])
        yy2 = np.minimum(y2[i], y2[reihenfolge[1:]])
        schnitt = np.maximum(0, xx2 - xx1 + 1) * np.maximum(0, yy2 - yy1 + 1)
        iou = schnitt / (flaeche[i] + flaeche[reihenfolge[1:]] - schnitt)
        reihenfolge = reihenfolge[1:][iou <= schwelle]
    return behalten


def _erkenne(bild):
    """
    Wie _erkenne_einmal. Findet es nichts, wird ein Rand um das Bild gelegt und
    erneut gesucht: Bei eng zugeschnittenen Porträts (das Gesicht füllt fast das
    ganze Bild, typisch für Referenzfotos) findet SCRFD sonst oft kein Gesicht.
    """
    ergebnis = _erkenne_einmal(bild)
    if ergebnis or not RAND_BEI_NICHTS_GEFUNDEN:
        return ergebnis
    h, w = bild.shape[:2]
    rand = int(max(h, w) * RAND_BEI_NICHTS_GEFUNDEN)
    gross = np.zeros((h + 2 * rand, w + 2 * rand, 3), dtype=bild.dtype)
    gross[rand:rand + h, rand:rand + w] = bild
    zurueck = []
    for (x1, y1, x2, y2), sicher, punkte in _erkenne_einmal(gross):
        zurueck.append(((max(0.0, x1 - rand), max(0.0, y1 - rand),
                         min(float(w), x2 - rand), min(float(h), y2 - rand)),
                        sicher, punkte - rand))
    return zurueck


def _erkenne_einmal(bild):
    """bild: RGB-Array. Rückgabe: Liste von (rahmen x1,y1,x2,y2, sicherheit, 5 Punkte)."""
    sitzung = _sitzung("det_10g.onnx")
    h, w = bild.shape[:2]
    faktor = ERKENNUNG_GROESSE / max(h, w)
    neu_w, neu_h = max(1, int(round(w * faktor))), max(1, int(round(h * faktor)))
    klein = np.asarray(Image.fromarray(bild).resize((neu_w, neu_h), Image.BILINEAR), dtype=np.float32)
    leinwand = np.zeros((ERKENNUNG_GROESSE, ERKENNUNG_GROESSE, 3), dtype=np.float32)
    leinwand[:neu_h, :neu_w] = klein
    eingabe = ((leinwand - 127.5) / 128.0).transpose(2, 0, 1)[None]

    ausgaben = sitzung.run(None, {sitzung.get_inputs()[0].name: eingabe})
    alle_rahmen, alle_werte, alle_punkte = [], [], []
    for i, schritt in enumerate((8, 16, 32)):
        werte = ausgaben[i].reshape(-1)
        abst = ausgaben[i + 3].reshape(-1, 4) * schritt
        pkt = ausgaben[i + 6].reshape(-1, 10) * schritt
        groesse = ERKENNUNG_GROESSE // schritt
        zentren = _anker(groesse, groesse, schritt)
        auswahl = np.where(werte >= ERKENNUNG_SCHWELLE)[0]
        if auswahl.size == 0:
            continue
        z = zentren[auswahl]
        d = abst[auswahl]
        alle_rahmen.append(np.stack([z[:, 0] - d[:, 0], z[:, 1] - d[:, 1],
                                     z[:, 0] + d[:, 2], z[:, 1] + d[:, 3]], axis=-1))
        p = pkt[auswahl].reshape(-1, 5, 2) + z[:, None, :]
        alle_punkte.append(p)
        alle_werte.append(werte[auswahl])
    if not alle_rahmen:
        return []
    rahmen = np.concatenate(alle_rahmen) / faktor
    punkte = np.concatenate(alle_punkte) / faktor
    werte = np.concatenate(alle_werte)
    ergebnis = []
    for i in _nms(rahmen, werte, NMS_SCHWELLE):
        x1, y1, x2, y2 = rahmen[i]
        if y2 - y1 < MIN_GESICHT_PX:
            continue
        ergebnis.append(((x1, y1, x2, y2), float(werte[i]), punkte[i]))
    return ergebnis


# -----------------------------------------------------------------
# FINGERABDRUCK (ArcFace)
# -----------------------------------------------------------------
def _aehnlichkeits_transformation(quelle, ziel):
    """Drehung + Skalierung + Verschiebung (Umeyama), die 'quelle' auf 'ziel' abbildet. 2x3-Matrix."""
    mq, mz = quelle.mean(0), ziel.mean(0)
    q, z = quelle - mq, ziel - mz
    kov = z.T @ q / len(quelle)
    u, s, vt = np.linalg.svd(kov)
    d = np.ones(2)
    if np.linalg.det(u) * np.linalg.det(vt) < 0:
        d[-1] = -1
    rot = u @ np.diag(d) @ vt
    skal = (s * d).sum() / q.var(0).sum()
    m = np.zeros((2, 3))
    m[:, :2] = skal * rot
    m[:, 2] = mz - skal * rot @ mq
    return m


def _fingerabdruck(bild, punkte):
    m = _aehnlichkeits_transformation(np.asarray(punkte, dtype=np.float64), _ARCFACE_ZIEL.astype(np.float64))
    # PIL braucht die Umkehrabbildung (Ausschnitt -> Originalbild)
    voll = np.vstack([m, [0, 0, 1]])
    inv = np.linalg.inv(voll)
    ausschnitt = Image.fromarray(bild).transform(
        (112, 112), Image.AFFINE, data=tuple(inv[:2].reshape(-1)), resample=Image.BILINEAR)
    a = (np.asarray(ausschnitt, dtype=np.float32) - 127.5) / 127.5
    sitzung = _sitzung("w600k_r50.onnx")
    emb = sitzung.run(None, {sitzung.get_inputs()[0].name: a.transpose(2, 0, 1)[None]})[0][0]
    return emb / np.linalg.norm(emb)


# -----------------------------------------------------------------
# DIESELBEN FUNKTIONEN WIE face_recognition
# -----------------------------------------------------------------
_letztes = {"id": None, "form": None, "gesichter": {}}


def face_locations(bild, number_of_times_to_upsample=1, model="hog"):
    """Rahmen (oben, rechts, unten, links) wie bei face_recognition. Die Parameter
    number_of_times_to_upsample und model werden hier nicht gebraucht."""
    h, w = bild.shape[:2]
    orte = {}
    for (x1, y1, x2, y2), _sicher, punkte in _erkenne(bild):
        ort = (max(0, int(round(y1))), min(w, int(round(x2))),
               min(h, int(round(y2))), max(0, int(round(x1))))
        orte[ort] = punkte
    _letztes.update({"id": id(bild), "form": bild.shape, "gesichter": orte})
    return list(orte)


def face_encodings(bild, known_face_locations=None, num_jitters=1):
    """Fingerabdrücke für die übergebenen Rahmen (num_jitters wird nicht gebraucht)."""
    if known_face_locations is None:
        known_face_locations = face_locations(bild)
    if _letztes["id"] != id(bild) or _letztes["form"] != bild.shape:
        face_locations(bild)
    ergebnis = []
    for ort in known_face_locations:
        punkte = _letztes["gesichter"].get(tuple(ort))
        if punkte is None:
            raise ValueError("Für diesen Rahmen gibt es keine Gesichtspunkte "
                             "(Rahmen muss von face_locations dieses Modells stammen).")
        ergebnis.append(_fingerabdruck(bild, punkte))
    return ergebnis


def face_distance(bekannte, unbekannt):
    """1 - Kosinus-Ähnlichkeit (Fingerabdrücke sind bereits normiert)."""
    if len(bekannte) == 0:
        return np.empty(0)
    return 1.0 - np.asarray(bekannte) @ np.asarray(unbekannt)
