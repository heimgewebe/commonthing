---
id: architecture.semantic-search
title: Semantic Search v1
summary: Verbindliche Wahrheits-, Sichtbarkeits-, Ranking-, Modell- und Betriebsgrenzen für die interne hybride Knotensuche.
doc_type: architecture
status: canonical
canonicality: normative
lifecycle_state: active
role: norm
organ: product-domain
owner: product-domain
last_reviewed: 2026-07-19
review_after: 2026-10-18
depends_on:
  - overview
  - architecture.weltgewebe-os
  - specs.garnrolle-knoten-faden
relations:
  - type: relates_to
    target: architecture/security.md
  - type: relates_to
    target: docs/specs/federation-core.md
  - type: relates_to
    target: docs/specs/ui-interaction.md
verifies_with:
  - contracts/search/relevance-goldset.schema.json
  - contracts/search/examples/relevance-goldset.example.json
  - contracts/search/examples/relevance-benchmark.heim-pc.json
  - scripts/search/validate_relevance_goldset.py
  - scripts/search/benchmark_relevance.py
  - scripts/ci/tests/test_semantic_search_contract.py
  - contracts/search/postgres-foundation-receipt.schema.json
  - contracts/search/examples/postgres-foundation.heim-pc.json
  - scripts/search/probe_postgres_foundation.py
  - scripts/ci/tests/test_semantic_search_postgres_foundation.py
  - contracts/search/postgres-foundation.up.sql
  - contracts/search/postgres-foundation.down.sql
  - contracts/search/hybrid-ranking-core-receipt.schema.json
  - contracts/search/examples/hybrid-ranking-core.heim-pc.json
  - scripts/search/hybrid_ranking_core.py
  - scripts/search/probe_hybrid_ranking_core.py
  - scripts/ci/tests/test_semantic_search_ranking_core.py
---

# Semantic Search v1

## 1. Entscheidung und Geltung

Die nützliche Semantik-Schicht wird direkt in `heimgewebe/commonthing` integriert. Sie ist keine allgemeine Semantikplattform und kein eigener Dienst. Ihr Zweck ist ausschließlich eine interne, hybride und sichtbarkeitsgebundene Suche nach commonThing-Knoten sowie eine klar getrennte Oberfläche „Ähnliche Knoten“.

PostgreSQL ist die einzige persistente Wahrheit der späteren Suchprojektion. Lexikalische Repräsentationen, Embeddings, Rangmerkmale und Indexzustände sind vollständig regenerierbare Projektionen aus kanonischen Knotenrevisionen.

T001 ist ein Architektur- und Testgrundlagen-Schnitt. Es führt weder Datenbankmigrationen noch Suchrouten, Embedding-Provider, Worker, Backfills, Weboberflächen, Produktionsänderungen oder SemantAH-Stilllegung ein.

## 2. Dialektik und alternative Sinnachse

**These:** Ein semantischer Retrieval-Anteil kann natürliche Anfragen auffangen, die exakte Begriffe, Tags oder Schreibweisen verfehlen. Innerhalb des Weltgewebes kann er dieselben Sichtbarkeits-, Lösch-, Revisions- und Multi-Instance-Grenzen wie die übrige Domänenlogik verwenden.

**Antithese:** PostgreSQL-Volltext und Trigramme können den praktisch relevanten Gewinn bereits liefern. Ein externer Vektorindex oder Cloud-Provider würde eine zweite Zustandswahrheit, neue Kosten- und Verfügbarkeitsabhängigkeiten sowie schwierigere Löschfortpflanzung erzeugen.

**Synthese:** Die Suche bleibt hybrid und messgetrieben. Exakte und lexikalische Signale führen. Semantik ergänzt sie nur, wenn T002 einen praktisch relevanten Mehrwert belegt. Das kleinste lokale mehrsprachige Modell gewinnt, sofern es die Qualitätsgrenzen erfüllt. ANN oder HNSW wird erst nach einem gemessenen PostgreSQL-Skalierungsengpass erwogen.

Wird kurzfristige Liefergeschwindigkeit höher als Datenschutz und Wahrheitsgrenzen gewichtet, wäre ein externer Embedding-Dienst schneller. Dieser Pfad ist verworfen. Wird ausschließlich lokale Souveränität gewichtet, könnte ein großes lokales Modell ohne Nutzenbeleg gewählt werden. Auch dieser Pfad ist verworfen: Relevanzgewinn pro Betriebsaufwand ist maßgeblich.

