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

    // The existing messages page now also loads account-owned system mail.
    // A direct-message regression fixture needs the real empty inbox shape;
    // the generic API mock's bare {} is not a valid PersonalEventPage.
    if (path === "/api/personal-events" && method === "GET") {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ items: [], unread_count: 0, has_more: false }),
      });
    }

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

test("verspätetes Senden behält Bobs Verlauf und Entwurf und aktualisiert Alice", async ({
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
  const api = await installDirectMessagesApi(page);
  let releaseSend!: () => void;
  const sendReleased = new Promise<void>((resolve) => {
    releaseSend = resolve;
  });
  await page.route("**/api/conversations/*/messages", async (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    expect(new URL(route.request().url()).pathname).toBe(
      `/api/conversations/${ALICE_CONVERSATION_ID}/messages`,
    );
    await sendReleased;
    return route.fulfill({
      status: 201,
      json: {
        ...message(
          ALICE_CONVERSATION_ID,
          "current-account",
          "Ich",
          "Nur an Alice",
        ),
        id: "aaaaaaaa-1111-4111-8111-111111111111",
      },
    });
  });

  await page.goto("/nachrichten");
  api.releaseAliceResponse();
  api.releaseAliceReadResponse();
  const alice = page.getByRole("button", { name: /Alice/ });
  const bob = page.getByRole("button", { name: /Bob/ });
  const composer = page.getByRole("textbox", { name: "Nachricht" });
  const history = page.locator(".conversation .message-list");
  await alice.click();
  await expect(history).toContainText("Verspätete Nachricht von Alice");
  await composer.fill("Nur an Alice");
  const sendStarted = page.waitForRequest(
    (request) =>
      request.method() === "POST" &&
      new URL(request.url()).pathname ===
        `/api/conversations/${ALICE_CONVERSATION_ID}/messages`,
  );
  await page.getByRole("button", { name: "Senden", exact: true }).click();
  await sendStarted;
  try {
    await bob.click();
    await expect(history).toContainText("Aktuelle Nachricht von Bob");
    await expect(composer).toHaveValue("");
    await composer.fill("Entwurf für Bob");
    releaseSend();
    await expect(alice).toContainText("Nur an Alice");
    await expect(bob).not.toContainText("Nur an Alice");
    await expect(history).not.toContainText("Nur an Alice");
    await expect(composer).toHaveValue("Entwurf für Bob");
    await alice.click();
    await expect(composer).toHaveValue("");
    await bob.click();
    await expect(composer).toHaveValue("Entwurf für Bob");
  } finally {
    releaseSend();
  }
});

test("verspätetes Blockieren ändert ausschließlich das ursprüngliche Gespräch", async ({
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
  const api = await installDirectMessagesApi(page);
  let releaseBlock!: () => void;
  const blockReleased = new Promise<void>((resolve) => {
    releaseBlock = resolve;
  });
  await page.route("**/api/direct-conversations/*/block", async (route) => {
    await blockReleased;
    return route.fulfill({
      status: 200,
      json: { ...conversations[0], blocked_by_me: true, can_send: false },
    });
  });
  await page.goto("/nachrichten");
  api.releaseAliceResponse();
  api.releaseAliceReadResponse();
  const alice = page.getByRole("button", { name: /Alice/ });
  const bob = page.getByRole("button", { name: /Bob/ });
  await alice.click();
  await expect(page.getByRole("heading", { name: "Alice" })).toBeVisible();
  const blockStarted = page.waitForRequest(
    (request) =>
      request.method() === "PUT" &&
      new URL(request.url()).pathname ===
        `/api/direct-conversations/${ALICE_CONVERSATION_ID}/block`,
  );
  await page.getByRole("button", { name: "Blockieren", exact: true }).click();
  await blockStarted;
  try {
    await bob.click();
    await expect(page.locator(".conversation .message-list")).toContainText(
      "Aktuelle Nachricht von Bob",
    );
    const blockFinished = page.waitForResponse(
      (response) =>
        response.request().method() === "PUT" &&
        new URL(response.url()).pathname.endsWith("/block"),
    );
    releaseBlock();
    await blockFinished;
    await page.evaluate(
      () =>
        new Promise<void>((resolve) =>
          requestAnimationFrame(() => requestAnimationFrame(() => resolve())),
        ),
    );
    await expect(page.getByRole("heading", { name: "Bob" })).toBeVisible();
    await expect(
      page.getByRole("textbox", { name: "Nachricht" }),
    ).toBeVisible();
    await alice.click();
    await expect(page.getByRole("button", { name: "Freigeben" })).toBeVisible();
  } finally {
    releaseBlock();
  }
});

const ALICE_OLDER_CURSOR = "alice-before-50+opaque/=";
const BOB_OLDER_CURSOR = "bob-before-first";

function numberedHistoryMessage(
  conversationId: string,
  ordinal: number,
  content: string,
  createdAt: string,
) {
  const alice = conversationId === ALICE_CONVERSATION_ID;
  return {
    ...message(
      conversationId,
      alice ? "alice-account" : "bob-account",
      alice ? "Alice" : "Bob",
      content,
    ),
    id: `${conversationId.slice(0, 8)}-3333-4333-8333-${String(ordinal).padStart(12, "0")}`,
    created_at: createdAt,
    updated_at: createdAt,
  };
}

async function installPagedHistoryApi(page: Page, deferAliceOlder = false) {
  await installStaticBuildFallback(page);
  await mockApiResponses(page, {
    auth: { authenticated: true, account_id: "current-account", role: "weber" },
  });
  const api = await installDirectMessagesApi(page);
  api.releaseAliceResponse();
  api.releaseAliceReadResponse();

  const recentAlice = Array.from({ length: 50 }, (_, index) =>
    numberedHistoryMessage(
      ALICE_CONVERSATION_ID,
      index + 3,
      `Alice Nachricht ${index + 3}`,
      new Date(Date.UTC(2026, 7, 9, 10, 3, index + 2)).toISOString(),
    ),
  );
  const olderAlice = [
    // A boundary overlap must not duplicate or overwrite an already loaded message.
    { ...recentAlice[0], content: "Veraltete Überlappung" },
    numberedHistoryMessage(
      ALICE_CONVERSATION_ID,
      2,
      "Alice Nachricht 2",
      "2026-08-09T10:03:00.1Z",
    ),
    numberedHistoryMessage(
      ALICE_CONVERSATION_ID,
      1,
      "Alice Nachricht 1",
      "2026-08-09T10:03:00Z",
    ),
  ];
  const requests: Array<{ id: string; cursor: string | null }> = [];
  let releaseAliceOlder!: () => void;
  let settleAliceOlder!: () => void;
  const aliceOlderReleased = new Promise<void>((resolve) => {
    releaseAliceOlder = resolve;
  });
  const aliceOlderSettled = new Promise<void>((resolve) => {
    settleAliceOlder = resolve;
  });

  await page.route(
    /\/api\/conversations\/[^/]+\/messages(?:\?.*)?$/,
    async (route) => {
      const request = route.request();
      if (request.method() !== "GET") return route.fallback();
      const url = new URL(request.url());
      const id = decodeURIComponent(url.pathname.split("/")[3]);
      const cursor = url.searchParams.get("cursor");
      requests.push({ id, cursor });

      let items;
      let nextCursor: string | null = null;
      if (id === ALICE_CONVERSATION_ID && cursor === null) {
        items = [...recentAlice].reverse();
        nextCursor = ALICE_OLDER_CURSOR;
      } else if (
        id === ALICE_CONVERSATION_ID &&
        cursor === ALICE_OLDER_CURSOR
      ) {
        if (deferAliceOlder) await aliceOlderReleased;
        items = olderAlice;
      } else if (id === BOB_CONVERSATION_ID && cursor === null) {
        items = [
          numberedHistoryMessage(
            BOB_CONVERSATION_ID,
            2,
            "Bob aktuelle Nachricht",
            "2026-08-09T10:03:01Z",
          ),
        ];
        nextCursor = BOB_OLDER_CURSOR;
      } else if (id === BOB_CONVERSATION_ID && cursor === BOB_OLDER_CURSOR) {
        items = [
          numberedHistoryMessage(
            BOB_CONVERSATION_ID,
            1,
            "Bob ältere Nachricht",
            "2026-08-09T10:03:00Z",
          ),
        ];
      } else {
        return route.fulfill({
          status: 400,
          contentType: "application/json",
          body: JSON.stringify({ code: "invalid_cursor" }),
        });
      }

      try {
        await route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            items,
            page: {
              limit: 50,
              next_cursor: nextCursor,
              has_more: nextCursor !== null,
            },
          }),
        });
      } catch (error) {
        // An aborted Alice request after switching is also a valid transport outcome.
        if (
          !(
            deferAliceOlder &&
            id === ALICE_CONVERSATION_ID &&
            cursor === ALICE_OLDER_CURSOR
          )
        )
          throw error;
      } finally {
        if (id === ALICE_CONVERSATION_ID && cursor === ALICE_OLDER_CURSOR)
          settleAliceOlder();
      }
    },
  );
  return { requests, api, releaseAliceOlder, aliceOlderSettled };
}

