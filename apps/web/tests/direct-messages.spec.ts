import { expect, test, type Page, type Route } from "@playwright/test";
import { resolve } from "node:path";
import { mockApiResponses } from "./fixtures/mockApi";

const ALICE_CONVERSATION_ID = "11111111-1111-4111-8111-111111111111";
const BOB_CONVERSATION_ID = "22222222-2222-4222-8222-222222222222";
const CAROL_CONVERSATION_ID = "44444444-4444-4444-8444-444444444444";

const conversations = [
  {
    id: ALICE_CONVERSATION_ID,
    counterpart_account_id: "alice-account",
    counterpart_title: "Alice",
    created_at: "2026-08-09T10:00:00Z",
    updated_at: "2026-08-09T10:02:00Z",
    unread_count: 2,
    last_message_preview: "Nachricht von Alice",
    last_message_at: "2026-08-09T10:02:00Z",
    blocked_by_me: false,
    can_send: true,
  },
  {
    id: BOB_CONVERSATION_ID,
    counterpart_account_id: "bob-account",
    counterpart_title: "Bob",
    created_at: "2026-08-09T10:00:00Z",
    updated_at: "2026-08-09T10:01:00Z",
    unread_count: 1,
    last_message_preview: "Nachricht von Bob",
    last_message_at: "2026-08-09T10:01:00Z",
    blocked_by_me: false,
    can_send: true,
  },
];

const carolConversation = {
  id: CAROL_CONVERSATION_ID,
  counterpart_account_id: "carol-account",
  counterpart_title: "Carol",
  created_at: "2026-08-09T10:04:00Z",
  updated_at: "2026-08-09T10:04:00Z",
  unread_count: 0,
  last_message_preview: null,
  last_message_at: null,
  blocked_by_me: false,
  can_send: true,
};

function message(
  conversationId: string,
  authorAccountId: string,
  authorTitle: string,
  content: string,
) {
  return {
    id: `${conversationId.slice(0, 8)}-3333-4333-8333-333333333333`,
    conversation_id: conversationId,
    author_account_id: authorAccountId,
    author_title: authorTitle,
    content,
    created_at: "2026-08-09T10:03:00Z",
    updated_at: "2026-08-09T10:03:00Z",
    deleted_at: null,
  };
}

async function installStaticBuildFallback(page: Page) {
  if (process.env.PLAYWRIGHT_STATIC_BUILD !== "1") return;

  await page.route("**/nachrichten", (route) =>
    route.fulfill({ path: resolve("build/nachrichten.html") }),
  );
  await page.route("**/_app/immutable/**", (route) => {
    const path = new URL(route.request().url()).pathname.slice(1);
    return route.fulfill({ path: resolve("build", path) });
  });
  await page.route("**/theme-init.js", (route) =>
    route.fulfill({ path: resolve("build/theme-init.js") }),
  );
}