## 3. Belegter Ausgangszustand

Am T001-Basisstand `4b9b0507e5d54eaa285d91dbb47a3f2cf74f4ccc` gilt:

- `apps/web/src/lib/stores/mapView.ts` führt eine clientseitige Teilstringsuche über höchstens zehn bereits sichtbare Kartenmarker aus.
- Bestehende Filter begrenzen den Markerbestand vor dieser Suche.
- Eine serverseitige Search-API, PostgreSQL-Volltextsuche, Trigramm-Suche, Embedding-Projektion, ein Embedding-Worker und eine belegte `pgvector`-Fähigkeit existieren nicht.
- `architecture/overview.md` beschreibt JSONL weiterhin als Code-Default und PostgreSQL als expliziten Opt-in-Pfad.

Diese Ausgangslage darf nicht als bereits vorhandene hybride Suche dargestellt werden.

## 4. Aktivierungsgrenze zwischen JSONL und PostgreSQL

Die serverseitige hybride Suche darf nur aktiviert werden, wenn Knoten im PostgreSQL-Domänenmodus kanonisch gelesen und geschrieben werden. Ein PostgreSQL-Suchindex über einem gleichzeitig kanonischen JSONL-Bestand wäre eine konkurrierende Wahrheit und ist verboten.

Bis zum belegten PostgreSQL-Domänencutover bleibt die bestehende clientseitige Teilstringsuche die reale Produktfunktion. Nach T006/T007 ist die serverseitige hybride Suche der Primärpfad. Die clientseitige lexikalische Suche darf dann nur als technisch klar gekennzeichneter, begrenzter Notfall-Fallback über bereits an den Client ausgelieferte sichtbare Projektionen bestehen.

Der Fallback darf keine verborgenen Daten nachladen, keine eigene dauerhafte Suchwahrheit speichern und keine semantischen oder kuratierten Beziehungen behaupten.

## 5. Wahrheits- und Projektionsvertrag

Kanonisch bleiben Domänenzeilen und ihre fachlichen Revisionen in PostgreSQL. Die Suchprojektion darf keinen Knoten, Text oder Sichtbarkeitszustand führen, der nicht aus dieser Wahrheit rekonstruierbar ist.

Die spätere Projektion enthält mindestens:

- `node_id`
- `source_revision`
- `content_sha256`
- `title`
- `tags`
- `searchable_text`
- lexikalische Suchrepräsentation
- optionales Embedding
- Provider
- Modell-ID
- Modellrevision
- Dimension
- Indexgeneration
- Sichtbarkeitsklasse
- Indexierungsstatus
- `indexed_at`

Der physische Tabellen- und Indextyp wird in T003 festgelegt. T001 behauptet weder eine vorhandene Migration noch eine installierte PostgreSQL-Erweiterung.

Suche ist kein fachlicher Schreibpfad. Ähnlichkeit darf niemals automatisch Fäden oder kuratierte Beziehungen erzeugen, Knoten zusammenführen, Knoten löschen, Moderationsentscheidungen treffen oder Sichtbarkeit erweitern.

## 6. Dokumentbildung und Datenminimierung

Nur tatsächlich suchbare Knotenfelder dürfen in `searchable_text` oder ein Embedding eingehen:

- Titel
- Kurzbeschreibung
- ausführlicher Informationstext
- Tags
- Knotenart
- Sprache
- ausdrücklich öffentlich freigegebener Ortsname
- fachlich zulässige Handlungsbegriffe

Reihenfolge, Feldmarkierungen und Normalisierung müssen deterministisch sein, damit derselbe Knotenstand denselben `content_sha256` erzeugt.

Ausgeschlossen sind:

- E-Mail-Adressen
- Sitzungen und Authentifizierungsdaten
- private Account- oder Garnrollenfelder
- interne Moderationsdaten
- private Gespräche
- verborgene oder interne Orte
- nicht sichtbare historische Versionen
- technische Auditdaten
- Secrets, Tokens und Provider-Schlüssel
- personenbezogene Felder ohne ausdrücklichen Suchzweck

Garnrollen werden in v1 nicht eingebettet. Die bestehende sichtbare Garnrollensuche bleibt lexikalisch, bis ein eigener Datenschutz- und Zweckbindungsvertrag beschlossen ist.

