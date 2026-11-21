# Mega Backup für Home Assistant

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz)
[![Validate](https://github.com/Schnax3/mega2backup/actions/workflows/validate.yml/badge.svg)](https://github.com/Schnax3/mega2backup/actions/workflows/validate.yml)

Nutze dein [Mega.io](https://mega.io)-Konto als Speicherort für Home-Assistant-Backups. Die Integration registriert einen **Backup-Agent**, der in den normalen Backup-Einstellungen (Einstellungen → System → Backups) neben „Lokal" und „Home Assistant Cloud" auftaucht.

## Funktionen

- Backups automatisch oder manuell nach Mega hochladen (mit Fortschrittsanzeige)
- Backups aus Mega auflisten, herunterladen, wiederherstellen und löschen
- Gelöschte Backups werden **endgültig** entfernt und belegen keinen Platz im Mega-Papierkorb
- Mehrere Mega-Konten möglich, Re-Authentifizierung bei geändertem Passwort

## Installation (HACS)

1. [![Repository in HACS öffnen](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=Schnax3&repository=mega2backup&category=integration) oder in HACS → ⋮ → *Benutzerdefinierte Repositories* → `https://github.com/Schnax3/mega2backup`, Kategorie *Integration*.
2. „Mega Backup" herunterladen und Home Assistant neu starten.
3. Einstellungen → Geräte & Dienste → *Integration hinzufügen* → **Mega Backup**.
4. E-Mail, Passwort und Namen des Backup-Ordners eingeben (Standard: `Home Assistant Backups`, wird im Hauptverzeichnis angelegt).
5. Unter Einstellungen → System → Backups → *Backup-Speicherorte* den neuen Speicherort aktivieren.

Manuell: den Ordner `custom_components/mega2backup` nach `<config>/custom_components/` kopieren.

## Gespeicherte Dateien

Pro Backup entstehen zwei Dateien im Backup-Ordner:

| Datei | Inhalt |
| --- | --- |
| `<id>.tar` | das Backup |
| `<id>.metadata.json` | Name, Datum, Größe, enthaltene Komponenten |

Die Metadaten werden zuletzt hochgeladen, unvollständige Uploads tauchen deshalb nicht in der Liste auf.

## Hinweise und Einschränkungen

- **Zwei-Faktor-Authentifizierung wird nicht unterstützt** (die verwendete Bibliothek kann das noch nicht). Das Konto muss ohne 2FA angemeldet werden können.
- **Temporärer Speicherplatz:** Die Mega-Bibliothek arbeitet mit Dateien. Beim Hochladen und Herunterladen wird das Backup kurz im temporären Verzeichnis des Systems zwischengespeichert. Dort muss also Platz für ein komplettes Backup sein.
- **Verschlüsselung:** Mega verschlüsselt Dateien Ende-zu-Ende. Zusätzlich solltest du die Backup-Verschlüsselung von Home Assistant aktiviert lassen, damit Backups auch beim Herunterladen aus Mega geschützt bleiben.
- Das Passwort wird (wie bei allen Integrationen) im Klartext in `.storage/core.config_entries` abgelegt. Nutze ein eigenes Konto für Backups, wenn dich das stört.
- Die Anbindung nutzt die inoffizielle Bibliothek [`async-mega-py`](https://pypi.org/project/async-mega-py/). Änderungen an der Mega-API können die Integration vorübergehend außer Betrieb setzen.
- Benötigt Home Assistant 2025.6 oder neuer.

## Fehlersuche

```yaml
logger:
  logs:
    custom_components.mega2backup: debug
    mega: debug
```

## Entwicklung

Der Repo enthält automatische Prüfungen (GitHub Actions): Hassfest, HACS-Validierung, Ruff, CodeQL sowie Dependabot, Release Drafter und einen Stale-Bot. Releases entstehen aus dem Release-Drafter-Entwurf; vor dem Veröffentlichen die `version` in `custom_components/mega2backup/manifest.json` anpassen.

Lizenz: MIT. Dieses Projekt steht in keiner Verbindung zu Mega Limited.
