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
- **Verbrauchsprofil und Prognose**: Aus den letzten Wochen entsteht ein Wochentag-×-Stunde-Profil
  (neuere Wochen zählen stärker). Daraus folgen Prognosen für die nächste Stunde, den Rest des Tages,
  die nächsten 24 h und morgen sowie eine Schätzung, **wann der Akku ohne PV leer wäre**.
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

Erreichbar über **Geräte & Dienste → HyCube → Konfigurieren**.

| Option | Standard | Beschreibung |
| --- | --- | --- |
| Abfrageintervall Echtzeitwerte | 10 s | Wie oft `/get_values/` abgefragt wird (min. 5 s). Kürzere Intervalle machen die Energiezähler genauer, belasten aber den Controller. |
| Abfrageintervall Status | 60 s | Wie oft die Statusflags (`/data_row/`) gelesen werden. |
| Nutzbare Batteriekapazität | 10 kWh | Für „gespeicherte Energie“ und die Prognose „Batterie leer“. Trage den Wert deines Speichers ein. |
| Wochen Historie für das Profil | 8 | Zeitraum, aus dem das Verbrauchsprofil gebaut wird. |
| Profil-Quelle | leer | Optional ein beliebiger Energie-Sensor mit Langzeitstatistik. Leer = Tagesstatistik der HyCube (Fallback: Hausverbrauch-Zähler dieser Integration). |
| Batteriesteuerung aktivieren | an | Ist sie aus, lehnt die Integration jede Modusänderung ab und liest nur noch. |
| Request für Modus … | siehe unten | Welche HTTP-Requests ein Modus auslöst. |

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
> Erkennt die Integration Batterieladung ohne PV, schreibt sie eine Warnung ins Log. Beobachte das
> Verhalten deines Geräts, bevor du den Modus in Automationen nutzt.

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

Das Profil wird täglich um 00:10 Uhr und beim Start neu berechnet, auf Wunsch auch über den Button
**Verbrauchsprofil neu berechnen**. Die Prognose-Sensoren sind verfügbar, sobald mindestens ein voller
Tag an Daten vorliegt.

Der Sensor **Verbrauchsprognose 24 h** hat zusätzlich diese Attribute:

- `hourly`: Liste `[{start, kwh}]` für die nächsten 24 vollen Stunden, z. B. für ApexCharts
- `profile_today`: 24 Stundenwerte des heutigen Wochentags
- `profile_days_of_data`, `profile_built`, `profile_source`

**Batterie leer (Prognose, ohne PV)** rechnet den nutzbaren Inhalt gegen das Profil und gibt den
Zeitpunkt zurück, an dem der Akku ohne PV-Ertrag nichts mehr ans Haus abgibt (bis 48 h voraus).
Nutzbar ist nur der Teil oberhalb der Reserve-Grenze `100 − x_active` (Tiefentladeschutz, Puffer und
Notstrom-Reserve), bei der Standard-Aufteilung also oberhalb von 15 %. Da die HyCube ihre Aufteilung nicht
meldet, nimmt die Integration `x_active` aus dem Request des aktiven Modus. Im Modus *Energielevel
halten* gilt die Grenze des Standard-Modus: Der Sensor zeigt dann, wie lange der gehaltene Inhalt nach
der Freigabe reicht. Die Attribute `reserve_soc` (Grenze in %) und `usable_energy_kwh` zeigen die
verwendeten Werte.

Beispiel: Um 2 Uhr nachts aus dem Netz laden, wenn der Akku sonst vor 10 Uhr leer wäre (z. B. bei
günstigem Nachttarif). Zurück auf *Standard* schaltet eine zweite Automation oder die HyCube selbst,
sobald der Ziel-Ladezustand erreicht ist.

```yaml
triggers:
  - trigger: time
    at: "02:00:00"
conditions:
  - condition: template
    value_template: >
      {{ has_value('sensor.hycube_batterie_leer_prognose_ohne_pv')
         and as_datetime(states('sensor.hycube_batterie_leer_prognose_ohne_pv')) < today_at('10:00') }}
actions:
  - action: select.select_option
    target:
      entity_id: select.hycube_batteriemodus
    data:
      option: grid_charge
```

(Entitäts-IDs hängen von der Sprache bei der Einrichtung ab; passe sie an deine Installation an.)

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

**Diagnose**: Letzte Lückenfüllung (mit Lückenlänge, nachgetragener Energie und nicht rekonstruierbarer
Zeit als Attribute)

**Binärsensoren**: Netzladung aktiv, Batterie-Zeitplan aktiv, Notstrom aktiviert, Notstrom Überlast

**Steuerung**: Auswahl *Batteriemodus*, Zahl *Netzladung Ziel-Ladezustand*, Button *Verbrauchsprofil neu berechnen*

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

## Mitwirken

Fehler und Wünsche bitte als [Issue](https://github.com/daddeldash/ha-hycube/issues) melden, mit
HyWeb-Version, Diagnosedaten und gegebenenfalls Debug-Log. Pull Requests sind willkommen. Besonders
hilfreich sind Rückmeldungen zu anderen Firmware-Versionen und zum Modus „Energielevel halten“.

## Lizenz

[MIT](LICENSE)