async function installDirectMessagesApi(page: Page) {
  let releaseAliceOpenResponse: (() => void) | undefined;
  const aliceOpenResponseReleased = new Promise<void>((resolve) => {
    releaseAliceOpenResponse = resolve;
  });
  let releaseAliceResponse: (() => void) | undefined;
  const aliceResponseReleased = new Promise<void>((resolve) => {
    releaseAliceResponse = resolve;
  });
  let releaseAliceReadResponse: (() => void) | undefined;
  const aliceReadResponseReleased = new Promise<void>((resolve) => {
    releaseAliceReadResponse = resolve;
  });
  const requestedOpenAccountIds: string[] = [];
  const requestedConversationIds: string[] = [];
  const markedReadConversationIds: string[] = [];

  await page.route("**/api/**", async (route: Route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const method = request.method();

    if (path === "/api/direct-conversations" && method === "GET") {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ items: conversations }),
      });
    }

    if (path === "/api/direct-conversations" && method === "POST") {
      const payload = request.postDataJSON() as {
        recipient_account_id?: string;
      };
      const accountId = payload.recipient_account_id ?? "";
      requestedOpenAccountIds.push(accountId);
      if (accountId === "alice-account" || accountId === "carol-account") {
        await aliceOpenResponseReleased;
      }
      const conversation =
        [...conversations, carolConversation].find(
          (item) => item.counterpart_account_id === accountId,
        ) ?? conversations[0];
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(conversation),
      });
    }

    const messagesMatch = path.match(
      /^\/api\/conversations\/([^/]+)\/messages$/,
    );
    if (messagesMatch && method === "GET") {
      const conversationId = decodeURIComponent(messagesMatch[1]);
      requestedConversationIds.push(conversationId);
      if (conversationId === ALICE_CONVERSATION_ID) {
        await aliceResponseReleased;
      }
      const item =
        conversationId === ALICE_CONVERSATION_ID
          ? message(
              ALICE_CONVERSATION_ID,
              "alice-account",
              "Alice",
              "Verspätete Nachricht von Alice",
            )
          : message(
              BOB_CONVERSATION_ID,
              "bob-account",
              "Bob",
              "Aktuelle Nachricht von Bob",
            );
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          items: [item],
          page: { limit: 50, next_cursor: null, has_more: false },
        }),
      });
    }

    const readMatch = path.match(
      /^\/api\/direct-conversations\/([^/]+)\/read$/,
    );
    if (readMatch && method === "POST") {
      const conversationId = decodeURIComponent(readMatch[1]);
      markedReadConversationIds.push(conversationId);
      if (conversationId === ALICE_CONVERSATION_ID) {
        await aliceReadResponseReleased;
      }
      return route.fulfill({ status: 204 });
    }

    return route.fallback();
  });

  return {
    requestedOpenAccountIds,
    requestedConversationIds,
    markedReadConversationIds,
    releaseAliceOpenResponse: () => releaseAliceOpenResponse?.(),
    releaseAliceResponse: () => releaseAliceResponse?.(),
    releaseAliceReadResponse: () => releaseAliceReadResponse?.(),
  };
}

test("ein verspätetes Öffnen per ?mit=Alice überschreibt eine spätere Bob-Auswahl nicht", async ({
  page,
}) => {
  await installStaticBuildFallback(page);
  await mockApiResponses(page, {
    auth: {
      authenticated: true,
      account_id: "current-account",
      role: "weber",
    },
  });
  const directMessagesApi = await installDirectMessagesApi(page);

  await page.goto("/nachrichten?mit=alice-account");
  await expect
    .poll(() => directMessagesApi.requestedOpenAccountIds)
    .toEqual(["alice-account"]);

  const bobButton = page.getByRole("button", { name: /Bob/ });
  await bobButton.click();
  await expect
    .poll(() => directMessagesApi.requestedConversationIds)
    .toEqual([BOB_CONVERSATION_ID]);
  await expect(page.getByRole("heading", { name: "Bob" })).toBeVisible();
  await expect(page.locator(".conversation .message-list")).toContainText(
    "Aktuelle Nachricht von Bob",
  );

  const delayedAliceOpen = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return (
      url.pathname === "/api/direct-conversations" &&
      response.request().method() === "POST"
    );
  });
  directMessagesApi.releaseAliceOpenResponse();
  await delayedAliceOpen;
  await page.evaluate(
    () =>
      new Promise<void>((resolve) =>
        requestAnimationFrame(() => requestAnimationFrame(() => resolve())),
      ),
  );

  await expect(page.getByRole("heading", { name: "Bob" })).toBeVisible();
  await expect(bobButton).toHaveAttribute("aria-current", "true");
  await expect(page.locator(".conversation .message-list")).toContainText(
    "Aktuelle Nachricht von Bob",
  );
  await expect(page).not.toHaveURL(
    new RegExp(`\\?id=${ALICE_CONVERSATION_ID}`),
  );
});