test("ältere Direktnachrichten werden über den Cursor vollständig und ohne Duplikate geladen", async ({
  page,
}) => {
  const history = await installPagedHistoryApi(page);
  await page.goto("/nachrichten");
  await page.getByRole("button", { name: /Alice/ }).click();
  const list = page.locator(".conversation .message-list");
  const rows = list.locator("article.message");
  await expect(rows).toHaveCount(50);
  await expect(rows.last()).toContainText("Alice Nachricht 52");
  await list.evaluate((element) => {
    element.scrollTop = 0;
  });

  const original = list.getByText("Alice Nachricht 3", { exact: true });
  const older = page.getByRole("button", { name: "Ältere Nachrichten laden" });
  await expect(older).toBeVisible();
  const originalTop = await original.evaluate(
    (element) => element.getBoundingClientRect().top,
  );
  await older.click();

  await expect(rows).toHaveCount(52);
  await expect(rows.nth(0)).toContainText("Alice Nachricht 1");
  await expect(rows.nth(1)).toContainText("Alice Nachricht 2");
  await expect(rows.nth(2)).toContainText("Alice Nachricht 3");
  await expect(rows.last()).toContainText("Alice Nachricht 52");
  await expect(original).toHaveCount(1);
  await expect(list).not.toContainText("Veraltete Überlappung");
  await expect(older).toHaveCount(0);
  expect(history.requests).toEqual([
    { id: ALICE_CONVERSATION_ID, cursor: null },
    { id: ALICE_CONVERSATION_ID, cursor: ALICE_OLDER_CURSOR },
  ]);
  expect(history.api.markedReadConversationIds).toEqual([
    ALICE_CONVERSATION_ID,
  ]);
  await expect
    .poll(async () =>
      Math.abs(
        (await original.evaluate(
          (element) => element.getBoundingClientRect().top,
        )) - originalTop,
      ),
    )
    .toBeLessThanOrEqual(3);
});