## 7. Sichtbarkeit vor Retrieval

Sichtbarkeit und Löschstatus werden in derselben serverseitigen Kandidatenauswahl wie die lexikalischen und semantischen Signale angewendet. Ein System, das zunächst verborgene Kandidaten rankt und sie anschließend entfernt, ist nicht konform.

Es gelten mindestens diese Invarianten:

1. Ein nicht sichtbarer Knoten ist weder Volltext- noch Vektorkandidat.
2. Ein gelöschter Knoten ist ab wirksamer Löschung in keiner aktiven Generation suchbar.
3. Sichtbarkeitsentzug wirkt mindestens so streng wie eine Inhaltsänderung und invalidiert betroffene Projektionen.
4. Ein veralteter Worker darf weder eine frühere Revision noch eine frühere Sichtbarkeit zurückschreiben.
5. Cache, Fallback und „Ähnliche Knoten“ verwenden dieselbe zulässige Kandidatenmenge.
6. Föderierte Projektionen berücksichtigen Ursprung, Reichweite und lokale Annahmeregeln; `global` bedeutet nicht automatisch indexiert.

Die konkreten SQL-Prädikate und Reichweitenabbildungen gehören zu T003, T005 und T006. Bis sie vollständig belegbar sind, bleibt die neue Search-API fail-closed.

## 8. Retrieval- und Rankingvertrag

Reine Vektorsuche ist verboten. Die Rangfolge besitzt harte Prioritätsklassen:

1. exakter normalisierter Titel
2. exaktes normalisiertes Tag
3. Titelpräfix
4. Schreibfehlertoleranz über PostgreSQL-Trigramme
5. PostgreSQL-Volltext
6. semantische Ähnlichkeit
7. stabile Tie-Breaks

Ein semantisch ähnlicher Treffer darf einen exakten Titel- oder Tagtreffer nicht verdrängen. Bestehende Filter begrenzen Kandidaten vor dem Ranking. Stabile Tie-Breaks verwenden mindestens eine deterministische Objektidentität und dürfen nicht von Prozessreihenfolge oder zufälliger Vektorausgabe abhängen.

„Ähnliche Knoten“ ist eine eigene, ausdrücklich maschinell berechnete Oberfläche. Sie ist weder Faden noch kuratierte Beziehung und verwendet denselben Sichtbarkeits- und Generationsvertrag wie die Suche.

## 9. Modell- und Providervertrag

T002 vergleicht:

- die heutige lexikalische Suche
- PostgreSQL-Volltext und Trigramme
- ein kompaktes lokales mehrsprachiges Embedding-Modell
- lokales `qwen3-embedding:8b` als Qualitätsmaßstab
- optional einen kostenlosen OpenRouter-Embedding-Endpunkt als begrenzten Gegenkandidaten

OpenRouter-Free ist nur zulässig:

- mit synthetischen oder nachweislich nicht sensiblen Texten
- mit festem kleinem Auftragsbudget
- ohne Annahme eines vorhandenen API-Schlüssels
- ohne Produktionsabhängigkeit
- ohne stillen Wechsel auf einen kostenpflichtigen Endpunkt
- ohne Speicherung von Secrets oder Rohantworten im Repository

Cloud gewinnt nur bei einem klaren, praktisch relevanten Qualitätsvorsprung und nach ausdrücklicher Kostenfreigabe. Andernfalls gewinnt das kleinste lokale Modell, das die Qualitätsgates erfüllt.

## 10. T002-Messung und begrenzte Modellentscheidung

Die T002-Messung verwendet ausschließlich den eingecheckten synthetischen Korpus mit 22 Knoten und 24 deutschen Suchfällen. 19 Fälle sind natürliche Anfragen. Zwei Knoten sind verborgen, ein Knoten ist gelöscht; alle Retrievalpfade erhalten ausschließlich die pro Fall zulässige sichtbare Kandidatenmenge. Es wurden keine realen oder pseudonymisierten Daten, keine Cloud-API und kein OpenRouter-Endpunkt verwendet. Die externen Kosten betragen null.

Der reproduzierbare Beleg liegt in `contracts/search/examples/relevance-benchmark.heim-pc.json`. Er bindet Dataset, Schema, Benchmarkquellcode, Goldset-Validator, Ollama-Modell-Digest, Dimension und Dokumentrevision. Für jeden Fall speichert er die vollständige begrenzte Rangfolge; der Offline-Checker berechnet daraus sämtliche Qualitätsaggregate und die Modellentscheidung neu. Die Modellidentität wird vor und nach beiden Embedding-Aufrufen gelesen und muss unverändert bleiben. Rohvektoren und Rohproviderantworten werden weder eingecheckt noch als Metrik gespeichert.

