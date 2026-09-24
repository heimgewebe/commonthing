---
id: specs.private-nachrichten
title: Private Nachrichten zwischen Konten
summary: Kanonischer Produkt-, Datenschutz- und Ausführungsvertrag für private 1:1-Unterhaltungen in commonThing.
doc_type: specification
status: canonical
canonicality: normative
lifecycle_state: active
role: norm
organ: product
owner: product
last_reviewed: 2026-08-04
review_after: 2026-10-22
depends_on:
  - specs.garnrolle-knoten-faden
relations:
  - type: relates_to
    target: docs/specs/privacy-api.md
  - type: relates_to
    target: docs/specs/objektlebenszyklen-und-loeschwirkungen.md
  - type: relates_to
    target: docs/adr/ADR-0012__ereignisrueckgrat-transactional-outbox.md
verifies_with:
  - apps/api/tests/db_node_conversations.rs
  - apps/api/src/routes/conversations.rs
  - apps/api/src/notifications.rs
  - apps/api/migrations/20260804000001_web_push_notifications.up.sql
  - apps/web/src/lib/api/directMessages.ts
  - apps/web/src/lib/api/notifications.ts
  - apps/web/src/lib/components/NotificationSettings.svelte
  - apps/web/static/sw.js
  - apps/web/src/routes/nachrichten/+page.svelte
attention_source_status: source
attention_source_rationale: "Ungelesene Direktunterhaltungen liefern kanonische persönliche Neuheit, ohne daraus eine Antwortpflicht abzuleiten."
attention_source_facts:
  - direct conversation participant relation
  - direct conversation unread_count
  - direct conversation last_message_at beziehungsweise updated_at
attention_projection:
  - apps/web/src/lib/components/topBarAttentionState.ts
attention_transition_tests:
  - apps/web/src/lib/components/topBarAttentionState.test.ts
  - apps/web/tests/attention-bubbles.spec.ts
---

# Private Nachrichten zwischen Konten

## Zweck

Angemeldete Konten können sich direkt austauschen, ohne den Inhalt in einem
öffentlichen Knoten- oder Antragsgespräch zu veröffentlichen. Eine private
Unterhaltung besitzt genau zwei Teilnehmer und ist nicht Teil der Karte, der
Suche, der Föderation oder der öffentlichen Fadenprojektion.

## Sichtbarkeit und Sicherheitsgrenze

Eine private Unterhaltung trägt den Typ `direct` und die Sichtbarkeit
`participants`.

- Nur die zwei live gebundenen Teilnehmer dürfen Metadaten und Nachrichten
  lesen oder verändern.
- Nicht angemeldete Besucher, fremde Konten und Administratoren erhalten für
  eine unbekannte oder fremde Unterhaltungs-ID dieselbe Antwort `404`.
- Administratorstatus allein hebt die Teilnehmergrenze nicht auf.
- Nachrichten werden verschlüsselt übertragen und serverseitig gespeichert,
  sind aber **nicht Ende-zu-Ende verschlüsselt**. Der Server muss den Klartext
  zur Zustellung und Darstellung verarbeiten können.
- Inhalte werden nicht in öffentliche Such-, Karten-, Faden- oder
  Föderationsprojektionen übernommen.

Diese Grenze schützt vor gewöhnlichem Zugriff anderer Nutzer. Sie ersetzt
keine Ende-zu-Ende-Verschlüsselung und keinen gesonderten rechtlichen
Aufbewahrungsvertrag.

## Gesprächsmodell

Pro ungeordnetem Kontopaar existiert höchstens eine aktive private
Unterhaltung. Gleichzeitige Anfragen `A → B` und `B → A` führen daher zu
demselben Gespräch statt zu Dubletten.

Jeder Teilnehmerdatensatz enthält:

- den live gebundenen Account, solange er besteht;
- einen unveränderlichen Anzeigenamen-Snapshot;
- den eigenen Gelesen-Zeitpunkt;
- den eigenen Blockierzustand.

Nachrichten verwenden den kanonischen Nachrichtenspeicher. Damit gelten auch
für private Nachrichten:

- 1 bis 4.000 Zeichen Klartext;
- idempotentes Senden über `Idempotency-Key`;
- optimistische Versionsprüfung bei Änderung oder Entfernung;
- Anzeigename-Snapshot für nachvollziehbare Historie.

## Bedienung

Eine angemeldete Person kann an einer fremden Garnrolle **Private Nachricht**
auswählen. Das Postfach unter `/nachrichten` zeigt:

- Gegenüber und letzte Nachricht;
- Zahl ungelesener Nachrichten;
- chronologische Unterhaltung;
- Nachrichteneingabe;
- Blockieren oder Freigeben.

Das eigene Konto wird nicht als Empfänger angeboten. Beim Öffnen einer
Unterhaltung sendet der Browser die Kennung der neuesten tatsächlich geladenen
Nachricht zurück. Der Server markiert nur bis zu genau dieser Nachricht als
gelesen; eine gleichzeitig eintreffende spätere Nachricht bleibt ungelesen.

## Blockieren

Jeder Teilnehmer kann die Unterhaltung für sich blockieren. Solange mindestens
einer der beiden Teilnehmer blockiert hat, dürfen beide Seiten keine neue
Nachricht senden. Bereits vorhandene Nachrichten bleiben für beide Teilnehmer
lesbar; ihr jeweiliger Autor darf sie weiterhin ändern oder entfernen. Die
Sperre gilt ausschließlich für neue Nachrichten. Ein Teilnehmer kann nur den
eigenen Blockierzustand aufheben.

