import { expect, test, type Page } from "@playwright/test";
import { mockApiResponses } from "./fixtures/mockApi";

type Kind = "circle_invited" | "circle_invitation_sent";

async function installPersonalInbox(
  page: Page,
  kind: Kind = "circle_invited",
  failReadReceipt = false,
) {
  let acted = false;
  let read = false;
  let state = "invited";
  let actions = 0;
  let declineExpectedStatus: string | null = null;
  let acceptedInOtherTab = false;
  const circleId = "22222222-2222-4222-8222-222222222222";
  const id = "11111111-1111-4111-8111-111111111111";
  const invitationCreatedAt = "2026-11-12T12:00:00Z";

  await mockApiResponses(page, {
    auth: { authenticated: true, account_id: "personal-a", role: "weber" },
  });
  await page.route("**/api/direct-conversations", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ items: [] }),
    }),
  );
  await page.route("**/api/personal-events**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/read") && route.request().method() === "POST") {
      if (failReadReceipt) return route.fulfill({ status: 503 });
      read = true;
      return route.fulfill({ status: 204 });
    }
    if (path !== "/api/personal-events" || route.request().method() !== "GET")
      return route.fallback();
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        items: [
          {
            id,
            event_kind: kind,
            invitation_created_at: invitationCreatedAt,
            content:
              kind === "circle_invited"
                ? "Du hast eine Einladung zu einer Gemeinschaft erhalten."
                : "Du hast eine Einladung zu einer Gemeinschaft verschickt.",
            created_at: "2026-11-12T12:00:00Z",
            read_at: read ? "2026-11-12T12:30:00Z" : null,
            circle_id: circleId,
            circle_name: "Hausgemeinschaft",
            target_account_id:
              kind === "circle_invitation_sent" && !acted ? "personal-b" : null,
            actionable: !acted,
          },
        ],
        unread_count: read ? 0 : 1,
        has_more: false,
      }),
    });
  });

  await page.route("**/api/circles/**", (route) => {
    const request = route.request();
    if (
      request.method() === "POST" &&
      new URL(request.url()).pathname === `/api/circles/${circleId}/accept`
    ) {
      actions += 1;
      acted = true;
      state = "active";
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          id: circleId,
          kind: "household",
          name: "Hausgemeinschaft",
          state,
          membership_status: state,
        }),
      });
    }
    if (
      request.method() === "DELETE" &&
      new URL(request.url()).pathname.startsWith(`/api/circles/${circleId}/`)
    ) {
      const url = new URL(request.url());
      if (url.pathname.endsWith("/membership")) {
        declineExpectedStatus = url.searchParams.get("expected_status");
        if (acceptedInOtherTab) {
          // Backend recognizes the stale invite and preserves membership.
          acted = true;
          return route.fulfill({ status: 409 });
        }
      }
      actions += 1;
      acted = true;
      return route.fulfill({ status: 204 });
    }
    return route.fallback();
  });

  return {
    countActions: () => actions,
    wasRead: () => read,
    wasActed: () => acted,
    invitationCreatedAt: () => invitationCreatedAt,
    declineExpectedStatus: () => declineExpectedStatus,
    simulateOtherTabAccept: () => {
      acceptedInOtherTab = true;
    },
  };
}

test("Einladung erscheint im echten Postfach, kann angenommen werden und wird nicht nochmals angeboten", async ({
  page,
}) => {
  const server = await installPersonalInbox(page);
  await page.goto("/nachrichten");
  const inbox = page.getByTestId("personal-events-inbox");
  await expect(
    inbox.getByText("Du hast eine Einladung", { exact: false }),
  ).toBeVisible();
  await expect(inbox.getByRole("button", { name: "Annehmen" })).toBeVisible();
  await inbox.getByRole("button", { name: "Annehmen" }).click();
  await expect.poll(server.countActions).toBe(1);
  await expect.poll(server.wasRead).toBe(true);
  await expect(inbox.getByRole("button", { name: "Annehmen" })).toHaveCount(0);
  await page.reload();
  await expect(inbox.getByRole("button", { name: "Annehmen" })).toHaveCount(0);
});