CI prüft Struktur, Hashbindungen, vollständige Fallabdeckung, Rangfolgen, Aggregate, Nichtbehauptungen und Entscheidung deterministisch. CI erzeugt die Ollama-Embeddings nicht unabhängig neu; eine Änderung von Goldset, Benchmarkquelle, Modell-Digest oder Dokumentbildung erzwingt deshalb eine neue receipt-gebundene lokale Messung. Laufzeitwerte bleiben beobachtende Heim-PC-Metadaten und sind kein Auswahlkriterium oder deterministischer CI-Beleg.

Die Messung trennt drei Ebenen:

1. Die reale heutige Client-Teilstringsuche erreicht bei natürlichen Anfragen 0 von 19 relevanten Top-3-Treffern.
2. Die deterministische FTS-/Trigramm-Referenz erreicht 11 von 19 natürlichen Top-3-Treffern. Sie ist eine Offline-Näherung und belegt ausdrücklich keine Parität zu PostgreSQL-FTS oder `pg_trgm`.
3. Lokale hybride Kandidaten behalten die harten lexikalischen Vorrangklassen bei und ergänzen nur Fälle ohne stärkeres lexikalisches Signal.

| Lokaler Kandidat | Dimension | Natürliche Top-3 | Falsche Top-1 | Sichtbarkeitslecks | Gate |
| --- | ---: | ---: | ---: | ---: | --- |
| `qwen3-embedding:0.6b` | 1024 | 18/19 | 3 | 0 | nicht qualifiziert |
| `qwen3-embedding:4b` | 2560 | 19/19 | 2 | 0 | nicht qualifiziert |
| `qwen3-embedding:8b` | 4096 | 19/19 | 1 | 0 | qualifiziert |

Die lexikalische Referenz besitzt einen falschen Top-1-Treffer. Deshalb scheiden 0,6B und 4B trotz hoher Top-3-Abdeckung aus: Sie verschlechtern dieses harte Gate auf drei beziehungsweise zwei falsche Top-1-Treffer. `qwen3-embedding:8b` hält den Referenzwert, wahrt exakte Titel- und Tagtreffer und erreicht 19 von 19 natürlichen Top-3-Treffern. Es ist damit der kleinste der vollständig gemessenen lokalen Kandidaten, der alle T002-Gates erfüllt.

Diese Auswahl bindet für T003 und T004 nur den derzeitigen lokalen Referenzkandidaten samt Digest und Dimension. Sie ist keine Produktionsfreigabe, kein Nachweis realer Nutzerrelevanz, keine Aussage über PostgreSQL- oder `pgvector`-Betrieb und keine Freigabe zur SemantAH-Stilllegung. Änderungen an Goldset, Dokumentbildung, Modell-Digest, Quantisierung oder Rankingrevision verlangen eine neue vollständige Messung.

## 11. T003-Livebeleg: PostgreSQL-Grundlage

Der ausführbare T003-SQL-Vertrag liegt bewusst unter `contracts/search` und nicht im automatisch ausgerollten SQLx-Migrationspfad. Ein Merge von T003 führt daher keine Suchmigration gegen Produktion aus. T003 wurde ausschließlich gegen einen wegwerfbaren lokalen Container aus dem bereits im Repository gepinnten Image `postgres:16@sha256:be01cf82fc7dbba824acf0a82e150b4b360f3ff93c6631d7844af431e841a95c` gemessen. Es wurden nur synthetische T002-Daten verwendet; externe Kosten, Cloud-APIs und Produktionseffekte betragen null. Der Lauf nennt den Repository-Basiscommit ausdrücklich, kennzeichnet den Messworktree als dirty und bindet die acht tatsächlich ausgeführten Quelldateien einzeln sowie gemeinsam über SHA-256. Er behauptet daher keinen bereits commitgebundenen Lauf.

Der receipt-gebundene Lauf belegt:

- PostgreSQL 16.14 (`server_version_num` 160014);
- `pgcrypto` 1.3 ist verfügbar, wird von T003 aber nicht installiert; `pg_trgm` 1.6 ist verfügbar und im Proof installiert;
- pgvector ist im kanonischen PostgreSQL-Image nicht verfügbar und nicht installiert;
- eine aktive Generation bindet Provider, Modell-ID, Modellrevision, Dimension, Dokumentrevision und Normalisierungsrevision;
- Projektionen binden Quellversion, Quellrevision und `content_sha256`;
- Sichtbarkeit, Löschstatus, aktive Generation, explizit autorisierte Knoten-IDs und Filter begrenzen die Kandidatenmenge vor jedem Ranking;
- parallele Schreibversuche werden pro Generation und Knoten serialisiert; veraltete Versionen werden verworfen, identische Wiederholungen bleiben idempotent und gleichversionige Identitätskonflikte werden abgelehnt;
- ein vollständiger Neuaufbau erzeugt denselben Projektions-Digest;
- `pg_dump`/`pg_restore` erhält Projektions-Digest und aktive Generation; die Down-Migration entfernt nur die Suchprojektion und lässt `domain_nodes` bestehen;
- weder HNSW noch IVFFlat oder ein anderer ANN-Index wird angelegt.

Die reale PostgreSQL-Rangfolge aus FTS und `pg_trgm` erreicht im synthetischen T002-Korpus 14 von 19 natürlichen Top-3-Treffern, 0 falsche Top-1-Treffer und 0 Sichtbarkeitslecks. Die frühere rein synthetische FTS-/Trigramm-Referenz erreichte 11 von 19, einen falschen Top-1-Treffer und ebenfalls 0 Lecks. 14 von 24 vollständigen begrenzten Rangfolgen sind identisch. Die Abweichung entsteht vor allem durch PostgreSQLs deutsche Lexemanalyse, parallele einfache Tokenvektoren, Fachbegriffsabdeckung und `word_similarity`; sie ist als reale PostgreSQL-Messung dokumentiert und kein Beleg realer Nutzerqualität.

Die gespeicherten Laufzeiten messen den vollständigen `docker exec`-/`psql`-Roundtrip je Anfrage. Sie sind ausdrücklich keine reine Datenbanklatenz und kein Produktions-SLO.

Da pgvector im kanonischen Image fehlt, bleibt jede persistente Embedding-Projektion fail-closed. T004 darf unabhängig davon einen rein flüchtigen, testgebundenen Embedding- und Rankingkern gegen synthetische Daten ausführen, solange weder Vektoren noch Providerantworten persistiert werden. Vor T005 muss pgvector explizit paketiert, versions- und image-digest-gebunden belegt werden oder ein anderer exakter Speicherpfad einen eigenen Vertrag erhalten. `DOUBLE PRECISION[]` bleibt in T003 nur eine dimensionsgeprüfte Referenzablage ohne Runtime-Verbraucher. Es gibt keine Search-API, keinen Worker, keinen Backfill, keine Webintegration, keinen Produktionsrollout und keine SemantAH-Stilllegung.

## 12. T004-Livebeleg: interner Embedding- und Hybrid-Rankingkern

T004 implementiert ausschließlich einen ausführbaren Referenz- und Beweiskern in `scripts/search/hybrid_ranking_core.py`. Er ist weder Produktcode noch neue Runtime oder fachlicher Schreibpfad. Die reale PostgreSQL-Rangfolge aus T003 bleibt die führende lexikalische Wahrheit: T004 übernimmt eine bereits sichtbarkeitsgefilterte und autorisierte Reihenfolge, prüft ihre Knoten-IDs gegen die exakte Kandidatenmenge und ergänzt höchstens einen bislang nicht lexikalisch gerankten, ausreichend klar abgesetzten semantischen Kandidaten. Damit entsteht keine zweite Python-Volltextwahrheit und keine semantische Ergebnisinflation.

Die Normalisierung ist als `weltgewebe-search-normalization-v1` gebunden; die gehärtete Top-1-Fusion als `weltgewebe-hybrid-ranking-v2`. Eine Generation bindet lokalen Provider, Modell-ID, Modellrevision, Laufzeitidentität, Dimension, Dokumentrevision, Normalisierungsrevision und Rankingrevision in einer deterministischen `generation_id`. Mischung verschiedener Generationen, falsche Dimensionen, leere oder übergroße Vektoren, Bool-Werte, nichtendliche Zahlen und Nullvektoren werden abgelehnt.

