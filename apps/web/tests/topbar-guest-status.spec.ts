import { test, expect } from "@playwright/test";
import { mockApiResponses } from "./fixtures/mockApi";

const PENDING_WEBER_PROPOSAL = {
  id: "pending-weber",
  kind: "weberantrag",
  applicant_account_id: "guest-topbar",
  applicant_title: "Gast Topbar",
  status: "consent",
  created_at: "2026-08-17T06:00:00Z",
  consent_until: "2026-08-24T06:00:00Z",
  webgemeindezentrum_id: "wgz-test",
  veto_count: 0,
  yes_votes: 0,
  no_votes: 0,
  abstain_votes: 0,
};

function directConversation(id: string, unreadCount: number, at: string) {
  return {
    id,
    counterpart_account_id: `counterpart-${id}`,
    counterpart_title: "Ada",
    created_at: at,
    updated_at: at,
    unread_count: unreadCount,
    last_message_preview: "Hallo",
    last_message_at: at,
    blocked_by_me: false,
    can_send: true,
  };
}

test.describe("Topbar — guest role visibility", () => {
  test("an authenticated guest sees a Gast badge that links to the Weber application", async ({
    page,
  }) => {
    await mockApiResponses(page, {
      auth: { authenticated: true, account_id: "guest-topbar", role: "gast" },
    });
    await page.route("**/api/proposals", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([]),
      });
    });
    await page.goto("/map");

    const badge = page.getByTestId("topbar-guest-badge");
    await expect(badge).toBeVisible();
    await expect(badge).toHaveAttribute("data-state", "available");
    await expect(badge).toHaveAttribute("href", "/antraege#antrag-stellen");
    await expect(badge).toContainText("Gast");

    const box = await badge.boundingBox();
    expect(box, "guest badge has no visible box").not.toBeNull();
    expect(box!.height).toBeGreaterThanOrEqual(44);

    await expect(
      page.getByRole("link", { name: "Private Nachrichten" }),
    ).toBeVisible();

    await badge.click();
    await expect(page).toHaveURL(/\/antraege#antrag-stellen$/);
    await expect(
      page.getByRole("heading", { name: "Weberstatus beantragen" }),
    ).toBeVisible();
  });

  test("active unread attention keeps the pending application as lower-priority waiting attention", async ({
    page,
  }) => {
    await mockApiResponses(page, {
      auth: { authenticated: true, account_id: "guest-topbar", role: "gast" },
    });
    await page.route("**/api/proposals", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([PENDING_WEBER_PROPOSAL]),
      });
    });
    await page.route("**/api/direct-conversations", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          items: [directConversation("dm-1", 3, "2026-08-17T07:00:00Z")],
        }),
      });
    });

    await page.goto("/map");

    const badge = page.getByTestId("topbar-guest-badge");
    await expect(badge).toHaveAttribute("data-state", "pending");
    await expect(badge).toContainText("Weberstatus beantragt");
    await expect(badge).toHaveAttribute("href", "/antraege");

    const messages = page.getByRole("link", { name: "Private Nachrichten" });
    await expect(messages).toBeVisible();
    await expect(messages.locator(".message-unread-badge")).toHaveCount(0);

    const attention = page.getByTestId("attention-bubbles");
    await expect(attention).toBeVisible();
    const bubbles = attention.locator(".attention-bubble");
    await expect(bubbles).toHaveCount(2);
    await expect(bubbles.nth(0)).toHaveAttribute(
      "data-attention-id",
      "direct:dm-1",
    );
    await expect(bubbles.nth(1)).toHaveAttribute(
      "data-attention-id",
      "waiting-summary:guest-topbar",
    );
    await attention.locator('[data-attention-id="direct:dm-1"]').click();
    await expect(
      page
        .getByTestId("attention-card")
        .getByRole("link", { name: "Nachricht öffnen" }),
    ).toHaveAttribute("href", "/nachrichten?id=dm-1");

    await page.keyboard.press("Escape");
    await expect(
      attention.locator('[data-attention-id="waiting-summary:guest-topbar"]'),
    ).toBeVisible();
    await expect(badge).toHaveAttribute("href", "/antraege");
  });

  test("a failed initial proposal read never masquerades as permission to apply", async ({
    page,
  }) => {
    await mockApiResponses(page, {
      auth: { authenticated: true, account_id: "guest-topbar", role: "gast" },
    });
    let proposalRequests = 0;
    await page.route("**/api/proposals", async (route) => {
      proposalRequests += 1;
      await route.fulfill({ status: 503, body: "temporarily unavailable" });
    });

    await page.goto("/map");
    await expect.poll(() => proposalRequests).toBeGreaterThan(0);

    const badge = page.getByTestId("topbar-guest-badge");
    await expect(badge).toHaveAttribute("data-state", "unknown");
    await expect(badge).toHaveAttribute("href", "/antraege");
    await expect(badge).toHaveAttribute(
      "aria-label",
      "Rolle: Gast – Weberstatus wird geprüft",
    );
    await expect(badge).not.toContainText("Weber werden");
  });

  test("a saturated conversation bubble uses lower-bound wording", async ({
    page,
  }) => {
    await mockApiResponses(page, {
      auth: { authenticated: true, account_id: "guest-topbar", role: "gast" },
    });
    await page.route("**/api/direct-conversations", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          items: [directConversation("dm-many", 120, "2026-08-17T07:00:00Z")],
        }),
      });
    });

    await page.goto("/map");

    const bubble = page.locator('[data-attention-id="direct:dm-many"]');
    await expect(bubble).toBeVisible();
    await expect(bubble).toHaveAttribute(
      "aria-label",
      "Neu für dich. Ada: 99 oder mehr ungelesene Nachrichten",
    );
    await expect(bubble.locator(".attention-count")).toHaveText("99+");
  });

  test("a pending guest stays compact and separated from attention at 320 pixels", async ({
    page,
  }) => {
    await mockApiResponses(page, {
      auth: { authenticated: true, account_id: "guest-topbar", role: "gast" },
    });
    await page.route("**/api/proposals", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([PENDING_WEBER_PROPOSAL]),
      });
    });
    await page.setViewportSize({ width: 320, height: 568 });
    await page.goto("/map");

    const badge = page.getByTestId("topbar-guest-badge");
    await expect(badge).toBeVisible();
    await expect(badge).toHaveAttribute("data-state", "pending");
    await expect(badge.locator(".guest-badge-cta")).toBeHidden();
    await expect(badge.locator(".guest-badge-compact")).toBeVisible();

    const box = await badge.boundingBox();
    expect(box, "guest badge has no visible box").not.toBeNull();
    expect(box!.height).toBeGreaterThanOrEqual(44);
    expect(box!.width).toBe(44);
    expect(box!.x).toBeGreaterThanOrEqual(0);
    expect(box!.x + box!.width).toBeLessThanOrEqual(320);

    const authBox = await page.locator(".auth-slot").boundingBox();
    const attentionBox = await page
      .getByTestId("attention-bubbles")
      .boundingBox();
    expect(authBox, "auth slot has no visible box").not.toBeNull();
    expect(
      attentionBox,
      "attention bubbles have no visible box",
    ).not.toBeNull();
    expect(attentionBox!.x).toBeGreaterThanOrEqual(0);
    expect(attentionBox!.x + attentionBox!.width).toBeLessThanOrEqual(
      authBox!.x,
    );

    await expect(
      page.getByRole("link", { name: "Private Nachrichten" }),
    ).toBeVisible();
  });

  test("a weber does not see the guest badge", async ({ page }) => {
    await mockApiResponses(page, {
      auth: {
        authenticated: true,
        account_id: "weber-topbar",
        role: "weber",
      },
    });
    await page.goto("/map");

    await expect(
      page.getByRole("link", { name: "Private Nachrichten" }),
    ).toBeVisible();
    await expect(page.getByTestId("topbar-guest-badge")).toHaveCount(0);
  });

  test("an anonymous visitor sees neither guest badge nor attention bubbles", async ({
    page,
  }) => {
    await mockApiResponses(page, { auth: { authenticated: false } });
    await page.goto("/map");

    await expect(
      page.getByRole("link", { name: "Anmelden", exact: true }),
    ).toBeVisible();
    await expect(page.getByTestId("topbar-guest-badge")).toHaveCount(0);
    await expect(page.getByTestId("attention-bubbles")).toHaveCount(0);
  });
});