test("Ablehnung ist auf eine ausstehende Einladung beschränkt", async ({
  page,
}) => {
  const server = await installPersonalInbox(page);
  await page.goto("/nachrichten");
  await page
    .getByTestId("personal-events-inbox")
    .getByRole("button", { name: "Ablehnen" })
    .click();
  await expect.poll(server.countActions).toBe(1);
  expect(server.declineExpectedStatus()).toBe("invited");
  await expect(
    page
      .getByTestId("personal-events-inbox")
      .getByRole("button", { name: "Ablehnen" }),
  ).toHaveCount(0);
});

test("veraltetes Ablehnen nach Annahme in einem anderen Tab löscht keine Mitgliedschaft", async ({
  page,
}) => {
  const server = await installPersonalInbox(page);
  await page.goto("/nachrichten");
  const inbox = page.getByTestId("personal-events-inbox");
  await expect(inbox.getByRole("button", { name: "Ablehnen" })).toBeVisible();
  server.simulateOtherTabAccept();
  await inbox.getByRole("button", { name: "Ablehnen" }).click();
  expect(server.declineExpectedStatus()).toBe("invited");
  await expect.poll(server.countActions).toBe(0);
  await expect(inbox.getByRole("button", { name: "Ablehnen" })).toHaveCount(0);
  await expect(inbox.getByRole("alert")).toContainText("nicht abgeschlossen");
});

test("Gesendete offene Einladung kann zurückgenommen werden, ohne fiktiven Chatpartner", async ({
  page,
}) => {
  const server = await installPersonalInbox(page, "circle_invitation_sent");
  await page.goto("/nachrichten");
  const inbox = page.getByTestId("personal-events-inbox");
  await expect(
    inbox.getByRole("button", { name: "Einladung zurückziehen" }),
  ).toBeVisible();
  await inbox.getByRole("button", { name: "Einladung zurückziehen" }).click();
  await expect.poll(server.countActions).toBe(1);
  await expect(
    inbox.getByRole("button", { name: "Einladung zurückziehen" }),
  ).toHaveCount(0);
});
test("Erfolgreiche Ablehnung bleibt trotz fehlerhafter Lesebestätigung sichtbar", async ({
  page,
}) => {
  await installPersonalInbox(page, "circle_invited", true);
  await page.goto("/nachrichten");
  const inbox = page.getByTestId("personal-events-inbox");
  await inbox.getByRole("button", { name: "Ablehnen" }).click();
  await expect(inbox.getByRole("button", { name: "Ablehnen" })).toHaveCount(0);
  await expect(inbox.getByRole("alert")).toContainText(
    "nur die Lesebestätigung ist fehlgeschlagen",
  );
  await expect(page).toHaveURL(/nachrichten/);
});

test("Alte Hausgemeinschaftsoberfläche darf nach Annahme anderswo keine Mitgliedschaft löschen", async ({
  page,
}) => {
  const circleId = "22222222-2222-4222-8222-222222222222";
  let membership: "invited" | "active" = "invited";
  let expectedStatus: string | null = null;
  let expectedInvitationCreatedAt: string | null = null;
  let revokedActiveMember = false;
  await mockApiResponses(page, {
    auth: { authenticated: true, account_id: "personal-a", role: "weber" },
  });
  await page.route("**/api/circles/me*", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([
        {
          id: circleId,
          kind: "household",
          name: "Altes Haus",
          state: "active",
          membership_status: membership,
          invitation_created_at: "2026-11-12T12:00:00Z",
        },
      ]),
    }),
  );
  await page.route(`**/api/circles/${circleId}/membership*`, (route) => {
    if (route.request().method() !== "DELETE") return route.fallback();
    const query = new URL(route.request().url()).searchParams;
    expectedStatus = query.get("expected_status");
    expectedInvitationCreatedAt = query.get("expected_invitation_created_at");
    // Emulate the real conditional DELETE: only a still-pending invitation
    // may be declined, never an already accepted member.
    if (membership === "active" && expectedStatus !== "invited") {
      revokedActiveMember = true;
      return route.fulfill({ status: 204 });
    }
    return route.fulfill({ status: membership === "active" ? 409 : 204 });
  });

  await page.goto("/settings");
  const controls = page.getByTestId("audience-circle-controls");
  await expect(
    controls.getByRole("button", { name: "Ablehnen" }),
  ).toBeVisible();
  membership = "active"; // acceptance in another tab, panel has stale state
  await controls.getByRole("button", { name: "Ablehnen" }).click();
  expect(expectedStatus).toBe("invited");
  expect(expectedInvitationCreatedAt).toBe("2026-11-12T12:00:00Z");
  expect(revokedActiveMember).toBe(false);
  expect(membership).toBe("active");
  await expect(controls.getByRole("alert")).toContainText(
    "Die Aktion konnte nicht abgeschlossen werden.",
  );
});