test("eine verspätete Alice-Antwort überschreibt die ausgewählte Bob-Unterhaltung nicht", async ({
  page,
}) => {
  await installStaticBuildFallback(page);
  await mockApiResponses(page, {
    auth: {
      authenticated: true,
      account_id: "current-account",
      role: "weber",
    },
  });
  const directMessagesApi = await installDirectMessagesApi(page);

  await page.goto("/nachrichten");
  const aliceButton = page.getByRole("button", { name: /Alice/ });
  const bobButton = page.getByRole("button", { name: /Bob/ });
  await aliceButton.click();
  await expect
    .poll(() => directMessagesApi.requestedConversationIds)
    .toEqual([ALICE_CONVERSATION_ID]);

  await bobButton.click();
  try {
    await expect
      .poll(() => directMessagesApi.requestedConversationIds)
      .toEqual([ALICE_CONVERSATION_ID, BOB_CONVERSATION_ID]);
    await expect(page.locator(".conversation .message-list")).toContainText(
      "Aktuelle Nachricht von Bob",
    );
  } catch (cause) {
    directMessagesApi.releaseAliceResponse();
    throw cause;
  }

  const delayedAliceResponse = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return (
      url.pathname === `/api/conversations/${ALICE_CONVERSATION_ID}/messages` &&
      response.request().method() === "GET"
    );
  });
  directMessagesApi.releaseAliceResponse();
  await delayedAliceResponse;
  await page.evaluate(
    () =>
      new Promise<void>((resolve) =>
        requestAnimationFrame(() => requestAnimationFrame(() => resolve())),
      ),
  );

  await expect(page.getByRole("heading", { name: "Bob" })).toBeVisible();
  await expect(bobButton).toHaveAttribute("aria-current", "true");
  await expect(aliceButton).not.toHaveAttribute("aria-current", "true");
  await expect(page.locator(".conversation .message-list")).toContainText(
    "Aktuelle Nachricht von Bob",
  );
  await expect(page.locator(".conversation .message-list")).not.toContainText(
    "Verspätete Nachricht von Alice",
  );
  await expect(
    aliceButton.getByLabel("2 ungelesene Nachrichten"),
  ).toBeVisible();
  await expect(bobButton.getByLabel("1 ungelesene Nachrichten")).toHaveCount(0);
  await expect(page.getByText("Lade Nachrichten…")).toHaveCount(0);
  await expect(page.getByRole("alert")).toHaveCount(0);
  expect(directMessagesApi.markedReadConversationIds).toEqual([
    BOB_CONVERSATION_ID,
  ]);
});

test("eine verspätet geöffnete neue Unterhaltung bleibt in der Seitenleiste", async ({
  page,
}) => {
  await installStaticBuildFallback(page);
  await mockApiResponses(page, {
    auth: {
      authenticated: true,
      account_id: "current-account",
      role: "weber",
    },
  });
  const directMessagesApi = await installDirectMessagesApi(page);

  await page.goto("/nachrichten?mit=carol-account");
  await expect
    .poll(() => directMessagesApi.requestedOpenAccountIds)
    .toEqual(["carol-account"]);

  const bobButton = page.getByRole("button", { name: /Bob/ });
  await bobButton.click();
  await expect
    .poll(() => directMessagesApi.requestedConversationIds)
    .toEqual([BOB_CONVERSATION_ID]);
  await expect(page.getByRole("heading", { name: "Bob" })).toBeVisible();

  const delayedCarolOpen = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return (
      url.pathname === "/api/direct-conversations" &&
      response.request().method() === "POST"
    );
  });
  directMessagesApi.releaseAliceOpenResponse();
  await delayedCarolOpen;

  const carolButton = page.getByRole("button", { name: /Carol/ });
  await expect(carolButton).toBeVisible();
  await expect(bobButton).toHaveAttribute("aria-current", "true");
  await expect(carolButton).not.toHaveAttribute("aria-current", "true");
  await expect(page.getByRole("heading", { name: "Bob" })).toBeVisible();
  await expect(page).not.toHaveURL(
    new RegExp(`\\?id=${CAROL_CONVERSATION_ID}`),
  );
});

