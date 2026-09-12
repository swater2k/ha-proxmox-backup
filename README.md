# Proxmox Backup Server für Home Assistant

Custom Integration, die einen Proxmox Backup Server (PBS) über die REST-API in Home
Assistant abbildet: Host-Zustand, Datastore-Belegung, Garbage Collection, Verify-Abdeckung
und – ab Meilenstein 2 – jede Backup-Gruppe einzeln.

## Was diese Integration nicht kann

**PBS kann keine Backups anstoßen.** Backups werden immer vom Client gepusht, also von
Proxmox VE (`vzdump`) oder vom `proxmox-backup-client`. Über die PBS-API gibt es keinen
Endpunkt „sichere jetzt CT 107". Ein Button dafür gehört auf die PVE-Seite, nicht hierher.

Steuerbar sind ausschließlich PBS-eigene Operationen: Garbage Collection, Verify, Prune,
Forget, Protect, Notizen, Wartungsmodus und das Starten konfigurierter Jobs. Diese
Aktionen kommen in Meilenstein 3.

## Installation

1. In HACS unter *Benutzerdefinierte Repositories* dieses Repository als Kategorie
   *Integration* hinzufügen.
2. „Proxmox Backup Server" installieren, Home Assistant neu starten.
3. *Einstellungen → Geräte & Dienste → Integration hinzufügen → Proxmox Backup Server*.

Entfernen: Integration in *Geräte & Dienste* löschen. Es bleiben keine Dateien zurück,
auf dem PBS wird nichts verändert. Den API-Token danach in PBS widerrufen.

## API-Token anlegen

PBS bildet die Schnittmenge aus Benutzer- und Token-Rechten. Die ACL muss deshalb
**zweimal** gesetzt werden, einmal auf den Benutzer und einmal auf den Token:

```bash
proxmox-backup-manager user create ha@pbs
proxmox-backup-manager user generate-token ha@pbs homeassistant
# Der Wert unter "value" ist das Secret und wird nur ein einziges Mal angezeigt.

proxmox-backup-manager acl update /datastore/<datastore> DatastoreAdmin --auth-id 'ha@pbs!homeassistant'
proxmox-backup-manager acl update /datastore/<datastore> DatastoreAdmin --auth-id 'ha@pbs'
proxmox-backup-manager acl update /system Audit --auth-id 'ha@pbs!homeassistant'
proxmox-backup-manager acl update /system Audit --auth-id 'ha@pbs'
```

`Audit` auf `/system` liefert Node-Status, Dienste, Zertifikate, Tasks, Updates und die
Plattenliste inklusive SMART. Fehlt es, funktioniert alles andere trotzdem: die
betroffenen Entitäten bleiben schlicht nicht verfügbar, und im Log steht einmalig,
welcher Endpunkt abgelehnt wurde.

Wer keine Aktionen auslösen will, kann statt `DatastoreAdmin` auch `DatastoreAudit`
vergeben – dann sind alle Sensoren da, aber Meilenstein 3 bleibt wirkungslos.

## Konfiguration

| Feld | Bedeutung |
|---|---|
| Host / Port | Adresse des PBS, Standardport 8007 |
| Token-ID | Vollständig, z. B. `ha@pbs!homeassistant` |
| Token-Secret | Der `value` aus `generate-token` |
| TLS-Zertifikat prüfen | Für das mitgelieferte selbstsignierte Zertifikat ausschalten |

Danach werden die sichtbaren Datastores angeboten; nur für die ausgewählten entstehen
Entitäten. In den Optionen lassen sich später anpassen: Datastore-Auswahl, Schwelle für
veraltete Backups, Warn- und Kritisch-Schwelle der Belegung, GC-Warnung sowie die beiden
Schalter für Schreib- und Löschaktionen (beide standardmäßig aus).

## Datenaktualisierung

Drei getrennte Abfrageintervalle, weil sich die Daten unterschiedlich schnell ändern:

| Intervall | Inhalt |
|---|---|
| 60 s | Host-Last, Datastore-Belegung, aktive Lese-/Schreibvorgänge, laufende Tasks |
| 5 min | Backup-Gruppen, Snapshots, Garbage Collection, Jobs, fehlgeschlagene Tasks |
| 6 h | Version, Dienste, Zertifikate, Updates, Platten/SMART |

Ab etwa 1500 Snapshots wird der Snapshot-Detailabruf automatisch auf rund stündlich
gedrosselt; Anzahl und Gruppen kommen dann aus der günstigeren Zählung von PBS selbst.

## Entitäten

**Gerät PBS-Instanz:** CPU, IO-Wait, Load (1/5/15), RAM, Swap, Root-Dateisystem,
Startzeit, laufende Tasks, fehlgeschlagene Tasks (24 h / 7 d), letzter fehlgeschlagener
Task, Version, verfügbare Updates, Zertifikatsablauf, Subscription, Verbindung,
Dienstproblem, Plattenproblem.

**Gerät je Datastore:** belegt / frei / gesamt, Belegung in Prozent, voraussichtlich voll,
aktive Lese- und Schreibvorgänge, Backup-Gruppen, Snapshots, geprüfte / fehlgeschlagene /
ungeprüfte Snapshots, ältester und neuester Snapshot, Deduplizierungsfaktor,
GC-Zeitpunkt / -Status / -Dauer / -freigegeben / -ausstehend / -Zeitplan, GC läuft,
Wartungsmodus, Sammelstatus „Problem" mit Begründungen als Attribut.

Selten gebrauchte Werte (Load 5/15, RAM gesamt, IO-Wait, fehlgeschlagene Tasks 7 d,
GC ausstehend) sind standardmäßig deaktiviert und lassen sich pro Entität einschalten.

## Fehlersuche

| Symptom | Ursache |
|---|---|
| „Kein Datastore sichtbar" beim Einrichten | ACL fehlt auf dem Benutzer hinter dem Token. Beide Zeilen aus dem Abschnitt oben ausführen. |
| Plattenproblem-Sensor nicht verfügbar | `Audit` auf `/system` fehlt. `/system/status` allein deckt `/system/disks` nicht ab. |
| Host nicht erreichbar | TLS-Prüfung ist an, PBS nutzt aber sein selbstsigniertes Zertifikat. |
| Token abgelehnt | Der Reauth-Dialog erscheint automatisch; dort ein neues Secret hinterlegen. |

Für Fehlerberichte hilft der Diagnose-Download der Integration: er enthält die rohen
API-Antworten, Secrets und Kennungen sind entfernt.

## Entwicklung

Testdaten der eigenen Instanz aufnehmen (Meilenstein 0):

```bash
python3 scripts/dump_fixtures.py \
  --host 192.168.178.138 --token-id 'ha@pbs!homeassistant' \
  --token-secret '…' --insecure
```

Schreibt jede API-Antwort nach `tests/fixtures/`; abgelehnte Endpunkte landen in
`_errors.json`. Diese Dateien sind die Grundlage der Unit-Tests aus Meilenstein 5.

## Stand

- **M1 – erledigt:** API-Client, Config-Flow mit Datastore-Auswahl, Reauth und
  Reconfigure, drei Coordinatoren, Instanz- und Datastore-Entitäten, Diagnostics.
- **M2:** Geräte je Backup-Gruppe, dynamisches Nachziehen neuer Gruppen, Job-Entitäten,
  Gesamtstatus über alle Gruppen.
- **M3:** Aktionen (GC, Verify, Prune, Forget, Jobs, Wartungsmodus) mit Task-Verfolgung
  und den beiden Sicherheitsschaltern.
- **M4:** Repair-Issues bei fehlenden Rechten, Icon-Übersetzungen, eigener CA-Pfad.
- **M5:** Tests gegen die Fixtures, CI, Beispiel-Dashboard.
