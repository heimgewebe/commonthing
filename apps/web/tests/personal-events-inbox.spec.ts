import { expect, test, type Page } from "@playwright/test";
import { mockApiResponses } from "./fixtures/mockApi";

type Kind = "circle_invited" | "circle_invitation_sent";

async function installPersonalInbox(page: Page, kind: Kind = "circle_invited") {
  let acted = false;
  let read = false;
  let state = "invited";
  let actions = 0;
  const circleId = "22222222-2222-4222-8222-222222222222";
  const id = "11111111-1111-4111-8111-111111111111";

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

test("Ablehnung benutzt den vorhandenen Mitgliedschaftsaustritt und entfernt die Aktion", async ({
  page,
}) => {
  const server = await installPersonalInbox(page);
  await page.goto("/nachrichten");
  await page
    .getByTestId("personal-events-inbox")
    .getByRole("button", { name: "Ablehnen" })
    .click();
  await expect.poll(server.countActions).toBe(1);
  await expect(
    page
      .getByTestId("personal-events-inbox")
      .getByRole("button", { name: "Ablehnen" }),
  ).toHaveCount(0);
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
