import { test, expect } from "@playwright/test";
import { mockApiResponses, mockListResponse } from "./fixtures/mockApi";
import { moveSearchTargetOffscreen } from "./fixtures/mapCamera";
import { activateToolFanAction } from "./fixtures/toolFan";

const ONE_HOUR_MS = 60 * 60 * 1000;

function oneHourFromNowIso() {
  return new Date(Date.now() + ONE_HOUR_MS).toISOString();
}

function directConversation(id: string, unreadCount: number, at: string) {
  return {
    id,
    counterpart_account_id: `counterpart-${id}`,
    counterpart_title: `Person ${id}`,
    created_at: at,
    updated_at: at,
    unread_count: unreadCount,
    last_message_preview: `Nachricht ${id}`,
    last_message_at: at,
    blocked_by_me: false,
    can_send: true,
  };
}

function proposal(
  id: string,
  createdAt: string,
  overrides: Record<string, unknown> = {},
) {
  return {
    id,
    kind: "sachantrag",
    webgemeindezentrum_id: "wgz-test",
    title: `Antrag ${id}`,
    applicant_account_id: `author-${id}`,
    applicant_title: `Autor ${id}`,
    status: "consent",
    created_at: createdAt,
    consent_until: oneHourFromNowIso(),
    veto_count: 0,
    yes_votes: 0,
    no_votes: 0,
    abstain_votes: 0,
    viewer_participation: {
      vote_choice: null,
      has_veto: false,
      may_vote: false,
      may_veto: true,
    },
    ...overrides,
  };
}

