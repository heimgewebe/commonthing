import { expect, test, type Locator, type Page } from "@playwright/test";
import { mockApiResponses } from "./fixtures/mockApi";
import { waitForNodeMarker } from "./fixtures/mapReady";

async function openFirstNode(page: Page) {
  const nodeMarker = await waitForNodeMarker(page);
  await nodeMarker.evaluate((marker) => {
    marker.dispatchEvent(new MouseEvent("click", { bubbles: true }));
  });
  const panel = page.locator('[data-testid="context-panel"]');
  await expect(panel).toBeVisible();
  await expect(panel.getByRole("tab", { name: "Übersicht" })).toBeVisible();
  return panel;
}

async function confirmNodeRemoval(panel: Locator) {
  await panel.getByRole("button", { name: "Aus dem Gewebe entfernen" }).click();
  await expect(panel.getByText("Knoten wirklich entfernen?")).toBeVisible();
  await panel.getByRole("button", { name: "Jetzt entfernen" }).click();
}

test.describe("Knoten bearbeiten und löschen", () => {
  test("hält den Bearbeiten-Reiter auf Tabletbreite in einer Zeile", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 1024, height: 768 });
    await mockApiResponses(page, {
      auth: {
        authenticated: true,
        account_id: "e2e-weber",
        role: "weber",
      },
    });
    await page.goto("/map");

    const panel = await openFirstNode(page);
    const tabList = panel.getByRole("tablist", { name: "Knoten-Tabs" });
    const editTab = tabList.getByRole("tab", { name: "Bearbeiten" });
    const layout = await editTab.evaluate((element) => {
      const style = getComputedStyle(element);
      const range = document.createRange();
      range.selectNodeContents(element);
      return {
        whiteSpace: style.whiteSpace,
        overflowWrap: style.overflowWrap,
        textRects: range.getClientRects().length,
        clientWidth: element.clientWidth,
        scrollWidth: element.scrollWidth,
      };
    });

    expect(layout.whiteSpace).toBe("nowrap");
    expect(layout.overflowWrap).toBe("normal");
    expect(layout.textRects).toBe(1);
    expect(layout.scrollWidth).toBeLessThanOrEqual(layout.clientWidth + 1);
    await expect(tabList).toHaveCSS("overflow-x", "auto");
  });

  test("Löschbestätigung bleibt auch nach vielen Wiederholungen reaktionsfähig", async ({
    page,
  }) => {
    await mockApiResponses(page, {
      auth: {
        authenticated: true,
        account_id: "e2e-weber",
        role: "weber",
      },
    });
    let nativeDialogCount = 0;
    page.on("dialog", async (dialog) => {
      nativeDialogCount += 1;
      await dialog.dismiss();
    });
    await page.goto("/map");

    const panel = await openFirstNode(page);
    await panel.getByRole("tab", { name: "Bearbeiten" }).click();
    for (let attempt = 0; attempt < 8; attempt += 1) {
      await panel
        .getByRole("button", { name: "Aus dem Gewebe entfernen" })
        .click();
      await expect(panel.getByText("Knoten wirklich entfernen?")).toBeVisible();
      await expect(
        panel.getByRole("button", { name: "Abbrechen" }),
      ).toBeFocused();
      await panel.getByRole("button", { name: "Abbrechen" }).click();
      await expect(
        panel.getByRole("button", { name: "Aus dem Gewebe entfernen" }),
      ).toBeFocused();
    }
    expect(nativeDialogCount).toBe(0);

    await panel
      .getByRole("button", { name: "Aus dem Gewebe entfernen" })
      .click();
    await expect(
      panel.getByRole("button", { name: "Abbrechen" }),
    ).toBeFocused();
    await page.keyboard.press("Escape");
    await expect(
      panel.getByRole("button", { name: "Aus dem Gewebe entfernen" }),
    ).toBeFocused();
    await expect(panel.getByText("Knoten wirklich entfernen?")).toHaveCount(0);
    await expect(panel).toBeVisible();
  });

  test("Weber kann einen gemeinsamen Knoten bearbeiten und samt Fadenprojektionen löschen", async ({
    page,
  }) => {
    await mockApiResponses(page, {
      auth: {
        authenticated: true,
        account_id: "e2e-weber",
        role: "weber",
      },
    });
    await page.goto("/map");

    const panel = await openFirstNode(page);
    const originalTitle = await panel.locator("h3").innerText();
    await expect(
      panel.getByRole("button", { name: "Bearbeiten", exact: true }),
    ).toHaveCount(0);
    const editTab = panel.getByRole("tab", { name: "Bearbeiten" });
    await editTab.click();
    await panel
      .getByRole("button", { name: "Bearbeiten", exact: true })
      .click();
    await expect(panel.getByLabel("Titel")).toBeFocused();
    await panel.getByLabel("Titel").fill("Verworfener Entwurf");
    await panel.getByRole("button", { name: "Abbrechen" }).click();
    await expect(panel.locator("form")).toHaveCount(0);
    await expect(panel.locator("h3")).toHaveText(originalTitle);
    await expect(editTab).toBeFocused();
    await expect(editTab).toHaveAttribute("aria-selected", "true");
    await expect(panel.getByRole("alert")).toHaveCount(0);
    await panel
      .getByRole("button", { name: "Bearbeiten", exact: true })
      .click();
    await expect(panel.getByLabel("Titel")).toBeFocused();
    await panel.getByLabel("Titel").fill("Gemeinsam gepflegter Knoten");
    await panel
      .getByLabel("Kurzbeschreibung")
      .fill("Aktualisierte öffentliche Kurzbeschreibung");

    const putRequestPromise = page.waitForRequest(
      (request) =>
        request.method() === "PUT" &&
        /\/api\/nodes\/[^/]+$/.test(new URL(request.url()).pathname),
    );
    await panel.getByRole("button", { name: "Änderungen speichern" }).click();
    const putRequest = await putRequestPromise;
    expect(putRequest.headers()["if-match"]).toMatch(/^".+"$/);
    expect(putRequest.postDataJSON()).toMatchObject({
      title: "Gemeinsam gepflegter Knoten",
      summary: "Aktualisierte öffentliche Kurzbeschreibung",
    });
    await expect(panel).toBeVisible();
    await expect(panel.locator("h3")).toHaveText("Gemeinsam gepflegter Knoten");
    await expect(
      panel.getByText("Aktualisierte öffentliche Kurzbeschreibung"),
    ).toBeVisible();
    const markerCountBeforeDelete = await page.locator(".map-marker").count();

    await expect(panel.getByRole("tab", { name: "Übersicht" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    await expect(panel.getByRole("tab", { name: "Übersicht" })).toBeFocused();
    await panel.getByRole("tab", { name: "Bearbeiten" }).click();
    const deleteRequestPromise = page.waitForRequest(
      (request) =>
        request.method() === "DELETE" &&
        /\/api\/nodes\/[^/]+$/.test(new URL(request.url()).pathname),
    );
    await confirmNodeRemoval(panel);
    const deleteRequest = await deleteRequestPromise;
    expect(deleteRequest.headers()["if-match"]).toMatch(/^".+"$/);

    await expect(panel).toHaveCount(0);
    await expect(page.locator(".map-marker")).toHaveCount(
      markerCountBeforeDelete - 1,
    );
  });

  test("archivierte Gesprächsbeiträge bleiben nach der Entfernung erreichbar", async ({
    page,
  }) => {
    await mockApiResponses(page, {
      auth: {
        authenticated: true,
        account_id: "e2e-weber",
        role: "weber",
      },
      nodeDeleteConversation: {
        effect: "archived",
        archive_id: "70000000-0000-4000-8000-000000000001",
        archive_url: "/api/conversations/70000000-0000-4000-8000-000000000001",
      },
    });
    await page.route("**/api/conversations/**", async (route) => {
      const path = new URL(route.request().url()).pathname;
      if (path === "/api/conversations/70000000-0000-4000-8000-000000000001") {
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            id: "70000000-0000-4000-8000-000000000001",
            conversation_type: "node",
            lifecycle_state: "archived",
            node_id: null,
            node_id_snapshot: "fake-id",
            node_title_snapshot: "Entfernter Knoten",
            visibility: "public",
            created_at: "2026-07-27T08:00:00Z",
            updated_at: "2026-07-27T09:05:00Z",
            archived_at: "2026-07-27T09:05:00Z",
            deleted_at: null,
          }),
        });
      }
      if (
        path ===
        "/api/conversations/70000000-0000-4000-8000-000000000001/messages"
      ) {
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            items: [
              {
                id: "80000000-0000-4000-8000-000000000001",
                conversation_id: "70000000-0000-4000-8000-000000000001",
                author_account_id: "e2e-weber",
                author_title: "Eigene Garnrolle",
                content: "Erhaltener Gesprächsbeitrag",
                created_at: "2026-07-27T08:30:00Z",
                updated_at: "2026-07-27T08:30:00Z",
                deleted_at: null,
              },
            ],
            page: { limit: 50, next_cursor: null, has_more: false },
          }),
        });
      }
      return route.fallback();
    });
    await page.goto("/map");

    const panel = await openFirstNode(page);
    const markerCountBeforeDelete = await page.locator(".map-marker").count();
    await panel.getByRole("tab", { name: "Bearbeiten" }).click();
    await confirmNodeRemoval(panel);

    const receipt = panel.getByRole("link", { name: "Archiv öffnen" });
    await expect(receipt).toBeVisible();
    await expect(receipt).toHaveAttribute(
      "href",
      "/archive?id=70000000-0000-4000-8000-000000000001",
    );
    await expect(page.locator(".map-marker")).toHaveCount(
      markerCountBeforeDelete - 1,
    );
    await receipt.click();
    await expect(page).toHaveURL(
      /\/archive\?id=70000000-0000-4000-8000-000000000001$/,
    );
    await expect(
      page.getByRole("heading", { level: 1, name: "Entfernter Knoten" }),
    ).toBeVisible();
    await expect(page.getByText("Erhaltener Gesprächsbeitrag")).toBeVisible();
    await expect(page.locator("main button, main form")).toHaveCount(0);
  });

  test("Gast kann einen selbst geknüpften Knoten bearbeiten", async ({
    page,
  }) => {
    const guestId = "e2e-gast";
    await mockApiResponses(page, {
      auth: {
        authenticated: true,
        account_id: guestId,
        role: "gast",
      },
    });
    await page.goto("/map");
    const created = await page.evaluate(async (accountId) => {
      const response = await fetch("/api/nodes", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          title: "Eigener Gastknoten",
          kind: "place",
          address: "Gastweg 1",
          location: { lat: 53.5, lon: 10.0 },
          tags: [],
          operation_id: "40000000-0000-4000-8000-000000000001",
        }),
      });
      const node = await response.json();
      if (node.created_by_account_id !== accountId) {
        throw new Error("mock creator binding missing");
      }
      return node;
    }, guestId);
    await page.goto(`/map?focus=node:${created.id}`);
    const panel = page.locator('[data-testid="context-panel"]');
    await expect(panel).toBeVisible();
    await expect(panel.locator("h3")).toHaveText("Eigener Gastknoten");
    await panel.getByRole("tab", { name: "Bearbeiten" }).click();
    await panel
      .getByRole("button", { name: "Bearbeiten", exact: true })
      .click();
    await panel.getByLabel("Titel").fill("Vom Gast gepflegt");
    await panel.getByRole("button", { name: "Änderungen speichern" }).click();
    await expect(panel.locator("h3")).toHaveText("Vom Gast gepflegt");
  });

  test("Gast sieht an einem fremden Knoten keine Bearbeitungs- oder Löschaktion", async ({
    page,
  }) => {
    await mockApiResponses(page, {
      auth: {
        authenticated: true,
        account_id: "e2e-gast",
        role: "gast",
      },
    });
    await page.goto("/map");

    const panel = await openFirstNode(page);
    await expect(panel.getByRole("tab", { name: "Bearbeiten" })).toHaveCount(0);
    await expect(
      panel.getByRole("button", { name: "Bearbeiten", exact: true }),
    ).toHaveCount(0);
    await expect(
      panel.getByRole("button", { name: "Aus dem Gewebe entfernen" }),
    ).toHaveCount(0);

    const tabList = panel.getByRole("tablist", { name: "Knoten-Tabs" });
    const uebersichtTab = tabList.getByRole("tab", { name: "Übersicht" });
    const gespraechTab = tabList.getByRole("tab", { name: "Gespräch" });
    const verlaufTab = tabList.getByRole("tab", { name: "Verlauf" });
    const antraegeTab = tabList.getByRole("tab", { name: "Anträge" });
    await expect(tabList.getByRole("tab")).toHaveCount(4);

    const tabContract = [
      [uebersichtTab, "panel-uebersicht"],
      [gespraechTab, "panel-gespraech"],
      [verlaufTab, "panel-verlauf"],
      [antraegeTab, "panel-antraege"],
    ] as const;
    for (const [tab, panelId] of tabContract) {
      await expect(tab).toHaveAttribute("aria-controls", panelId);
      await expect(panel.locator(`#${panelId}`)).toHaveCount(1);
    }
    await expect(panel.locator("#panel-uebersicht")).toBeVisible();
    await expect(panel.locator("#panel-gespraech")).toBeHidden();
    await expect(panel.locator("#panel-verlauf")).toBeHidden();
    await expect(panel.locator("#panel-antraege")).toBeHidden();

    await uebersichtTab.focus();
    await page.keyboard.press("ArrowRight");
    await expect(gespraechTab).toBeFocused();
    await expect(gespraechTab).toHaveAttribute("aria-selected", "true");
    await expect(gespraechTab).toHaveAttribute("tabindex", "0");
    await expect(panel.locator("#panel-gespraech")).toBeVisible();
    await expect(panel.locator("#panel-uebersicht")).toBeHidden();

    await page.keyboard.press("End");
    await expect(antraegeTab).toBeFocused();
    await expect(antraegeTab).toHaveAttribute("aria-selected", "true");
    await expect(antraegeTab).toHaveAttribute("tabindex", "0");
    await expect(panel.locator("#panel-antraege")).toBeVisible();

    await page.keyboard.press("Home");
    await expect(uebersichtTab).toBeFocused();
    await expect(uebersichtTab).toHaveAttribute("aria-selected", "true");
    await expect(panel.locator("#panel-uebersicht")).toBeVisible();

    await page.keyboard.press("ArrowLeft");
    await expect(antraegeTab).toBeFocused();
    await expect(antraegeTab).toHaveAttribute("aria-selected", "true");
    await expect(uebersichtTab).toHaveAttribute("tabindex", "-1");
  });

  test("Weber can create a node Sachantrag without a client-supplied center", async ({
    page,
  }) => {
    await mockApiResponses(page, {
      auth: { authenticated: true, account_id: "e2e-weber", role: "weber" },
    });
    const proposalRequests: Record<string, unknown>[] = [];
    await page.route("**/api/proposals", async (route) => {
      if (route.request().method() === "POST") {
        const body = route.request().postDataJSON() as Record<string, unknown>;
        proposalRequests.push(body);
        await route.fulfill({
          status: 201,
          contentType: "application/json",
          body: JSON.stringify({
            id: "node-sachantrag",
            ...body,
            webgemeindezentrum_id: "resolved-by-api",
            target_node_title: "Demo Node",
            applicant_account_id: "e2e-weber",
            applicant_title: "E2E Weber",
            status: "consent",
            created_at: "2026-08-08T12:00:00Z",
            consent_until: "2026-08-15T12:00:00Z",
            veto_count: 0,
            message_count: 0,
            yes_votes: 0,
            no_votes: 0,
            abstain_votes: 0,
            remaining_seconds: 604800,
          }),
        });
        return;
      }
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: "[]",
      });
    });
    await page.goto("/map");

    const panel = await openFirstNode(page);
    await panel.getByRole("tab", { name: "Anträge" }).click();
    await panel
      .getByLabel(/Sachantrag zu/)
      .fill("Nutzung des Knotens beschließen");
    await panel.getByLabel("Begründung").fill("Gemeinsam und nachvollziehbar.");
    await panel.getByRole("button", { name: "Sachantrag stellen" }).click();

    await expect.poll(() => proposalRequests.length).toBe(1);
    expect(proposalRequests[0]).toMatchObject({
      kind: "sachantrag",
      title: "Nutzung des Knotens beschließen",
      summary: "Gemeinsam und nachvollziehbar.",
    });
    expect(proposalRequests[0]).toHaveProperty("target_node_id");
    expect(proposalRequests[0]).not.toHaveProperty("webgemeindezentrum_id");
  });

  test("fällt bei Berechtigungsverlust aus dem offenen Formular sicher auf Übersicht zurück", async ({
    page,
  }) => {
    await mockApiResponses(page, {
      auth: { authenticated: true, account_id: "e2e-weber", role: "weber" },
    });
    await page.goto("/map");

    const panel = await openFirstNode(page);
    await panel.getByRole("tab", { name: "Bearbeiten" }).click();
    await panel
      .getByRole("button", { name: "Bearbeiten", exact: true })
      .click();
    await panel.getByLabel("Titel").fill("Nicht gespeicherter Entwurf");
    await expect(panel.locator("form")).toBeVisible();

    const logoutStatus = await page.evaluate(async () => {
      const response = await fetch("/api/auth/logout", { method: "POST" });
      return response.status;
    });
    expect(logoutStatus).toBe(200);
    await page.reload();

    const guestPanel = await openFirstNode(page);
    const overviewTab = guestPanel.getByRole("tab", { name: "Übersicht" });
    await expect(guestPanel.locator("form")).toHaveCount(0);
    await expect(
      guestPanel.getByRole("tab", { name: "Bearbeiten" }),
    ).toHaveCount(0);
    await expect(overviewTab).toHaveAttribute("aria-selected", "true");
    await expect(guestPanel.locator("#panel-uebersicht")).toBeVisible();
    await expect(
      guestPanel.getByText("Nicht gespeicherter Entwurf"),
    ).toHaveCount(0);
  });

  test("zeigt einen Löschkonflikt, erhält den Knoten und bewahrt die Rückmeldung beim Reiterwechsel", async ({
    page,
  }) => {
    await mockApiResponses(page, {
      auth: { authenticated: true, account_id: "e2e-weber", role: "weber" },
    });

    let observedIfMatch = "";
    await page.route("**/api/nodes/*", async (route, request) => {
      if (request.method() !== "DELETE") {
        await route.fallback();
        return;
      }
      observedIfMatch = request.headers()["if-match"] ?? "";
      const requestedNodeId = decodeURIComponent(
        new URL(request.url()).pathname.split("/").pop() ?? "",
      );
      await route.fulfill({
        status: 412,
        contentType: "application/json",
        body: JSON.stringify({
          id: requestedNodeId,
          title: "Zwischenzeitlich aktualisierter Knoten",
          kind: "place",
          summary: "Dieser aktuelle Stand darf nicht gelöscht werden.",
          address: "Aktuelle Adresse",
          location: { lat: 53.5, lon: 10 },
          tags: ["aktuell"],
          updated_at: "2026-07-31T18:00:00Z",
        }),
      });
    });

    await page.goto("/map");
    const panel = await openFirstNode(page);
    const markerCountBeforeDelete = await page.locator(".map-marker").count();
    const editTab = panel.getByRole("tab", { name: "Bearbeiten" });
    await editTab.click();
    await confirmNodeRemoval(panel);

    const errorMessage =
      "Der Knoten wurde in der Zwischenzeit geändert und konnte nicht gelöscht werden. Die Ansicht zeigt nun den aktuellen Stand.";
    await expect(panel.getByText(errorMessage)).toBeVisible();
    expect(observedIfMatch).toMatch(/^".+"$/);
    await expect(page.locator(".map-marker")).toHaveCount(
      markerCountBeforeDelete,
    );
    await expect(panel.locator("h3")).toHaveText(
      "Zwischenzeitlich aktualisierter Knoten",
    );
    await expect(
      panel.getByRole("button", { name: "Aus dem Gewebe entfernen" }),
    ).toBeFocused();

    await panel.getByRole("tab", { name: "Übersicht" }).click();
    await expect(panel.getByText(errorMessage)).toHaveCount(0);
    await editTab.click();
    await expect(panel.getByText(errorMessage)).toBeVisible();
  });

  test("bewahrt den Entwurf bei 412 und speichert nach bewusstem Vergleich erneut", async ({
    page,
  }) => {
    await mockApiResponses(page, {
      auth: { authenticated: true, account_id: "e2e-weber", role: "weber" },
    });

    const observedIfMatch: string[] = [];
    let putAttempt = 0;
    await page.route("**/api/nodes/*", async (route, request) => {
      if (request.method() !== "PUT") {
        await route.fallback();
        return;
      }
      observedIfMatch.push(request.headers()["if-match"] ?? "");
      const requestedNodeId = decodeURIComponent(
        new URL(request.url()).pathname.split("/").pop() ?? "",
      );
      expect(requestedNodeId).not.toBe("");
      putAttempt += 1;
      if (putAttempt === 1) {
        await route.fulfill({
          status: 412,
          contentType: "application/json",
          body: JSON.stringify({
            id: requestedNodeId,
            title: "Neuer Titel vom Server",
            kind: "place",
            summary: "Neue Kurzbeschreibung vom Server",
            address: "Adresse",
            location: { lat: 0, lon: 0 },
            tags: ["server"],
            updated_at: "2026-07-18T10:00:00Z",
          }),
        });
        return;
      }

      const payload = request.postDataJSON();
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          id: requestedNodeId,
          ...payload,
          created_at: "2026-01-01T00:00:00Z",
          updated_at: "2026-07-18T10:01:00Z",
        }),
      });
    });

    await page.goto("/map");
    const panel = await openFirstNode(page);
    await panel.getByRole("tab", { name: "Bearbeiten" }).click();
    await panel
      .getByRole("button", { name: "Bearbeiten", exact: true })
      .click();
    await panel.getByLabel("Titel").fill("Mein Konflikt");
    await panel.getByRole("button", { name: "Änderungen speichern" }).click();

    expect(observedIfMatch[0]).toMatch(/^".+"$/);
    await expect(
      panel.getByText(
        "Der Knoten wurde in der Zwischenzeit geändert. Dein Entwurf bleibt erhalten. Vergleiche ihn mit dem aktuellen Stand und speichere anschließend erneut.",
      ),
    ).toBeVisible();
    await expect(panel.getByLabel("Titel")).toHaveValue("Mein Konflikt");
    const current = panel.getByRole("region", {
      name: "Aktueller Serverstand",
    });
    await expect(current).toContainText("Neuer Titel vom Server");
    await expect(current).toContainText("Neue Kurzbeschreibung vom Server");
    await expect(
      panel.getByRole("button", { name: "Änderungen speichern" }),
    ).toBeVisible();

    await panel.getByRole("button", { name: "Änderungen speichern" }).click();
    expect(observedIfMatch[1]).toBe('"2026-07-18T10:00:00Z"');
    await expect(panel.locator("h3")).toHaveText("Mein Konflikt");
    await expect(
      panel.getByRole("button", { name: "Änderungen speichern" }),
    ).toHaveCount(0);
  });
});