test("eine verspätete ältere Seite bleibt beim Wechsel auf Bob Alice zugeordnet", async ({
  page,
}) => {
  const history = await installPagedHistoryApi(page, true);
  await page.goto("/nachrichten");
  await page.getByRole("button", { name: /Alice/ }).click();
  const list = page.locator(".conversation .message-list");
  await expect(list.locator("article.message")).toHaveCount(50);

  try {
    await page
      .getByRole("button", { name: "Ältere Nachrichten laden" })
      .click();
    await expect
      .poll(() => history.requests)
      .toContainEqual({
        id: ALICE_CONVERSATION_ID,
        cursor: ALICE_OLDER_CURSOR,
      });
    await page.getByRole("button", { name: /Bob/ }).click();
    await expect(page.getByRole("heading", { name: "Bob" })).toBeVisible();
    await expect(list).toContainText("Bob aktuelle Nachricht");
    await expect(
      page.getByRole("button", { name: "Ältere Nachrichten laden" }),
    ).toBeEnabled();
  } finally {
    history.releaseAliceOlder();
  }
  await history.aliceOlderSettled;
  await expect(list).not.toContainText("Alice Nachricht");
  await page.getByRole("button", { name: "Ältere Nachrichten laden" }).click();
  await expect(list.locator("article.message")).toHaveCount(2);
  await expect(list.locator("article.message").first()).toContainText(
    "Bob ältere Nachricht",
  );
  await expect(list.locator("article.message").last()).toContainText(
    "Bob aktuelle Nachricht",
  );
  await expect(list).not.toContainText("Alice Nachricht");
  expect(history.requests).toEqual([
    { id: ALICE_CONVERSATION_ID, cursor: null },
    { id: ALICE_CONVERSATION_ID, cursor: ALICE_OLDER_CURSOR },
    { id: BOB_CONVERSATION_ID, cursor: null },
    { id: BOB_CONVERSATION_ID, cursor: BOB_OLDER_CURSOR },
  ]);
});

