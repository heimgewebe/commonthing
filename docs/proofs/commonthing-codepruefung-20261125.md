---
id: proofs.commonthing-codepruefung-20261125
title: Codeprüfung vom 25. November 2026
doc_type: proof
status: active
canonicality: diagnostic
created: 2026-11-25
lang: de
summary: Revisionsgebundene Codebefunde zu Knotenänderungen, Direktnachrichten, Outbox, Suche, Karte und Fadenkapazität.
relations:
  - type: relates_to
    target: docs/reports/optimierungsbericht.md
  - type: relates_to
    target: docs/reports/domain-edge-faden-lifecycle-proof.md
---

# Codeprüfung vom 25. November 2026

Geprüfter Stand ist [`63d35c96`](https://github.com/heimgewebe/commonthing/commit/63d35c96bf52d1eaae895d6676ae54650b916acc). Die betroffenen Codepfade sind auf der PR-Basis [`6d9eea4f`](https://github.com/heimgewebe/commonthing/commit/6d9eea4f25d8180a96c1f039e94a21a74d84534a) unverändert.
Die Priorität folgt der Auswirkung auf Nutzerdaten und Betrieb. Die Vorschläge beschreiben offene Folgearbeit. Die Prüfungen verwendeten synthetische Daten und isolierte Node- oder PostgreSQL-Abläufe.

## Hohe Priorität

1. **Knotenpanel kann den falschen Knoten ändern · isoliert nachgestellt.** Nach dem Wechsel von A zu B kann eine verspätete Speicherantwort für A das wiederverwendete Panel überschreiben. Speichern und Löschen beziehen die Ziel-ID aus diesen Details. Ziel-ID und Auswahl beim Start binden, verspätete Antworten nur dem zugehörigen Panel zuordnen und A→B mit verzögerter Antwort testen. [Speichern](https://github.com/heimgewebe/commonthing/blob/63d35c96bf52d1eaae895d6676ae54650b916acc/apps/web/src/lib/components/panels/NodePanel.svelte#L204-L265) · [Löschen](https://github.com/heimgewebe/commonthing/blob/63d35c96bf52d1eaae895d6676ae54650b916acc/apps/web/src/lib/components/panels/NodePanel.svelte#L289-L323)
2. **Direktnachrichten vermischen Verläufe und Entwürfe · isoliert nachgestellt.** Eine verspätete Sendeantwort für A kann in B erscheinen, Bs Vorschau überschreiben und den Entwurf leeren. Eine Blockierantwort kann die Überschrift A über dem Verlauf von B anzeigen. Entwürfe pro Gespräch führen und Antworten an die ursprüngliche Gesprächs-ID und Auswahl binden. Die Folgeprüfung sollte auch einen Wechsel während Senden und Blockieren abdecken. [Nachrichtenansicht](https://github.com/heimgewebe/commonthing/blob/63d35c96bf52d1eaae895d6676ae54650b916acc/apps/web/src/routes/nachrichten/+page.svelte#L109-L231)
3. **Verspäteter Outbox-Abschluss kann eine bestätigte Zustellung in Quarantäne belassen · PostgreSQL.** Die Reservierung gilt je Eintrag ab Beginn der Charge, der Versand läuft nacheinander. Nach Wiedervergabe setzt ein alter Fehlerversuch den Eintrag noch in Quarantäne. Der neuere Erfolg ändert dann null Zeilen. Beide Abschlüsse an eine eindeutige Reservierungskennung binden und die Zahl aktualisierter Zeilen prüfen. [Reservierung und Abschluss](https://github.com/heimgewebe/commonthing/blob/63d35c96bf52d1eaae895d6676ae54650b916acc/apps/api/src/outbox.rs#L390-L466) · [Versand](https://github.com/heimgewebe/commonthing/blob/63d35c96bf52d1eaae895d6676ae54650b916acc/apps/api/src/outbox.rs#L524-L549)

## Weitere Fehler und Verbesserungen

- **Semantischer Suchindex kann Modellgenerationen vermischen · PostgreSQL.** Ein für B konfigurierter Worker kann einen Auftrag der aktiven Generation A übernehmen. Im Test akzeptierte A einen simulierten B-Vektor gleicher Dimension. Die Auftragsauswahl an die konfigurierte Generation binden und mit zwei gleich dimensionierten Testmodellen absichern. [Auswahl](https://github.com/heimgewebe/commonthing/blob/63d35c96bf52d1eaae895d6676ae54650b916acc/apps/api/src/search/worker.rs#L615-L637) · [Vektorspeicherung](https://github.com/heimgewebe/commonthing/blob/63d35c96bf52d1eaae895d6676ae54650b916acc/apps/api/src/search/worker.rs#L992-L1024)
- **Paralleles Anlegen und Löschen von Knoten kann einen Deadlock auslösen · Codeprüfung.** Anlegen sperrt über den Trigger zuerst die Projektionsgeneration, Löschen zuerst `domain_edges`. Sperrreihenfolge vereinheitlichen und zwei Transaktionen mit verschiedenen Knoten parallel prüfen. [Anlegen](https://github.com/heimgewebe/commonthing/blob/63d35c96bf52d1eaae895d6676ae54650b916acc/apps/api/src/domain_db.rs#L3259-L3295) · [Löschen](https://github.com/heimgewebe/commonthing/blob/63d35c96bf52d1eaae895d6676ae54650b916acc/apps/api/src/domain_db.rs#L1447-L1465) · [Trigger](https://github.com/heimgewebe/commonthing/blob/63d35c96bf52d1eaae895d6676ae54650b916acc/apps/api/migrations/20260716000001_multi_instance_foundation.up.sql#L100-L134)
- **Karte kann eine Aktualisierung beim Start überspringen · isoliert nachgestellt.** Eine Invalidierung während der ersten Ausschnittsabfrage kann bei gleichem Ausschnitt am frisch gesetzten Cache hängenbleiben. Bei einer Dateninvalidierung den vorgemerkten Abruf unabhängig von der Cache-Abdeckung durchführen. [Cache-Abdeckung](https://github.com/heimgewebe/commonthing/blob/63d35c96bf52d1eaae895d6676ae54650b916acc/apps/web/src/routes/map/+page.svelte#L758-L802) · [Wiederholung](https://github.com/heimgewebe/commonthing/blob/63d35c96bf52d1eaae895d6676ae54650b916acc/apps/web/src/routes/map/+page.svelte#L879-L900)
- **Ältere Direktnachrichten fehlen im Postfach · Codeprüfung.** Die Oberfläche zeigt die erste Seite mit bis zu 50 Nachrichten und verwirft den Cursor. Die vorhandene Nachladelogik der Knotengespräche kann als Vorlage dienen. [Client](https://github.com/heimgewebe/commonthing/blob/63d35c96bf52d1eaae895d6676ae54650b916acc/apps/web/src/lib/api/directMessages.ts#L120-L125) · [Seitengröße](https://github.com/heimgewebe/commonthing/blob/63d35c96bf52d1eaae895d6676ae54650b916acc/apps/web/src/lib/api/nodeConversation.ts#L139-L148) · [Vorlage](https://github.com/heimgewebe/commonthing/blob/63d35c96bf52d1eaae895d6676ae54650b916acc/apps/web/src/lib/components/panels/NodeConversation.svelte#L181-L197)
- **Abgelaufene Fäden zählen gegen das Limit · PostgreSQL.** Die Kapazitätsabfrage erreichte im Test das Limit bei null aktiven Fäden. Das kann auch das Anlegen eines Knotens blockieren, wenn dessen Herkunftsfaden im selben Commit geschrieben werden muss. Für die bestehende Kapazitätsarbeit aktiven Bestand und persistierte Historie für Idempotenz getrennt abbilden. [Kapazitätsabfrage](https://github.com/heimgewebe/commonthing/blob/63d35c96bf52d1eaae895d6676ae54650b916acc/apps/api/src/domain_db.rs#L3077-L3095) · [Dokumentierter Vorbefund](https://github.com/heimgewebe/commonthing/blob/63d35c96bf52d1eaae895d6676ae54650b916acc/docs/reports/domain-edge-faden-lifecycle-proof.md#L149-L161)

## Prüfumfang

Die isolierten UI-Prüfungen verwendeten die Originalfunktionen mit verzögerten Testantworten. Die Datenbankprüfungen verwendeten Originalabfragen und synthetische Daten in PostgreSQL 16.
Die Befunde betreffen diese Codepfade. Für die Folge-PRs bleiben Browser-End-to-End-Tests und ein PostgreSQL-Test mit zwei konkurrierenden Transaktionen die nächsten Nachweise.
[CI des geprüften Standes](https://github.com/heimgewebe/commonthing/actions/runs/45461374630).