Nur der Namensraum `local:` ist zulässig. Die Live-Messung verwendet den proxyfreien Loopback-Endpunkt, Ollama 0.12.6 und exakt die bereits in T002 gebundenen Modelldigests. Die zwei Embedding-Aufrufe sind absichtlich getrennt: Der Dokumentaufruf bildet die spätere Indexierungsphase ab, der Anfrageaufruf die spätere Online-Suchphase; ein gemeinsamer Benchmark-Batch würde diese Betriebsgrenze und ihre getrennten Laufzeiten verdecken. Die Identität wird vor und nach beiden Aufrufen gelesen und muss unverändert bleiben. Nur `ProviderUnavailableError` aktiviert den lexikalischen Fallback. Identitäts-, Datenschutz-, Dimensions- und Generationsfehler bleiben fail-closed.

Das synthetische T002-Goldset wurde gegen die vollständigen T003-PostgreSQL-Rangfolgen gemessen. Die Fusion berechnet diese lexikalischen Vorrangklassen nicht erneut, sondern erhält die autoritative T003-Eingabereihenfolge unverändert. Erst danach darf genau ein semantischer Top-Kandidat folgen. Semantische Gleichstände werden ausdrücklich nach aufsteigender Knoten-ID entschieden.

| Pfad | Natürliche Top-3 | Falsche Top-1 | Sichtbarkeitslecks | Entscheidung |
| --- | ---: | ---: | ---: | --- |
| T003 PostgreSQL-FTS/`pg_trgm` | 14/19 | 0 | 0 | lexikalische Basis |
| `qwen3-embedding:0.6b` + T003 | 14/19 | 0 | 0 | kein Zusatznutzen |
| `qwen3-embedding:4b` + T003 | 18/19 | 0 | 0 | kleinster qualifizierter Kandidat |
| `qwen3-embedding:8b` + T003 | 19/19 | 0 | 0 | Qualitätsobergrenze, nicht erforderlich |

`natural_top3_relevant_count` bezieht sich ausschließlich auf die 19 als natürliche Sprache markierten Fälle. `top3_relevant_count` und `per_expected_rank_class` beziehen sich dagegen auf alle 24 Fälle einschließlich Filter-, Sichtbarkeits- und Löschfällen. Daher sind beim 4B-Modell 18 natürliche Top-3-Treffer, 23 Top-3-Treffer insgesamt und 16 Top-3-Treffer der erwarteten Klasse `semantic` gleichzeitig konsistent. Der Offline-Checker rekonstruiert sämtliche Aggregate aus den vollständigen Fallrangfolgen und lehnt jede Abweichung ab.

`qwen3-embedding:4b` ist der kleinste gemessene lokale Kandidat, der mindestens 85 Prozent natürliche Top-3, null falsche Top-1, null Sichtbarkeitslecks, unveränderte exakte Vorrangklassen und mindestens zwei zusätzliche natürliche Top-3-Fälle gegenüber T003 erfüllt. Diese T004-Entscheidung ersetzt die vorsichtigere T002-Referenzwahl von 8B nur für den realen T003-plus-T004-Fusionsvertrag. Sie ist weiterhin keine Produktionsfreigabe.

Der Beleg liegt als Receipt-Schema v2 in `contracts/search/examples/hybrid-ranking-core.heim-pc.json`. Die Baseline heißt ausschließlich `t003_postgresql` und enthält deren vollständige Fallqualität; ein missverständlicher zweiter „T004-Lexik“-Name existiert nicht mehr. Der Beleg bindet Architektur, Dataset, T002-Modellidentitäten, T003-Receipt, Kern, Probe, Regressionstests und Receipt-Schema über Einzelhashes und einen gemeinsamen Quellmanifest-Hash. Er speichert vollständige begrenzte Rangfolgen und Aggregate, aber keine Rohvektoren oder Providerrohdaten. Externe Kosten betragen null.

T004 erzeugt keine persistente Projektion, keine `pgvector`-Migration, keinen Worker, keinen Backfill, keine Search-API, keine Webintegration, kein Deployment, kein hybrides Ranking in Produktion und keine SemantAH-Stilllegung. Der Python-Kern ist eine ausführbare Referenz und ein Beweisvertrag. Eine spätere produktive Rust-Integration bleibt T005/T006 vorbehalten und muss denselben Generationen-, Datenschutz-, Fallback- und Rankingvertrag neu belegen.