test("Automatischer Postfach-Refresh erhält alle bereits geladenen älteren Mitteilungen", async ({
  page,
}) => {
  await mockApiResponses(page, {
    auth: { authenticated: true, account_id: "personal-a", role: "weber" },
  });
  await page.route("**/api/direct-conversations", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ items: [] }),
    }),
  );
  const allEvents = Array.from({ length: 55 }, (_, i) => ({
    id: `notice-${i}`,
    event_kind: "circle_left",
    invitation_created_at: null,
    content: `Historische Mitteilung ${i + 1}`,
    created_at: "2026-11-12T12:00:00Z",
    read_at: "2026-11-12T12:10:00Z",
    circle_id: null,
    circle_name: null,
    target_account_id: null,
    actionable: false,
  }));
  let initialPageRequests = 0;
  await page.route("**/api/personal-events**", (route) => {
    const url = new URL(route.request().url());
    if (route.request().method() !== "GET") return route.fallback();
    const before = url.searchParams.get("before");
    if (!before) initialPageRequests += 1;
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        items:
          before === "notice-49" ? allEvents.slice(50) : allEvents.slice(0, 50),
        unread_count: 0,
        has_more: !before,
      }),
    });
  });

  await page.goto("/nachrichten");
  const inbox = page.getByTestId("personal-events-inbox");
  await expect(
    inbox.getByText("Historische Mitteilung 1", { exact: true }),
  ).toBeVisible();
  await inbox
    .getByRole("button", { name: "Ältere Mitteilungen laden" })
    .click();
  await expect(
    inbox.getByText("Historische Mitteilung 55", { exact: true }),
  ).toBeVisible();

  await inbox.getByRole("button", { name: "Aktualisieren" }).click();
  await expect.poll(() => initialPageRequests).toBeGreaterThanOrEqual(2);
  await expect(
    inbox.getByText("Historische Mitteilung 55", { exact: true }),
  ).toBeVisible();
  await expect(inbox.locator("li")).toHaveCount(55);

  await page.evaluate(() => window.dispatchEvent(new Event("focus")));
  await expect.poll(() => initialPageRequests).toBeGreaterThanOrEqual(3);
  await expect(
    inbox.getByText("Historische Mitteilung 55", { exact: true }),
  ).toBeVisible();
  await expect(
    inbox.getByRole("button", { name: "Ältere Mitteilungen laden" }),
  ).toHaveCount(0);
});