test.describe("top-left attention bubbles", () => {
  test("shares one initial attention read across both topbar consumers", async ({
    page,
  }) => {
    await mockApiResponses(page, {
      auth: {
        authenticated: true,
        account_id: "weber-attention",
        role: "weber",
      },
    });
    let directReads = 0;
    let proposalReads = 0;
    await page.route("**/api/direct-conversations", async (route) => {
      directReads += 1;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          items: [directConversation("shared", 1, "2026-08-17T08:00:00Z")],
        }),
      });
    });
    await page.route("**/api/proposals", async (route) => {
      proposalReads += 1;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([]),
      });
    });

    await page.goto("/map");
    await expect(
      page.locator('[data-attention-id="direct:shared"]'),
    ).toBeVisible();
    await expect.poll(() => directReads).toBe(1);
    await expect.poll(() => proposalReads).toBe(1);
  });

  test("a bubble opens a non-modal attention card before leaving the map", async ({
    page,
  }) => {
    await mockApiResponses(page, {
      auth: {
        authenticated: true,
        account_id: "weber-attention",
        role: "weber",
      },
    });
    await page.route("**/api/direct-conversations", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          items: [directConversation("card", 2, "2026-08-17T08:00:00Z")],
        }),
      });
    });
    await page.route("**/api/proposals", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([]),
      });
    });
    await page.goto("/map");

    const bubble = page.locator('[data-attention-id="direct:card"]');
    await expect(bubble).toBeVisible();
    await expect(bubble).toHaveAttribute("data-attention-meaning", "new");
    await bubble.click();

    await expect(page).toHaveURL(/\/map$/);
    await expect(bubble).toHaveAttribute("aria-expanded", "true");
    const card = page.getByTestId("attention-card");
    await expect(card).toBeVisible();
    await expect(card).toHaveAttribute("data-attention-meaning", "new");
    await expect(card.getByText("Neu für dich", { exact: true })).toBeVisible();
    await expect(
      card.getByRole("heading", { name: "Person card" }),
    ).toBeVisible();
    const action = card.getByRole("link", { name: "Nachricht öffnen" });
    await expect(action).toHaveAttribute("href", "/nachrichten?id=card");
    const actionBox = await action.boundingBox();
    expect(actionBox).not.toBeNull();
    expect(actionBox!.height).toBeGreaterThanOrEqual(44);

    await page.keyboard.press("Escape");
    await expect(card).toHaveCount(0);
    await expect(page).toHaveURL(/\/map$/);
  });

  test("the open attention card stays out of the search direction marker area", async ({
    page,
  }) => {
    await mockApiResponses(page, {
      auth: {
        authenticated: true,
        account_id: "weber-attention",
        role: "weber",
      },
    });
    await page.route("**/api/direct-conversations", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          items: [directConversation("geometry", 1, "2026-08-17T08:00:00Z")],
        }),
      });
    });
    await page.route("**/api/proposals", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([]),
      });
    });
    await page.route("**/api/nodes*", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(
          mockListResponse(route.request().url(), [
            {
              id: "attention-search-node",
              title: "Abendliches Stricken",
              summary: "Wir stricken gemeinsam",
              kind: "Treffen",
              location: { lat: 51, lon: 10 },
              modules: [],
              created_at: "2026-08-17T08:00:00Z",
            },
          ]),
        ),
      });
    });
    await page.route("**/api/search**", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          items: [
            {
              id: "attention-search-node",
              title: "Abendliches Stricken",
              summary: "Wir stricken gemeinsam",
              kind: "Treffen",
              item_type: "node",
              location: { lat: 51, lon: 10 },
            },
          ],
          mode: "hybrid",
          generation_id: "attention-search-generation",
          offset: 0,
        }),
      });
    });

    await page.goto("/map");
    await page.waitForFunction(
      () => (window as any).__TEST_MAP__ !== undefined,
      undefined,
      { timeout: 15000 },
    );
    await moveSearchTargetOffscreen(page);
    await activateToolFanAction(page, "find");
    await page.getByRole("combobox", { name: "Suchbegriff" }).fill("Strick");

    await page.locator('[data-attention-id="direct:geometry"]').click();
    const card = page.getByTestId("attention-card");
    await expect(card).toBeVisible();
    await expect(card).toHaveAttribute("data-attention-meaning", "new");
    await expect(card.getByText("Neu für dich", { exact: true })).toBeVisible();
    const direction = page.getByTestId(
      "search-direction-node-attention-search-node",
    );
    await expect(direction).toBeVisible();

    const [cardBox, directionBox] = await Promise.all([
      card.boundingBox(),
      direction.boundingBox(),
    ]);
    expect(cardBox).not.toBeNull();
    expect(directionBox).not.toBeNull();
    expect(directionBox!.y).toBeGreaterThanOrEqual(
      cardBox!.y + cardBox!.height,
    );
  });

  test("mobile keeps higher-semantic attention visible and exposes the rest through +N", async ({
    page,
  }) => {
    await mockApiResponses(page, {
      auth: {
        authenticated: true,
        account_id: "weber-attention",
        role: "weber",
      },
    });
    await page.route("**/api/direct-conversations", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          items: [
            directConversation("newest", 1, "2026-08-17T08:00:00Z"),
            directConversation("second", 2, "2026-08-17T07:00:00Z"),
            directConversation("third", 1, "2026-08-17T06:00:00Z"),
          ],
        }),
      });
    });
    await page.route("**/api/proposals", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([
          proposal("fourth", "2026-08-17T05:00:00Z"),
          proposal("fifth", "2026-08-17T04:00:00Z"),
        ]),
      });
    });
    await page.setViewportSize({ width: 320, height: 568 });
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.goto("/map");

    const attention = page.getByTestId("attention-bubbles");
    await expect(attention).toBeVisible();
    await expect(page.getByTestId("governance-fan-trigger")).toHaveCount(0);

    const bubbles = attention.locator(".attention-bubble");
    await expect(bubbles).toHaveCount(2);
    await expect(bubbles.nth(0)).toHaveAttribute(
      "data-attention-id",
      "direct:newest",
    );
    await expect(bubbles.nth(1)).toHaveAttribute(
      "data-attention-id",
      "direct:second",
    );

    for (let index = 0; index < 2; index += 1) {
      const box = await bubbles.nth(index).boundingBox();
      expect(box).not.toBeNull();
      expect(box!.width).toBeGreaterThanOrEqual(44);
      expect(box!.height).toBeGreaterThanOrEqual(44);
    }

    const overflow = attention.locator(".attention-overflow-trigger");
    await expect(overflow).toHaveText("+3");
    await expect(overflow).toHaveAccessibleName(
      "3 weitere Aufmerksamkeitseinheiten",
    );
    await overflow.click();
    const menu = page.getByTestId("attention-overflow-menu");
    await expect(menu).toBeVisible();
    await expect(menu.locator(".attention-overflow-item")).toHaveCount(3);
    await menu.locator('[data-attention-id="direct:third"]').click();
    await expect(menu).toBeHidden();
    await expect(page).toHaveURL(/\/map$/);
    const hiddenCard = page.getByTestId("attention-card");
    await expect(hiddenCard).toBeVisible();
    await expect(
      hiddenCard.getByRole("link", { name: "Nachricht öffnen" }),
    ).toHaveAttribute("href", "/nachrichten?id=third");

    await page.keyboard.press("Escape");
    await overflow.click();
    await menu.locator('[data-attention-id="proposal:fourth"]').click();
    await expect(
      page
        .getByTestId("attention-card")
        .getByRole("link", { name: "Antrag öffnen" }),
    ).toHaveAttribute("href", "/antraege?id=fourth");

    const [attentionBox, authBox] = await Promise.all([
      attention.boundingBox(),
      page.locator(".auth-slot").boundingBox(),
    ]);
    expect(attentionBox).not.toBeNull();
    expect(authBox).not.toBeNull();
    expect(attentionBox!.x).toBeGreaterThanOrEqual(0);
    expect(attentionBox!.x + attentionBox!.width).toBeLessThanOrEqual(
      authBox!.x,
    );
  });

  test("the bubble row stays inside its topbar track at 390 pixels", async ({
    page,
  }) => {
    await mockApiResponses(page, {
      auth: {
        authenticated: true,
        account_id: "weber-attention",
        role: "weber",
      },
    });
    await page.route("**/api/direct-conversations", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          items: [
            directConversation("newest", 1, "2026-08-17T08:00:00Z"),
            directConversation("second", 1, "2026-08-17T07:00:00Z"),
            directConversation("third", 1, "2026-08-17T06:00:00Z"),
          ],
        }),
      });
    });
    await page.route("**/api/proposals", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([
          proposal("fourth", "2026-08-17T05:00:00Z"),
          proposal("fifth", "2026-08-17T04:00:00Z"),
        ]),
      });
    });
    await page.setViewportSize({ width: 390, height: 720 });
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.goto("/map");

    const attention = page.getByTestId("attention-bubbles");
    const bubbles = attention.locator(".attention-bubble");
    await expect(bubbles).toHaveCount(2);
    await expect(attention.locator(".attention-overflow-trigger")).toHaveText(
      "+3",
    );

    const [attentionBox, authBox] = await Promise.all([
      attention.boundingBox(),
      page.locator(".auth-slot").boundingBox(),
    ]);
    expect(attentionBox).not.toBeNull();
    expect(authBox).not.toBeNull();
    expect(attentionBox!.x).toBeGreaterThanOrEqual(0);
    expect(attentionBox!.x + attentionBox!.width).toBeLessThanOrEqual(
      authBox!.x,
    );
  });

  test("a real deadline stays visible without outranking newer unread information", async ({
    page,
  }) => {
    await mockApiResponses(page, {
      auth: {
        authenticated: true,
        account_id: "weber-attention",
        role: "weber",
      },
    });
    const deadline = oneHourFromNowIso();
    await page.route("**/api/direct-conversations", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          items: [
            directConversation("fresh-message", 1, new Date().toISOString()),
          ],
        }),
      });
    });
    await page.route("**/api/proposals", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([
          proposal("deadline-vote", "2026-08-16T08:00:00Z", {
            status: "voting",
            consent_until: "2026-08-16T08:00:00Z",
            voting_until: deadline,
            remaining_seconds: 3600,
            viewer_participation: {
              vote_choice: null,
              has_veto: false,
              may_vote: true,
              may_veto: false,
            },
          }),
        ]),
      });
    });

    await page.goto("/map");
    const bubbles = page
      .getByTestId("attention-bubbles")
      .locator(".attention-bubble");
    await expect(bubbles).toHaveCount(2);
    await expect(bubbles.nth(0)).toHaveAttribute(
      "data-attention-id",
      "direct:fresh-message",
    );
    await expect(bubbles.nth(1)).toHaveAttribute(
      "data-attention-id",
      "proposal:deadline-vote",
    );
    await bubbles.nth(1).click();
    const card = page.getByTestId("attention-card");
    await expect(card).toContainText("Mitwirkung möglich");
    await expect(card).toContainText("Endet in");
    await expect(card.locator("time")).toHaveAttribute("datetime", deadline);
  });

  test("already handled governance is not projected as attention", async ({
    page,
  }) => {
    await mockApiResponses(page, {
      auth: {
        authenticated: true,
        account_id: "weber-attention",
        role: "weber",
      },
    });
    await page.route("**/api/direct-conversations", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ items: [] }),
      });
    });
    await page.route("**/api/proposals", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([
          proposal("handled", "2026-08-17T07:00:00Z", {
            status: "voting",
            voting_until: oneHourFromNowIso(),
            viewer_participation: {
              vote_choice: "ja",
              has_veto: false,
              may_vote: true,
              may_veto: false,
            },
          }),
        ]),
      });
    });

    await page.goto("/map");
    await expect(page.getByTestId("attention-bubbles")).toHaveCount(0);
  });

  test("own waiting matters collapse into one quiet summary", async ({
    page,
  }) => {
    await mockApiResponses(page, {
      auth: {
        authenticated: true,
        account_id: "weber-attention",
        role: "weber",
      },
    });
    await page.route("**/api/direct-conversations", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ items: [] }),
      });
    });
    await page.route("**/api/proposals", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([
          proposal("own-sach", "2026-08-17T07:00:00Z", {
            applicant_account_id: "weber-attention",
            applicant_title: "Eigener Account",
            viewer_participation: {
              vote_choice: null,
              has_veto: false,
              may_vote: false,
              may_veto: false,
            },
          }),
        ]),
      });
    });

    await page.goto("/map");
    const bubble = page.locator(
      '[data-attention-id="waiting-summary:weber-attention"]',
    );
    await expect(bubble).toHaveAttribute("data-attention-meaning", "waiting");
    await bubble.click();
    const card = page.getByTestId("attention-card");
    await expect(card).toContainText("Läuft ohne dein Zutun");
    await expect(card).toContainText("Du musst gerade nichts tun.");
    await expect(
      card.getByRole("link", { name: "Anträge öffnen" }),
    ).toHaveAttribute("href", "/antraege");
  });

  test("a newly observed item enters at the far left and shifts the older item right", async ({
    page,
  }) => {
    await mockApiResponses(page, {
      auth: {
        authenticated: true,
        account_id: "weber-attention",
        role: "weber",
      },
    });
    let conversations = [
      directConversation("older", 1, "2026-08-17T06:00:00Z"),
    ];
    await page.route("**/api/direct-conversations", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ items: conversations }),
      });
    });
    await page.route("**/api/proposals", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([]),
      });
    });
    await page.goto("/map");

    const bubbles = page
      .getByTestId("attention-bubbles")
      .locator(".attention-bubble");
    await expect(bubbles).toHaveCount(1);
    await expect(bubbles.first()).toHaveAttribute(
      "data-attention-id",
      "direct:older",
    );

    conversations = [
      directConversation("new", 1, "2026-08-17T08:00:00Z"),
      ...conversations,
    ];
    await page.evaluate(() => window.dispatchEvent(new Event("focus")));

    await expect(bubbles).toHaveCount(2);
    await expect(bubbles.nth(0)).toHaveAttribute(
      "data-attention-id",
      "direct:new",
    );
    await expect(bubbles.nth(1)).toHaveAttribute(
      "data-attention-id",
      "direct:older",
    );
  });
});