## 13. Generationen und Dimensionssicherheit

Eine aktive Indexgeneration bindet mindestens:

- Provider
- Modell-ID
- Modellrevision
- Dimension
- Dokumentbildungsrevision
- Normalisierungsrevision
- Erzeugungszeit und Aktivierungszustand

Ein Wechsel eines dieser Merkmale erzeugt eine vollständige neue Generation. Vektoren verschiedener Generationen werden niemals in derselben Kandidatenauswahl gemischt.

Eine neue Generation wird vollständig aufgebaut und geprüft, bevor sie atomar aktiviert wird. Ein Rückwechsel aktiviert nur eine vollständig konsistente vorherige Generation oder baut sie neu auf.

## 14. Projektion und Worker

Der spätere Worker ist idempotent und revisionsgebunden:

1. Er liest Knoten-ID und Quellrevision.
2. Er bildet das minimierte Suchdokument deterministisch.
3. Er berechnet Hash, lexikalische Repräsentation und optional das Embedding.
4. Vor dem Schreiben liest er die aktuelle Quellrevision erneut.
5. Nur die noch aktuelle Revision darf die Projektion ersetzen.
6. Provider-Ausfall markiert semantische Arbeit als ausstehend, blockiert aber keine Knotenmutation.
7. Löschung und Sichtbarkeitsentzug invalidieren alle betroffenen Generationen.
8. Backfill und laufende Aktualisierung verwenden denselben Schreibvertrag.

Mehrere API- oder Worker-Instanzen dürfen denselben Auftrag wiederholen, ohne doppelte fachliche Wirkung oder Rückwärtslauf.

## 15. Vorgesehene interne Codegrenze

T004 bleibt als ausführbare, nichtproduktive Referenz unter `scripts/search`, damit weder API noch Persistenz vorweggenommen werden. Die reale Produktorganisation wird ab T005/T006 innerhalb von `apps/api` ergänzt, nicht in einer neuen Runtime:

```text
apps/api/src/search/
  contract.rs
  document.rs
  lexical.rs
  semantic.rs
  ranking.rs
  visibility.rs
  repository.rs
  worker.rs
  metrics.rs
  provider/
apps/api/src/routes/search.rs
apps/api/src/bin/search-backfill.rs
apps/api/src/bin/search-evaluate.rs
```

Die genaue Dateigrenze darf an bestehende Rust-Module angepasst werden. Verboten bleibt eine separate `apps/semantic-service`-Runtime.

## 16. Goldset-Vertrag

`contracts/search/relevance-goldset.schema.json` definiert ein maschinenlesbares Format. Das eingecheckte Beispiel ist ausschließlich synthetisch.

Jeder Fall enthält mindestens:

- stabile Fall-ID
- Sprache
- Anfrage
- sichtbaren Suchkontext
- relevante Knoten-IDs
- erwartete Rangklasse
- ausgeschlossene Knoten-IDs
- überprüfbare Begründung
- `contains_personal_data: false`

Relevante IDs müssen sichtbar sein. Ausgeschlossene IDs dürfen nicht sichtbar oder relevant sein. Der Validator verweigert E-Mail-ähnliche Inhalte, damit Testdaten nicht versehentlich zu einem Transportweg für personenbezogene Daten werden.

Das Goldset misst Retrievalqualität, nicht gesellschaftliche Wahrheit. Bewertungen müssen begründet und bei fachlichen Änderungen versioniert werden.

## 17. Qualitäts- und Freigabegates

Vor T008 müssen mindestens belegt sein:

- Exakte Titel- und Tagtreffer bleiben auf Rang 1.
- Mindestens 85 Prozent relevanter natürlicher Goldset-Anfragen liefern einen relevanten Top-3-Treffer.
- Falsche Top-1-Treffer nehmen gegenüber der lexikalischen Basis nicht zu.
- Gelöschte oder unsichtbare Inhalte erscheinen nie.
- Embedding-Ausfall blockiert keine Knotenänderung.
- Veraltete Worker überschreiben keine neueren Revisionen.
- Ein vollständiger Indexneuaufbau ist reproduzierbar.
- Modellwechsel sind generationsgebunden.
- Multi-Instance-Betrieb bleibt konsistent.
- Backup, Restore und PITR umfassen die Projektion oder belegen deren vollständige Regeneration.
- Externe API-Kosten sind hart begrenzt.
- ANN oder HNSW wird ohne gemessenen Bedarf nicht eingeführt.