test("ein verspätetes Read-Receipt räumt nur den alten Ungelesen-Badge auf", async ({
  page,
}) => {
  await installStaticBuildFallback(page);
  await mockApiResponses(page, {
    auth: {
      authenticated: true,
      account_id: "current-account",
      role: "weber",
    },
  });
  const directMessagesApi = await installDirectMessagesApi(page);

  await page.goto("/nachrichten");
  const aliceButton = page.getByRole("button", { name: /Alice/ });
  const bobButton = page.getByRole("button", { name: /Bob/ });
  await aliceButton.click();
  await expect
    .poll(() => directMessagesApi.requestedConversationIds)
    .toEqual([ALICE_CONVERSATION_ID]);
  directMessagesApi.releaseAliceResponse();
  await expect
    .poll(() => directMessagesApi.markedReadConversationIds)
    .toEqual([ALICE_CONVERSATION_ID]);

  await bobButton.click();
  await expect
    .poll(() => directMessagesApi.markedReadConversationIds)
    .toEqual([ALICE_CONVERSATION_ID, BOB_CONVERSATION_ID]);
  await expect(page.getByRole("heading", { name: "Bob" })).toBeVisible();
  await expect(page.locator(".conversation .message-list")).toContainText(
    "Aktuelle Nachricht von Bob",
  );

  const delayedAliceRead = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return (
      url.pathname ===
        `/api/direct-conversations/${ALICE_CONVERSATION_ID}/read` &&
      response.request().method() === "POST"
    );
  });
  directMessagesApi.releaseAliceReadResponse();
  await delayedAliceRead;

  await expect(aliceButton.getByLabel("2 ungelesene Nachrichten")).toHaveCount(
    0,
  );
  await expect(bobButton).toHaveAttribute("aria-current", "true");
  await expect(page.getByRole("heading", { name: "Bob" })).toBeVisible();
  await expect(page.locator(".conversation .message-list")).toContainText(
    "Aktuelle Nachricht von Bob",
  );
  await expect(page.getByText("Lade Nachrichten…")).toHaveCount(0);
  await expect(page.getByRole("alert")).toHaveCount(0);
});

test("ein verspätetes Öffnen überschreibt keinen neueren Sidebar-Zustand", async ({
  page,
}) => {
  await installStaticBuildFallback(page);
  await mockApiResponses(page, {
    auth: {
      authenticated: true,
      account_id: "current-account",
      role: "weber",
    },
  });
  const directMessagesApi = await installDirectMessagesApi(page);

  await page.goto("/nachrichten?mit=alice-account");
  await expect
    .poll(() => directMessagesApi.requestedOpenAccountIds)
    .toEqual(["alice-account"]);

  const aliceButton = page.getByRole("button", { name: /Alice/ });
  await aliceButton.click();
  await expect
    .poll(() => directMessagesApi.requestedConversationIds)
    .toEqual([ALICE_CONVERSATION_ID]);
  directMessagesApi.releaseAliceResponse();
  await expect
    .poll(() => directMessagesApi.markedReadConversationIds)
    .toEqual([ALICE_CONVERSATION_ID]);

  const aliceReadResponse = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return (
      url.pathname ===
        `/api/direct-conversations/${ALICE_CONVERSATION_ID}/read` &&
      response.request().method() === "POST"
    );
  });
  directMessagesApi.releaseAliceReadResponse();
  await aliceReadResponse;
  await expect(aliceButton.getByLabel("2 ungelesene Nachrichten")).toHaveCount(
    0,
  );

  const delayedAliceOpen = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return (
      url.pathname === "/api/direct-conversations" &&
      response.request().method() === "POST"
    );
  });
  directMessagesApi.releaseAliceOpenResponse();
  await delayedAliceOpen;

  await expect(aliceButton.getByLabel("2 ungelesene Nachrichten")).toHaveCount(
    0,
  );
  await expect(aliceButton).toHaveAttribute("aria-current", "true");
  await expect(page.getByRole("heading", { name: "Alice" })).toBeVisible();
  await expect(page.getByRole("alert")).toHaveCount(0);
});
