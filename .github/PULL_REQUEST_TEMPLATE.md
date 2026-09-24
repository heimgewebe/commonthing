<!--
commonThing is the primary project. Replace R? with exactly one class:
R0 = Markdown-only, at most 50 changed lines; CI only
R1 = bounded low-risk change or approved raster asset; one native current-head GitHub approval is enough
R2 = application code, API, tests, scripts or dependencies; two hash-bound reports
R3 = auth, privacy, security, concurrency, migration, workflow, deploy or operations
The gate rejects a class below the path-derived minimum.
-->
<!-- weltgewebe-risk: R? -->

## Ziel

<!-- Was soll sich für Nutzer oder Betrieb konkret ändern? -->

## Nicht-Ziele

<!-- Was ist ausdrücklich nicht Teil dieses PR? -->

## Zu bewahrende Invarianten

<!-- Welche Regeln dürfen trotz der Änderung nicht verletzt werden? -->

## Fehlerszenarien und Rückfallweg

<!-- Wie kann die Änderung scheitern, wie wird der Fehler erkannt und wie wird zurückgerollt? -->

## Prüfungen

<!-- Ausgeführte Tests und noch fehlende praktische Nachweise. Keine pauschalen Aussagen. -->

## Reviewevidenz

Der Workflow **Review Evidence Gate** erzeugt ein herunterladbares Paket mit:

- von GitHub geliefertem Textdiff und Patch;
- Basis-, Head-, Merge-Basis- und Diff-SHA-256;
- Reviewauftrag und maschinenlesbarem Manifest;
- aktuellem Ergebnis der hashgebundenen Reviewattestierungen.

Für R0 ist keine Fremdreview erforderlich. Für R1 genügt eine normale GitHub-Review
mit **Approve**, sofern GitHub den Prüfer als `OWNER`, `MEMBER` oder `COLLABORATOR`
ausweist und die Freigabe den aktuellen Head betrifft. Bei Fork- oder
Dependabot-PRs postet ein Maintainer danach `/review-evidence recheck`. R2 und R3
benötigen weiterhin die vollständigen hashgebundenen Berichte aus der erzeugten
`*.review-request.md`.

Nicht textuell dargestellte Dateien werden als `opaque_files` ausgewiesen. Gängige
Rastergrafiken in den festgelegten Doku- und Web-Assetpfaden sind mit mindestens
R1 visuell prüfbar. PDF, SVG, Archive, ausführbare Dateien und andere undurchsichtige
Artefakte blockieren weiterhin. Jeder neue Push oder Basiswechsel entwertet
frühere hashgebundene Berichte.

## Veröffentlichung und Liveprüfung

<!-- Welche Produktionsoberfläche wird geprüft? Woran ist die exakte Version erkennbar? -->