Beide Seiten erfahren beim Laden der Unterhaltung, ob gesendet werden darf. Die
Unterhaltung führt dafür eine ausdrückliche Sendeerlaubnis, die genau dann gilt,
wenn beide Konten aktiv sind und keine Seite blockiert hat. Sie nennt nicht,
welche Seite blockiert hat: Der eigene Blockierzustand ist gesondert sichtbar,
eine Blockade des Gegenübers erscheint nur als fehlende Sendeerlaubnis. Damit
bietet die Oberfläche keine Eingabe an, die der Server anschließend ablehnt.

## Missbrauchsgrenzen

Die erste produktive Fassung begrenzt serverseitig:

- neue private Unterhaltungen eines Kontos auf 20 pro Stunde;
- private Nachrichten eines Kontos auf 30 pro Minute über alle Unterhaltungen;
- zusätzlich weiterhin auf 10 Nachrichten pro Minute innerhalb derselben
  Unterhaltung.

Die Zählung und Prüfung erfolgt unter PostgreSQL-Sperren, damit parallele
Anfragen die Grenzen nicht umgehen.

## Konto-Austritt und Identität

Wird ein Teilnehmerkonto gelöscht, wird seine live Accountbindung in der
Unterhaltung entfernt. Der Anzeigename-Snapshot und die bisherige Historie
bleiben für den noch vorhandenen Teilnehmer lesbar. Neue Nachrichten setzen
zwei weiterhin aktive Accountbindungen voraus und werden nach dem Austritt
eines Teilnehmers serverseitig abgelehnt.

Ein stillgelegtes Konto gilt dabei als ausgeschieden. Es kann sich nicht mehr
anmelden und deshalb weder lesen noch senden; für den verbleibenden Teilnehmer
verhält sich die Unterhaltung wie nach einer Löschung. Darstellung und
Sendeprüfung verwenden dieselbe Grenze, damit die Oberfläche keine Zustellung
anbietet, die der Server ablehnt. Wird das Konto später wieder aktiviert, lebt
die bestehende Unterhaltung unverändert weiter.

Wird dieselbe textuelle Account-ID später erneut vergeben, erhält das neue
Konto keinen Zugriff auf die alte Unterhaltung. Das frühere Kontopaar gilt als
beendet und kann nicht stillschweigend reaktiviert werden.

Für Unterhaltungen, bei denen beide Konten ausgeschieden sind, besteht in der
ersten Fassung noch keine automatische Löschfrist. Sie sind für Nutzer nicht
mehr erreichbar; eine verbindliche Aufbewahrungs- und Löschregel wird separat
festgelegt.

## Freiwilliger Web Push

Private Nachrichten können zusätzlich als Web-Push-Hinweis zugestellt werden.
Das Postfach und der kanonische Nachrichtenspeicher bleiben dabei die
verbindliche Wahrheit. Push ist weder Lesebestätigung noch garantierte
Zustellung und darf den Zugriff auf `/nachrichten` nicht ersetzen.

Die Freigabe besitzt zwei getrennte Ebenen:

- Das Konto schaltet Push für private Nachrichten ausdrücklich an oder aus.
- Jedes Gerät erhält zusätzlich eine eigene Browserfreigabe und ein eigenes
  widerrufbares Push-Abonnement.
- Pro Konto sind höchstens 20 gleichzeitig aktive Geräteabonnements zulässig,
  damit ein einzelnes Konto die Zustellfächerung nicht unbegrenzt vergrößern kann.

Die Browserberechtigung wird erst nach einer bewussten Aktion in der
Benachrichtigungsverwaltung angefragt. Nicht unterstützte oder blockierte Browser verändern
die Kontoeinstellung und das Postfach nicht.

Der verschlüsselte Push-Inhalt ist absichtlich neutral. Er enthält weder
Nachrichtentext noch Absendername oder Account-ID, sondern nur die Ereignisart,
einen gleichursprünglichen Verweis auf die private Unterhaltung und eine
Zusammenfassungskennung für das Betriebssystem.

Zustellaufträge entstehen erst aus dem bereits bestätigten
`domain.message.created`-Ereignis der transaktionalen Outbox. Pro Ereignis und
Geräteabonnement existiert höchstens ein Zustellbeleg. Vorübergehende Fehler
werden begrenzt wiederholt; dauerhaft abgelaufene Browserabonnements werden
stillgelegt. Ein Push-Fehler verändert oder entfernt niemals die Nachricht.

Beim Ausschalten werden noch nicht abgeschlossene Zustellaufträge abgebrochen.
Ein Hinweis, den ein externer Push-Anbieter in diesem Moment bereits angenommen
hat, kann technisch nicht zurückgerufen werden; die Kontoeinstellung verhindert
aber jeden späteren Claim oder Wiederholungsversuch.

Der Zustellkanal wird nur aktiv, wenn PostgreSQL, NATS JetStream und eine
vollständige VAPID-Konfiguration verfügbar sind. Ausgehende Push-Endpunkte
müssen zu einer ausdrücklich zugelassenen DNS-Endung gehören. Eine fehlende
Konfiguration lässt Push geschlossen, während das Postfach normal weiterläuft.

## Nicht Teil der ersten Fassung

- Ende-zu-Ende-Verschlüsselung und Schlüsselwiederherstellung;
- E-Mail-Benachrichtigungen und Push für Erwähnungen, Anträge oder Fristen;
- Gruppenunterhaltungen;
- Anhänge;
- Meldungs- und Moderationsworkflow über das Blockieren hinaus;
- Paginierung sehr großer Postfächer und vollständiger Historien.