test("Sekundenbruchteile bestimmen die tatsächliche Reihenfolge der Postfachvorschau", async ({
  page,
}) => {
  await installStaticBuildFallback(page);
  await mockApiResponses(page, {
    auth: { authenticated: true, account_id: "current-account", role: "weber" },
  });
  const api = await installDirectMessagesApi(page);
  const bob = {
    ...conversations[1],
    updated_at: "2026-08-09T10:02:00.100Z",
    last_message_at: "2026-08-09T10:02:00.100Z",
  };
  await page.route("**/api/direct-conversations", (route) => {
    if (route.request().method() !== "GET") return route.fallback();
    return route.fulfill({ json: { items: [bob, conversations[0]] } });
  });
  await page.route("**/api/conversations/*/messages", (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    return route.fulfill({
      status: 201,
      json: {
        ...message(
          ALICE_CONVERSATION_ID,
          "current-account",
          "Ich",
          "Neuester Sekundenbruchteil",
        ),
        id: "aaaaaaaa-1111-4111-8111-111111111111",
        created_at: "2026-08-09T10:02:00.123456Z",
        updated_at: "2026-08-09T10:02:00.123456Z",
      },
    });
  });
  await page.goto("/nachrichten");
  api.releaseAliceResponse();
  api.releaseAliceReadResponse();
  const alice = page.getByRole("button", { name: /Alice/ });
  await alice.click();
  await expect(page.locator(".conversation .message-list")).toContainText(
    "Verspätete Nachricht von Alice",
  );
  await page
    .getByRole("textbox", { name: "Nachricht" })
    .fill("Neuester Sekundenbruchteil");
  await page.getByRole("button", { name: "Senden", exact: true }).click();
  await expect(alice).toContainText("Neuester Sekundenbruchteil");
  await expect(page.locator(".conversation-list button").first()).toContainText(
    "Alice",
  );
});

test("Kontowechsel verwirft private Direktnachrichten und alte ausstehende Antworten", async ({
  page,
}) => {
  let accountId = "current-account";
  await installStaticBuildFallback(page);
  await mockApiResponses(page, {
    auth: { authenticated: true, account_id: accountId, role: "weber" },
  });
  const api = await installDirectMessagesApi(page);
  await page.route("**/api/auth/me", (route) =>
    route.fulfill({
      json: { authenticated: true, account_id: accountId, role: "weber" },
    }),
  );
  await page.route("**/api/direct-conversations", (route) => {
    if (route.request().method() !== "GET") return route.fallback();
    return route.fulfill({
      json: {
        items: [
          accountId === "current-account" ? conversations[0] : conversations[1],
        ],
      },
    });
  });
  let releaseSend!: () => void;
  const sendReleased = new Promise<void>((resolve) => {
    releaseSend = resolve;
  });
  await page.route("**/api/conversations/*/messages", async (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    await sendReleased;
    return route.fulfill({
      status: 201,
      json: {
        ...message(
          ALICE_CONVERSATION_ID,
          "current-account",
          "Ich",
          "Private alte Antwort",
        ),
        id: "aaaaaaaa-2222-4222-8222-222222222222",
      },
    });
  });
  const recheck = async () => {
    await page.evaluate(async () => {
      const check = (
        window as Window & {
          __commonthingE2ERecheckAuth?: () => Promise<unknown>;
        }
      ).__commonthingE2ERecheckAuth;
      if (!check) throw new Error("Auth recheck test seam unavailable");
      await check();
    });
  };
  await page.goto("/nachrichten");
  api.releaseAliceResponse();
  api.releaseAliceReadResponse();
  const alice = page.getByRole("button", { name: /Alice/ });
  const bob = page.getByRole("button", { name: /Bob/ });
  const history = page.locator(".conversation .message-list");
  const composer = page.getByRole("textbox", { name: "Nachricht" });
  await alice.click();
  await expect(history).toContainText("Verspätete Nachricht von Alice");
  await composer.fill("Privater alter Entwurf");
  await recheck();
  await expect(composer).toHaveValue("Privater alter Entwurf");
  const sendStarted = page.waitForRequest(
    (request) =>
      request.method() === "POST" &&
      new URL(request.url()).pathname ===
        "/api/conversations/" + ALICE_CONVERSATION_ID + "/messages",
  );
  await page.getByRole("button", { name: "Senden", exact: true }).click();
  await sendStarted;
  try {
    accountId = "other-account";
    await recheck();
    await expect(alice).toHaveCount(0);
    await expect(bob).toBeVisible();
    await expect(page.getByText("Verspätete Nachricht von Alice")).toHaveCount(
      0,
    );
    await bob.click();
    await expect(history).toContainText("Aktuelle Nachricht von Bob");
    await composer.fill("Unabhängiger neuer Entwurf");
    const completed = page.waitForResponse(
      (response) =>
        response.request().method() === "POST" &&
        new URL(response.url()).pathname ===
          "/api/conversations/" + ALICE_CONVERSATION_ID + "/messages",
    );
    releaseSend();
    await completed;
    await page.evaluate(
      () =>
        new Promise<void>((resolve) =>
          requestAnimationFrame(() => requestAnimationFrame(() => resolve())),
        ),
    );
    await expect(composer).toHaveValue("Unabhängiger neuer Entwurf");
    await expect(history).not.toContainText("Private alte Antwort");
    await expect(alice).toHaveCount(0);
    accountId = "current-account";
    await recheck();
    await alice.click();
    await expect(composer).toHaveValue("");
    await expect(history).not.toContainText("Private alte Antwort");
  } finally {
    releaseSend();
  }
});