test("Mehr als 50 neue Mitteilungen lassen keine unsichtbare Lücke in der Pagination", async ({
  page,
}) => {
  await mockApiResponses(page, {
    auth: { authenticated: true, account_id: "personal-a", role: "weber" },
  });
  await page.route("**/api/direct-conversations", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ items: [] }),
    }),
  );
  let burst = false;
  const item = (id: string, content: string) => ({
    id,
    event_kind: "circle_left",
    invitation_created_at: null,
    content,
    created_at: "2026-11-12T12:00:00Z",
    read_at: "2026-11-12T12:10:00Z",
    circle_id: null,
    circle_name: null,
    target_account_id: null,
    actionable: false,
  });
  const old = Array.from({ length: 3 }, (_, i) =>
    item(`old-${i}`, `Alt ${i + 1}`),
  );
  const fresh = Array.from({ length: 55 }, (_, i) =>
    item(`new-${i}`, `Neu ${i + 1}`),
  );
  await page.route("**/api/personal-events**", (route) => {
    if (route.request().method() !== "GET") return route.fallback();
    const before = new URL(route.request().url()).searchParams.get("before");
    const items = !burst
      ? old
      : before === "new-49"
        ? fresh.slice(50)
        : before === "new-54"
          ? old
          : fresh.slice(0, 50);
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        items,
        unread_count: 0,
        has_more: burst && before !== "new-54",
      }),
    });
  });

  await page.goto("/nachrichten");
  const inbox = page.getByTestId("personal-events-inbox");
  await expect(inbox.getByText("Alt 3", { exact: true })).toBeVisible();
  burst = true;
  await inbox.getByRole("button", { name: "Aktualisieren" }).click();
  await expect(inbox.getByText("Neu 1", { exact: true })).toBeVisible();
  await expect(inbox.getByText("Alt 3", { exact: true })).toHaveCount(0);
  await inbox
    .getByRole("button", { name: "Ältere Mitteilungen laden" })
    .click();
  await expect(inbox.getByText("Neu 55", { exact: true })).toBeVisible();
  await inbox
    .getByRole("button", { name: "Ältere Mitteilungen laden" })
    .click();
  await expect(inbox.getByText("Alt 3", { exact: true })).toBeVisible();
  await expect(inbox.locator("li")).toHaveCount(58);
});

test("Bereits beantwortete Einladungen aus älteren Seiten bleiben beim Refresh erledigt", async ({
  page,
}) => {
  const circleId = "22222222-2222-4222-8222-222222222222";
  await mockApiResponses(page, {
    auth: { authenticated: true, account_id: "personal-a", role: "weber" },
  });
  await page.route("**/api/direct-conversations", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ items: [] }),
    }),
  );
  const notice = (id: string, invited = false) => ({
    id,
    event_kind: invited ? "circle_invited" : "circle_left",
    invitation_created_at: invited ? "2026-11-12T12:00:00Z" : null,
    content: invited ? "Alte Einladung beantworten" : "Unkritische Nachricht",
    created_at: "2026-11-12T12:00:00Z",
    read_at: invited ? null : "2026-11-12T12:10:00Z",
    circle_id: invited ? circleId : null,
    circle_name: invited ? "Hausgemeinschaft" : null,
    target_account_id: null,
    actionable: invited,
  });
  const pageOne = Array.from({ length: 50 }, (_, i) => notice(`new-${i}`));
  const olderInvite = notice("old-invite", true);
  await page.route("**/api/personal-events**", (route) => {
    const url = new URL(route.request().url());
    if (route.request().method() === "POST" && url.pathname.endsWith("/read")) {
      return route.fulfill({ status: 204 });
    }
    if (route.request().method() !== "GET") return route.fallback();
    const before = url.searchParams.get("before");
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        items: before ? [olderInvite] : pageOne,
        unread_count: 0,
        has_more: !before,
      }),
    });
  });
  await page.route(`**/api/circles/${circleId}/accept*`, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        id: circleId,
        kind: "household",
        name: "Hausgemeinschaft",
        state: "active",
        membership_status: "active",
      }),
    }),
  );

  await page.goto("/nachrichten");
  const inbox = page.getByTestId("personal-events-inbox");
  await inbox
    .getByRole("button", { name: "Ältere Mitteilungen laden" })
    .click();
  await expect(inbox.getByRole("button", { name: "Annehmen" })).toBeVisible();
  await inbox.getByRole("button", { name: "Annehmen" }).click();
  await expect(inbox.getByRole("button", { name: "Annehmen" })).toHaveCount(0);
  await expect(inbox.getByText("Einladung nicht mehr offen")).toBeVisible();
  await expect(inbox.getByText("Alte Einladung beantworten")).toBeVisible();
});
