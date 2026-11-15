# HyCube für Home Assistant

Custom Integration für **HyCube** Batteriespeicher (HyCube Technologies GmbH). Sie liest den Speicher
direkt im lokalen Netz über die *CubeConnect*-API der HyWeb-Oberfläche aus, ganz ohne Cloud.

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/docs/faq/custom_repositories/)
![IoT class](https://img.shields.io/badge/IoT-local__polling-green)

> **Inoffizielles Projekt.** Es besteht keine Verbindung zur HyCube Technologies GmbH.
> Die Batteriesteuerung nutzt undokumentierte Endpunkte der HyWeb-Oberfläche. Du nutzt sie auf eigenes Risiko.

## Funktionen

- **Echtzeitwerte**: Ladezustand, Leistung von Batterie, Netz (gesamt und je Phase), Hausverbrauch,
  PV (gesamt und je String) und Wechselrichter, dazu Spannungen, Ströme und Netzfrequenz.
- **Energiezähler für das Energie-Dashboard**: Die HyCube liefert nur Leistungswerte. Die Integration
  rechnet daraus kWh-Zähler (Trapezregel) für Netzbezug, Einspeisung, Batterie geladen/entladen, PV,
  externe Erzeugung und Hausverbrauch. Die Zähler bleiben über Neustarts erhalten.
- **Lückenfüllung**: Fällt die Verbindung aus, holt die Integration die fehlende Energie aus den
  5-Minuten-Statistiken der HyCube nach (bis zu 7 Tage zurück), statt sie zu verlieren oder zu schätzen.
- **Lernende Verbrauchsprognose**: Die Integration archiviert Verbrauch und PV-Erzeugung dauerhaft und
  lernt daraus, wie der Verbrauch von Wochentag, Uhrzeit, Jahreszeit und Außentemperatur abhängt. Sie
  prüft ihre Modelle an der eigenen Historie und nutzt das mit dem kleinsten Fehler. Daraus folgen
  Prognosen für die nächste Stunde, den Rest des Tages, die nächsten 24 h und morgen sowie eine
  Schätzung, **wann der Akku ohne PV leer wäre**.
- **Kostenoptimierter Fahrplan**: Aus Verbrauchsprofil, PV-Prognose und Tibber-Preisen für heute und
  morgen berechnet die Integration, wann die PV das Haus versorgt, wann der Akku, wann das Netz und
  wann sich Laden aus dem Netz lohnt. Auf Wunsch schaltet sie den Batteriemodus selbst.
- **Batteriesteuerung**: Modi *Standard*, *Energielevel halten* und *Aus dem Netz laden* mit
  einstellbarem Ziel-Ladezustand.
- **Statusflags**: Netzladung aktiv, Batterie-Zeitplan, Notstrom (EPS) aktiviert und Überlast.
- **Schonend für den Controller**: Der Controller verträgt nur wenige gleichzeitige Verbindungen.
  Die Integration nutzt deshalb eine einzige Keep-Alive-Verbindung, hält Abstand zwischen den Requests,
  verwendet das Token weiter und wartet bei „Too many connections“ automatisch länger.
- Config Flow mit Reauth und Reconfigure, Optionen im UI, Diagnosedaten-Download und Übersetzungen auf
  Deutsch und Englisch.

## Voraussetzungen

- HyCube mit HyWeb-Oberfläche im lokalen Netz (entwickelt und getestet mit HyWeb **2.065**)
- Home Assistant **2025.2** oder neuer
- Zugangsdaten der HyWeb-Oberfläche (Werkseinstellung `hycube` / `hycube`)

## Installation

### Über HACS (empfohlen)

1. HACS öffnen → Menü oben rechts → **Benutzerdefinierte Repositories**.
2. `https://github.com/daddeldash/ha-hycube` als Typ **Integration** hinzufügen.
3. **HyCube** suchen, herunterladen und Home Assistant neu starten.

### Manuell

1. Den Inhalt dieses Repositories nach `config/custom_components/hycube/` kopieren, sodass dort
   unter anderem `config/custom_components/hycube/manifest.json` liegt.
2. Home Assistant neu starten.

## Einrichtung

1. **Einstellungen → Geräte & Dienste → Integration hinzufügen → HyCube**.
2. Die **IP-Adresse** (oder den Hostnamen) der HyCube eintragen, ohne `http://`.
3. Benutzername und Passwort eingeben. Ohne Änderung ist das `hycube` / `hycube`.

Die Integration erkennt das Gerät an seiner Seriennummer. Ändert sich die IP-Adresse, passt du sie
über **⋮ → Neu konfigurieren** an. Lehnt die HyCube die Zugangsdaten ab, startet Home Assistant
automatisch eine Neu-Anmeldung.

> Gib der HyCube im Router eine feste IP-Adresse (DHCP-Reservierung), damit die Verbindung stabil bleibt.

## Optionen

Erreichbar über **Geräte & Dienste → HyCube → Konfigurieren**. Der Dialog hat zwei Bereiche.

### Gerät, Profil und Steuerung

| Option | Standard | Beschreibung |
| --- | --- | --- |
| Abfrageintervall Echtzeitwerte | 10 s | Wie oft `/get_values/` abgefragt wird (min. 5 s). Kürzere Intervalle machen die Energiezähler genauer, belasten aber den Controller. |
| Abfrageintervall Status | 60 s | Wie oft die Statusflags (`/data_row/`) gelesen werden. |
| Nutzbare Batteriekapazität | 10 kWh | Energie pro 100 % Ladezustand, für „gespeicherte Energie“ und die Prognose „Batterie leer“. Die Diagnose-Entität **Batteriekapazität (geschätzt)** zeigt, was der Akku beim Entladen tatsächlich liefert (erster Wert nach mindestens 20 % Entladung ohne Laden, z. B. abends); trage diesen Wert hier ein. |
| Profil-Quelle | leer | Optional ein beliebiger Energie-Sensor mit Langzeitstatistik. Leer = Archiv aus der Tagesstatistik der HyCube (Fallback: Hausverbrauch-Zähler dieser Integration). |
| Außentemperatur-Sensor | leer | Hängt der Verbrauch an der Außentemperatur (Wärmepumpe, Heizung), lernt das Verbrauchsmodell diesen Zusammenhang. Der Sensor braucht eine Langzeitstatistik. |
| Wettervorhersage | leer | Wetter-Entität, die die Temperatur der kommenden Tage liefert. Erst mit Sensor und Wettervorhersage wird das Temperaturmodell genutzt. |
| Batteriesteuerung aktivieren | an | Ist sie aus, lehnt die Integration jede Modusänderung ab und liest nur noch. |
| Request für Modus … | siehe unten | Welche HTTP-Requests ein Modus auslöst. |

### Fahrplan

| Option | Standard | Beschreibung |
| --- | --- | --- |
| Tibber-Zuhause | leer | Zuhause, dessen Preise gelten. Leer = das einzige Zuhause oder das, dessen Preis-Sensor im Energie-Dashboard eingetragen ist. |
| Netzladung einplanen | an | Der Fahrplan darf den Akku aus dem Netz laden. |
| Halten einplanen | an | Der Fahrplan darf den Akku für teurere Stunden aufsparen (Modus *Energielevel halten*). |
| Netzladeleistung | 2500 W | Leistung, mit der die HyCube aus dem Netz lädt. |
| Maximaler Ladezustand bei Netzladung | 100 % | Höher lädt der Fahrplan nicht aus dem Netz. |
| Wirkungsgrad des Akkus | leer | Anteil der geladenen Energie, den der Akku wieder abgibt. Leer = der gemessene Wert der Diagnose-Entität **Wirkungsgrad (geschätzt)**, bis dahin 80 %. |
| Mindestvorteil | 1 ct/kWh | Netzladung und Halten werden nur eingeplant, wenn sie je kWh mehr als diesen Betrag sparen. Deckt Verschleiß und Prognosefehler ab. |
| Einspeisevergütung | 0 ct/kWh | Erlös je eingespeister kWh. |
| PV-Prognose automatisch korrigieren | an | Die Integration vergleicht Prognose und echte Erzeugung der letzten Wochen und korrigiert die Prognose je Tagesstunde. |
| Zusätzlicher Faktor PV-Prognose | 1,0 | Multipliziert die PV-Prognose zusätzlich zur automatischen Korrektur. Normalerweise 1. |

## Batteriemodi

Die Auswahl **Batteriemodus** schickt je nach Modus einen oder mehrere Requests an die HyCube:

| Modus | Wirkung | Standard-Requests |
| --- | --- | --- |
| **Standard (Eigenverbrauch)** | Normaler Betrieb. Netzladung wird gestoppt, die Akku-Aufteilung zurückgesetzt (85 % Normalbetrieb, 5 % Puffer). | `/smartCharging/ManualChargingActivation/?value=`<br>`/Bat/setCustomBat/?x_active=85&x_passive=5` |
| **Energielevel halten** ⚠️ | Der Akku soll nicht unter den aktuellen Ladezustand entladen werden. Dafür wird der Normalbetrieb verkleinert, sodass die Notstrom-Reserve beim aktuellen Ladezustand beginnt. Lädt PV nach, wird die Grenze mitgezogen. | `/smartCharging/ManualChargingActivation/?value=`<br>`/Bat/setCustomBat/?x_active={hold_active}&x_passive=5` |
| **Aus dem Netz laden** | Lädt den Akku aus dem Netz bis zum Ziel-Ladezustand (Entität **Netzladung Ziel-Ladezustand**, 10–100 %). | `/Bat/setCustomBat/?x_active=85&x_passive=5`<br>`/smartCharging/ManualChargingActivation/?value={soc}` |

Platzhalter in den Requests:

- `{soc}`: Ziel-Ladezustand der Netzladung
- `{current_soc}`: aktueller Ladezustand
- `{hold_active}`: `100 − aktueller Ladezustand`, mindestens 10

Die Requests kannst du in den Optionen ändern, einer pro Zeile. Erlaubt sind nur Pfade auf der
konfigurierten HyCube (beginnend mit `/`), keine fremden Hosts.

> ⚠️ **„Energielevel halten“ ist experimentell.** Die HyCube hat keinen Befehl „Entladen sperren“, der
> Modus arbeitet deshalb mit der Notstrom-Reserve. Manche Firmware füllt diese Reserve aus dem Netz auf.
> Erkennt die Integration Batterieladung ohne PV, schreibt sie eine Warnung ins Log. Beobachtet wurde
> außerdem, dass der Akku nach dem Verlassen von „Halten“ bis zu anderthalb Stunden nicht entlädt. Bezieht
> das Haus in der Stunde danach länger als 10 Minuten Strom aus dem Netz, obwohl der Akku über der Reserve
> steht, schreibt die Integration ebenfalls eine Warnung ins Log. Beobachte das Verhalten deines Geräts,
> bevor du den Modus in Automationen nutzt.

Ändert jemand die Netzladung direkt in der HyWeb-Oberfläche oder ist sie abgeschlossen, übernimmt die
Auswahl den Zustand beim nächsten Statusabruf.

## Energie-Dashboard

Unter **Einstellungen → Dashboards → Energie** zuordnen:

| Bereich | Entität |
| --- | --- |
| Netzbezug | *Netzbezug Energie* |
| Einspeisung | *Netzeinspeisung Energie* |
| Solarproduktion | *PV Energie* (plus ggf. *Externe Erzeugung Energie*, standardmäßig deaktiviert) |
| Batterie geladen / entladen | *Batterie geladen Energie* / *Batterie entladen Energie* |
| Einzelverbrauch (optional) | *Hausverbrauch Energie* |

Die Zähler laufen ab der Einrichtung. Die Integration selbst hat keine Vergangenheitswerte.

## Verbrauchsprognose

### Archiv

Die Integration speichert den Verbrauch und die PV-Erzeugung jedes Tages stundenweise und dauerhaft,
dazu das Tagesmittel der Außentemperatur. Quelle ist die Tagesstatistik der HyCube. Jede Nacht kommt
der Vortag hinzu; außerdem holt die Integration pro Lauf bis zu 30 ältere Tage nach, bis die HyCube
14 Tage in Folge nichts mehr liefert. So wächst das Archiv auch in die Vergangenheit, ohne den
Controller zu belasten. Je länger das Archiv reicht, desto mehr Muster kann die Prognose nutzen.

### Modelle

Drei Arten von Modell sagen den Verbrauch je Stunde voraus:

| Modell | Muster | Sinnvoll ab |
| --- | --- | --- |
| *Letzte Wochen* | Wochentag × Stunde, neuere Wochen zählen stärker. | wenigen Tagen |
| *Jahreszeit* | Wie oben, aber Tage derselben Jahreszeit zählen voll, auch aus den Vorjahren. | etwa einem Jahr |
| *Außentemperatur* | Je Stunde: Grundlast plus Mehrverbrauch je Grad unter einer Heizgrenze, dazu der Einfluss des Wochentags. Gilt auch für Kälte, die im Archiv noch nicht vorkam. | 21 Tagen mit mindestens 5 K Temperaturspanne |

Für das Temperaturmodell trägst du in den Optionen einen Außentemperatur-Sensor und eine
Wetter-Entität ein. Die Wettervorhersage weicht oft vom eigenen Sensor ab (Hauswand, Höhenlage); die
Integration misst diese Abweichung laufend und rechnet sie in die Vorhersage ein.

Welches Modell gilt, entscheidet eine Rückrechnung: Jedes Modell sagt jeden der letzten 21 Tage nur
aus den Daten davor voraus. Das Modell mit dem kleinsten Fehler wird genutzt; gewechselt wird erst bei
einem klaren Vorsprung. Das geschieht täglich um 00:10 Uhr und beim Start, auf Wunsch auch über den
Button **Verbrauchsprofil neu berechnen**. Die Prognose selbst wird stündlich mit der aktuellen
Wettervorhersage erneuert.

### Sensoren

Die Prognose-Sensoren sind verfügbar, sobald mindestens ein voller Tag an Daten vorliegt. Der Sensor
**Verbrauchsprognose 24 h** hat zusätzlich diese Attribute:

- `hourly`: Liste `[{start, kwh}]` für die nächsten 24 vollen Stunden, z. B. für ApexCharts
- `profile_today`: 24 Stundenwerte für heute
- `model`, `profile_days_of_data`, `profile_built`, `profile_source`

Drei Diagnose-Entitäten zeigen, was die Integration gelernt hat:

- **Verbrauchsmodell**: das genutzte Modell. Attribute: Fehler jedes Modells in der Rückrechnung
  (`backtest_daily_error`, `backtest_hourly_error` in %), Umfang des Archivs (`archive_days`,
  `archive_oldest`, `archive_complete`), Grundlast (`base_load_kwh` pro Tag), Mehrverbrauch
  (`kwh_per_kelvin` pro Tag), Heizgrenze (`base_temperature`), Abweichung zum Wetterdienst
  (`weather_offset`) und die verwendeten Tagestemperaturen (`temperatures`).
- **Verbrauchsprognose Fehler**: wie weit die Prognose vom Tagesanfang in den letzten 14 Tagen vom
  echten Tagesverbrauch abwich, in %. Der Wert hat eine Langzeitstatistik, sodass du über Monate
  siehst, ob die Prognose besser wird.
- **PV-Prognose Korrektur**: gelernter Faktor zwischen echter und vorhergesagter PV-Erzeugung, mit
  den Faktoren je Tagesstunde als Attribut.

**Batterie leer (Prognose, ohne PV)** rechnet den nutzbaren Inhalt gegen das Profil und gibt den
Zeitpunkt zurück, an dem der Akku ohne PV-Ertrag nichts mehr ans Haus abgibt (bis 48 h voraus).
Nutzbar ist nur der Teil oberhalb der Reserve-Grenze `100 − x_active` (Tiefentladeschutz, Puffer und
Notstrom-Reserve), bei der Standard-Aufteilung also oberhalb von 15 %. Da die HyCube ihre Aufteilung nicht
meldet, nimmt die Integration `x_active` aus dem Request des aktiven Modus. Im Modus *Energielevel
halten* gilt die Grenze des Standard-Modus: Der Sensor zeigt dann, wie lange der gehaltene Inhalt nach
der Freigabe reicht. Die Attribute `reserve_soc` (Grenze in %) und `usable_energy_kwh` zeigen die
verwendeten Werte.

## Fahrplan

Der Fahrplan beantwortet für jede Viertelstunde bis zum Ende der bekannten Preise: Versorgt die PV das
Haus, der Akku oder das Netz, und lohnt es sich, den Akku aus dem Netz zu laden? Ziel sind die
niedrigsten Stromkosten.

Eingangsdaten:

- **Preise**: Tibber, heute und (ab etwa 13 Uhr) morgen, über die Aktion `tibber.get_prices`.
  Die Tibber-Integration muss eingerichtet sein.
- **PV-Prognose**: die Prognose, die im Energie-Dashboard bei der Solarproduktion eingetragen ist
  (z. B. Forecast.Solar). Ohne Prognose rechnet der Fahrplan ohne PV-Ertrag. Die Integration merkt
  sich die Prognose jedes Tages, vergleicht sie mit der echten Erzeugung und korrigiert künftige
  Prognosen je Tagesstunde, sobald mindestens 5 Tage vorliegen. Das gleicht Verschattung, Schmutz und
  eine ungenaue Ausrichtung aus.
- **Verbrauch**: die [Verbrauchsprognose](#verbrauchsprognose) dieser Integration.
- **Akku**: aktueller Ladezustand, Reserve-Grenze, gemessene Kapazität und gemessener Wirkungsgrad.

Der Fahrplan wird jede Viertelstunde neu berechnet, immer ab dem tatsächlichen Ladezustand. Je
Viertelstunde wählt er einen der drei Batteriemodi:

| Modus | Wann der Fahrplan ihn wählt |
| --- | --- |
| *Standard* | Normalfall: PV-Überschuss lädt den Akku, der Akku versorgt das Haus, den Rest liefert das Netz. |
| *Energielevel halten* | Der Akkuinhalt ist später mehr wert als jetzt: Das Haus läuft am Netz, der Akku wartet auf die teuren Stunden. |
| *Aus dem Netz laden* | Der Strom ist jetzt so günstig, dass sich Laden trotz der Verluste für spätere teure Stunden lohnt. |

Nach dem Verlassen von *Energielevel halten* entlädt die HyCube bis zu anderthalb Stunden nicht (siehe
[Batteriemodi](#batteriemodi)). Der Fahrplan rechnet mit dieser Wartezeit und beendet das Halten
entsprechend früher, damit der Akku zur teuren Phase wieder liefert. Im Plan steht in dieser Zeit
*Standard* mit Versorgung aus dem Netz.

Netzladung lohnt sich seltener, als die Preiskurve vermuten lässt: Von 1 kWh aus dem Netz kommen
nach Lade- und Entladeverlusten etwa 0,75 kWh im Haus an. Der teure Preis muss deshalb rund ein Drittel
über dem günstigen liegen. An Tagen mit flacher Preiskurve bleibt der Fahrplan beim Modus *Standard*.

### Sensoren

- **Fahrplan Quelle**: was das Haus gerade laut Plan versorgt (*PV*, *Akku*, *Netz*, *Akku lädt aus dem
  Netz*). Attribute: `schedule` (zusammengefasste Abschnitte mit `start`, `end`, `source`, `mode`,
  `price`, `load_kwh`, `pv_kwh`, `grid_kwh`, `battery_kwh`, `soc`), `slots` (dieselben Werte je
  Viertelstunde, z. B. für ApexCharts), `horizon_end`, `capacity_kwh`, `efficiency`, `reserve_soc`,
  `tibber_home`, `pv_forecast`, `hold_blocked`, `automatic` und `error`. Ohne Plan nennt `error` den
  Grund: `no_prices` (keine Tibber-Preise), `no_profile` (Verbrauchsprofil noch leer), `no_soc`
  (Ladezustand unbekannt) oder `failed`.
- **Fahrplan Batteriemodus**: der Modus, den der Plan jetzt vorsieht, bei Netzladung mit `target_soc`.
- **Nächste Netzladung**: Beginn des nächsten Ladeabschnitts, mit Ende, Ziel-Ladezustand, Energie und Preis.
- **Fahrplan erwartete Ersparnis**: Ersparnis gegenüber durchgehendem Standardbetrieb bis zum Ende
  des Plans, mit den Kosten beider Varianten als Attribute.

### Automatisch ausführen

Mit dem Schalter **Fahrplan automatisch ausführen** (Standard: aus) setzt die Integration den
Batteriemodus selbst, jeweils zur Viertelstunde und nur bei einer Änderung. Beobachte den Plan am besten
erst ein paar Tage, bevor du den Schalter einschaltest.

- Solange der Schalter an ist, bestimmt der Fahrplan den Modus. Eine Auswahl von Hand gilt nur bis zur
  nächsten Viertelstunde.
- Bei Netzladung setzt der Fahrplan auch den Ziel-Ladezustand.
- Ohne Preise oder ohne Plan stellt die Integration einen von ihr gesetzten Modus auf *Standard* zurück,
  ebenso beim Ausschalten des Schalters.
- Lädt die HyCube im Modus *Energielevel halten* den Akku aus dem Netz nach (siehe
  [Batteriemodi](#batteriemodi)), nimmt der Fahrplan diesen Modus für 24 Stunden aus der Planung und
  schreibt eine Warnung ins Log.
- Die Option **Batteriesteuerung aktivieren** gilt weiter: Ist sie aus, wird nichts geschaltet.

### Karte für das Dashboard

Eine Markdown-Karte zeigt den Fahrplan als Tabelle:

```yaml
type: markdown
title: HyCube Fahrplan
content: |-
  {%- set quelle = {'solar': 'PV', 'battery': 'Akku', 'grid': 'Netz', 'grid_charge': 'Akku lädt aus dem Netz'} %}
  | Zeit | Versorgung | Preis | Akku danach |
  |:--|:--|--:|--:|
  {%- for b in state_attr('sensor.hycube_fahrplan_quelle', 'schedule') or [] %}
  | {{ as_timestamp(b.start) | timestamp_custom('%a %H:%M') }}–{{ as_timestamp(b.end) | timestamp_custom('%H:%M') }} | {{ quelle[b.source] }}{{ ' (Akku halten)' if b.mode == 'hold' }} | {{ (b.price * 100) | round(1) }} ct | {{ b.soc }} % |
  {%- endfor %}
```

(Entitäts-IDs hängen von Sprache und Bereich bei der Einrichtung ab; passe sie an deine Installation an.)

## Entitäten

<details>
<summary>Vollständige Liste</summary>

**Sensoren (Echtzeit)**: Batterie Ladezustand, Leistung, Lade-/Entladeleistung, Spannung, Strom ·
Netz Leistung, Bezug/Einspeisung, Leistung L1–L3*, Spannung (gesamt, L1–L3*), Strom L1–L3*, Frequenz* ·
Hausverbrauch · PV Leistung, String 1/2 Leistung, Spannung*, Strom* · Wechselrichter Leistung*,
Spannung*, Strom* · Externe Erzeugung Leistung*

**Sensoren (Energie, kWh)**: Netzbezug, Netzeinspeisung, Batterie geladen, Batterie entladen, PV,
Externe Erzeugung*, Hausverbrauch · Batterie gespeicherte Energie

**Sensoren (Prognose)**: Batterie leer (ohne PV), Verbrauchsprognose nächste Stunde / Rest des Tages /
24 h / morgen

**Sensoren (Fahrplan)**: Fahrplan Quelle, Fahrplan Batteriemodus, Nächste Netzladung, Fahrplan
erwartete Ersparnis

**Diagnose**: Batteriekapazität (geschätzt) · Wirkungsgrad (geschätzt) · Verbrauchsmodell ·
Verbrauchsprognose Fehler · PV-Prognose Korrektur · Letzte Lückenfüllung (mit Lückenlänge, nachgetragener
Energie und nicht rekonstruierbarer Zeit als Attribute)

**Binärsensoren**: Netzladung aktiv, Batterie-Zeitplan aktiv, Notstrom aktiviert, Notstrom Überlast

**Steuerung**: Auswahl *Batteriemodus*, Zahl *Netzladung Ziel-Ladezustand*, Schalter *Fahrplan
automatisch ausführen*, Button *Verbrauchsprofil neu berechnen*

\* standardmäßig deaktiviert oder Diagnose-Entität, bei Bedarf in den Entitätseinstellungen aktivieren.

</details>

## Sicherheit

- Die HyCube spricht nur **unverschlüsseltes HTTP**. Zugangsdaten und Token gehen im Klartext
  (Base64) durchs lokale Netz. Gib die HyCube **niemals per Portweiterleitung ins Internet** frei.
- **Ändere das Standardpasswort** `hycube` in der HyWeb-Oberfläche, wenn dein Gerät das erlaubt.
  Ideal ist ein eigenes VLAN oder ein IoT-Netz für den Speicher.
- Zugangsdaten liegen wie bei jeder Integration im Config-Entry von Home Assistant. Der Diagnose-Download
  schwärzt Benutzername, Passwort und Seriennummer.
- Wer Home-Assistant-Admin ist, kann über die Options-Requests beliebige GET-Requests an die HyCube
  senden. Brauchst du die Steuerung nicht, schalte **Batteriesteuerung aktivieren** aus.

## Fehlersuche

- **„HyCube nicht erreichbar“**: IP prüfen und testen, ob `http://<IP>/` im Browser die HyWeb-Oberfläche zeigt.
- **„Too many connections“ im Log**: Andere Programme (Apps, Skripte, offene HyWeb-Tabs) belasten den
  Controller. Die Integration wartet automatisch; ggf. das Abfrageintervall erhöhen.
- **Debug-Log**: Auf der Integrationsseite **⋮ → Debug-Protokollierung aktivieren**, Fehler
  reproduzieren, dann wieder deaktivieren. Das Log wird heruntergeladen.
- **Diagnosedaten**: Geräteseite → **⋮ → Diagnosedaten herunterladen**. Bitte bei Issues anhängen.

## Technischer Hintergrund

Verwendete Endpunkte der HyCube:

| Endpunkt | Zweck |
| --- | --- |
| `GET /auth/` | Token holen (dokumentiert, API Manual v1.0) |
| `GET /info/` | Seriennummer, Modell, Versionen (dokumentiert) |
| `GET /get_values/` | Echtzeitwerte (dokumentiert) |
| `GET /data_row/` | Statusflags (undokumentiert, aus der HyWeb-Oberfläche) |
| `GET /db_today/?day=YYYY-MM-DD` | 5-Minuten-Mittelwerte eines Tages (undokumentiert) |
| `GET /smartCharging/ManualChargingActivation/` | Netzladung starten/stoppen (undokumentiert) |
| `GET /Bat/setCustomBat/` | Akku-Aufteilung Normalbetrieb/Puffer/Reserve (undokumentiert) |

## Entwicklung

Die Rechenlogik (`energy.py`, `profile.py`, `archive.py`, `model.py`, `solar.py`, `optimizer.py`)
kommt ohne Home Assistant aus und wird mit der Standardbibliothek getestet. `-P` verhindert, dass
Module des Repositorys (`select.py`, `profile.py`) Module der Standardbibliothek verdecken:

```bash
python3 -P -m unittest discover -s tests
```

Die Tests in `tests/ha` fahren die Integration in einem echten Home Assistant mit simuliertem Gerät
hoch. Sie brauchen `pytest-homeassistant-custom-component`:

```bash
cd tests/ha && pytest
```

## Mitwirken

Fehler und Wünsche bitte als [Issue](https://github.com/daddeldash/ha-hycube/issues) melden, mit
HyWeb-Version, Diagnosedaten und gegebenenfalls Debug-Log. Pull Requests sind willkommen. Besonders
hilfreich sind Rückmeldungen zu anderen Firmware-Versionen und zum Modus „Energielevel halten“.

## Lizenz

[MIT](LICENSE)