Basis und Kandidaten werden getrennt berichtet. Ein gemittelter Gesamtscore darf Regressionen bei exakten Treffern oder Sichtbarkeit nicht verdecken.

## 18. Betrieb und Beobachtbarkeit

Später mindestens beobachtbar sind:

- Rückstand nach Indexierungsstatus
- Alter der ältesten ausstehenden Revision
- aktive Generation und Modellidentität
- verworfene veraltete Worker-Ergebnisse
- Providerfehler und begrenzte Retryzahl
- Suchlatenz nach Retrievalanteil
- Fallback-Nutzung
- Goldset-Ergebnis pro Rankingklasse
- Rebuild-Dauer und Ergebnis

Metriken enthalten keine Rohqueries, privaten Texte oder Embeddings, solange dafür kein eigener Datenschutzvertrag besteht.

## 19. Hard Cut und Nichtziele

Nicht Bestandteil der Zielarchitektur sind:

- separate SemantAH-Runtime
- JSONL-Persistenz der Suchprojektion
- eigenständiger `indexd`-Dienst
- dauerhafte Git- oder Cargo-Abhängigkeit auf SemantAH
- allgemeine Namespace-Plattformabstraktionen
- Obsidian-Pipeline, Wissensgraph oder Related-Blöcke
- Knowledge Observatory oder Daily Insights
- HausKI- oder RepoBrief-Rollen
- Commonworld-Anbindung
- Shadow-Modus
- automatische Fäden, Beziehungen, Löschungen oder Zusammenführungen aus Ähnlichkeit
- ANN-/HNSW-Arbeit ohne gemessenen Weltgewebe-Engpass

Brauchbare SemantAH-Konzepte wie Providergrenzen, Dimensionsprüfung, Normalisierung, deterministische Cosinus-Referenzsuche, Benchmarks und Tests dürfen zielgerichtet neu implementiert werden. SemantAH-Code wird nicht als dauerhafte Abhängigkeit übernommen.

## 20. Taskgrenzen

- **T001:** Architekturvertrag, Goldset-Format, synthetisches Beispiel und Validator.
- **T002:** Relevanzbasis und Modellvergleich.
- **T003:** PostgreSQL-Suchschema, Volltext und Trigramme belegt; fehlendes `pgvector` als harte Stopbedingung dokumentiert.
- **T004:** interner Embedding- und Rankingkern belegt; `qwen3-embedding:4b` ist der kleinste lokale Testkandidat für den T003-plus-T004-Fusionsvertrag.
- **T005:** idempotente Projektion, Worker, Backfill und Löschfortpflanzung.
- **T006:** hybride serverseitige Such-API.
- **T007:** Webintegration und getrennte „Ähnliche Knoten“.
- **T008:** vollständige Abnahme, direkter Rollout und öffentlicher Live-Beweis.
- **T009:** SemantAH stilllegen, archivieren und Bureau/Systemkatalog bereinigen.

Erst nach erfolgreichem T008-Beweis darf T009 `SEMANTAH-USEFULNESS-V1`, `SEMANTAH-INDEXD-SCALING-V1` und `SEMANTAH-E2E-PORTABILITY-V1` superseden oder schließen.

## 21. Stopbedingungen

Die Initiative wird gestoppt oder neu geschnitten, wenn:

- Sichtbarkeit nicht vor Retrieval durchsetzbar ist,
- PostgreSQL keine tragfähige Projektions- und Wiederherstellungsgrenze bietet,
- FTS und Trigramme den relevanten Nutzen bereits ohne Embeddings liefern,
- lokale Modelle die Qualitätsgrenzen klar verfehlen und keine ausdrückliche Kostenfreigabe besteht,
- der Betriebsaufwand den gemessenen Relevanzgewinn überwiegt oder
- die Suche eine zweite fachliche Wahrheit oder automatische Beziehungssemantik erzeugen würde.

## 22. Nicht behaupteter Zustand

Dieser Vertrag belegt nicht:

- installierte oder produktionsfähige `pgvector`-Unterstützung
- eine produktiv freigegebene Embedding-Modell-ID
- eine vorhandene Search-API
- laufende Worker oder Backfills
- verbesserte reale Relevanz
- gesunden Produktionsbetrieb
- einen öffentlichen Live-Beweis
- die Freigabe zur Archivierung von SemantAH
