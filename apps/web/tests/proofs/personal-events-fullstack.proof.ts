import { randomUUID } from "node:crypto";
import { execFileSync } from "node:child_process";
import { expect, test, type BrowserContext } from "@playwright/test";

const DATABASE_URL = process.env.MAP_CARDINALITY_FULLSTACK_DATABASE_URL ?? "";
const API_ORIGIN = process.env.MAP_CARDINALITY_FULLSTACK_API_BASE ?? "";
const WEB_ORIGIN = "http://127.0.0.1:" + (process.env.PORT ?? "5173");
const PROOF_DB = "commonthing_map_cardinality_fullstack";
const PROOF_MARKER = "commonthing-map-cardinality-fullstack-v1";

function requireDisposableEnvironment(): void {
  const db = new URL(DATABASE_URL);
  if (
    !["postgres:", "postgresql:"].includes(db.protocol) ||
    db.hostname !== "127.0.0.1" ||
    db.port !== "5432" ||
    db.pathname !== "/" + PROOF_DB ||
    db.search !== "" ||
    db.hash !== ""
  ) {
    throw new Error("P3a proof refuses a non-disposable PostgreSQL target");
  }
  if (API_ORIGIN !== "http://127.0.0.1:8787") {
    throw new Error("P3a proof requires the existing loopback-only API");
  }
  if (!/^http:\/\/127\.0\.0\.1:\d+$/.test(WEB_ORIGIN)) {
    throw new Error("P3a proof requires loopback-only browser origin");
  }
  if (
    psql(
      "SELECT marker FROM map_cardinality_fullstack_proof_guard WHERE id = 1;",
    ) !== PROOF_MARKER
  ) {
    throw new Error("P3a proof database guard is missing");
  }
}

function psql(statement: string): string {
  return execFileSync(
    "psql",
    [
      "--no-psqlrc",
      "--dbname=" + DATABASE_URL,
      "--set=ON_ERROR_STOP=1",
      "--tuples-only",
      "--no-align",
      "--command",
      statement,
    ],
    { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] },
  ).trim();
}

function account(prefix: "a" | "b" | "c" | "g"): string {
  return "p3a-browser-" + prefix + "-" + randomUUID().replace(/-/g, "");
}

function sqlAccount(id: string): string {
  if (!/^p3a-browser-[abcg]-[0-9a-f]{32}$/.test(id)) {
    throw new Error("Invalid synthetic account identity");
  }
  return "'" + id + "'";
}

function api(path: string): string {
  return WEB_ORIGIN + "/api" + path;
}

async function login(context: BrowserContext, id: string): Promise<void> {
  const response = await context.request.post(api("/auth/dev/login"), {
    data: { account_id: id },
    headers: { origin: WEB_ORIGIN },
  });
  expect(response.status(), "local-only real HTTP dev login").toBe(200);
  const cookies = await context.cookies(WEB_ORIGIN);
  expect(cookies.some((cookie) => cookie.name === "gewebe_session")).toBe(true);
  const own = await context.request.get(api("/auth/me"));
  expect(own.status()).toBe(200);
  expect((await own.json()).account_id).toBe(id);
}

async function personalEvents(context: BrowserContext): Promise<{
  unread_count: number;
  items: Array<{
    id: string;
    event_kind: string;
    content: string;
    read_at: string | null;
    actionable: boolean;
    circle_id: string | null;
    circle_name: string | null;
  }>;
}> {
  const response = await context.request.get(api("/personal-events"));
  expect(response.status()).toBe(200);
  return response.json();
}

test("real PostgreSQL and Chromium: personal invitations are private, actionable and idempotent", async ({
  browser,
}) => {
  test.setTimeout(180_000);
  requireDisposableEnvironment();
  const exactCommit = process.env.WELTGEWEBE_EXACT_REVISION;
  expect(exactCommit).toMatch(/^[0-9a-f]{40}$/);
  const version = await fetch(API_ORIGIN + "/version");
  expect(version.status).toBe(200);
  expect((await version.json()).commit).toBe(exactCommit);

  const a = account("a");
  const b = account("b");
  const c = account("c");
  const guest = account("g");
  const accounts = [a, b, c, guest];
  const names = accounts.map(sqlAccount);
  const houseName = "Browser-Proof-Haus-" + randomUUID().slice(0, 8);
  let circleId: string | null = null;
  const contexts: BrowserContext[] = [];

  // Test-only records are confined to the existing guarded disposable DB.
  // No API handler, session cookie or permission is mocked in this proof.
  psql(
    [
      "BEGIN;",
      ...accounts.map(
        (id, i) =>
          "INSERT INTO domain_accounts(id,title,kind,role,map_state,location_lat,location_lon) " +
          "VALUES (" +
          sqlAccount(id) +
          "," +
          sqlAccount(id) +
          ",'garnrolle','" +
          (i === 3 ? "gast" : "weber") +
          "','not_on_map',53.555,10.055);",
      ),
      "COMMIT;",
    ].join(" "),
  );

  try {
    for (let i = 0; i < accounts.length; i += 1) {
      const context = await browser.newContext({
        baseURL: WEB_ORIGIN,
        viewport:
          i === 2 ? { width: 390, height: 844 } : { width: 820, height: 1050 },
        serviceWorkers: "block",
      });
      contexts.push(context);
      await login(context, accounts[i]);
    }
    const [aContext, bContext, cContext, guestContext] = contexts;

    // A creates a real pending household invitation via HTTP. B's browser
    // renders it without Playwright route mocking or manufactured messages.
    const create = await aContext.request.post(api("/circles"), {
      data: {
        kind: "household",
        name: houseName,
        invitee_account_id: b,
      },
      headers: { origin: WEB_ORIGIN },
    });
    expect(create.status()).toBe(201);
    circleId = (await create.json()).id;
    expect(circleId).toMatch(/^[0-9a-f-]{36}$/);

    const bPage = await bContext.newPage();
    await bPage.goto("/nachrichten");
    const bInbox = bPage.getByTestId("personal-events-inbox");
    await expect(bInbox.getByText(houseName, { exact: true })).toBeVisible();
    await expect(
      bInbox.getByRole("button", { name: "Annehmen" }),
    ).toBeVisible();
    const initialB = await personalEvents(bContext);
    expect(initialB.unread_count).toBe(1);
    expect(
      initialB.items.filter((e) => e.event_kind === "circle_invited"),
    ).toHaveLength(1);
    for (const isolated of [cContext, guestContext]) {
      const page = await personalEvents(isolated);
      expect(page.items).toHaveLength(0);
      expect(page.unread_count).toBe(0);
    }

    await bInbox.getByRole("button", { name: "Annehmen" }).click();
    await expect(bInbox.getByRole("button", { name: "Annehmen" })).toHaveCount(
      0,
    );
    await expect
      .poll(async () => (await personalEvents(bContext)).unread_count)
      .toBe(0);
    const aResult = await personalEvents(aContext);
    expect(
      aResult.items.filter((e) => e.event_kind === "circle_joined"),
    ).toHaveLength(1);
    expect(
      aResult.items.filter((e) => e.event_kind === "circle_invitation_sent"),
    ).toHaveLength(1);

    const aPage = await aContext.newPage();
    await aPage.goto("/nachrichten");
    const aInbox = aPage.getByTestId("personal-events-inbox");
    const joined = aResult.items.find((e) => e.event_kind === "circle_joined");
    expect(joined).toBeDefined();
    await expect(
      aInbox.getByText(joined!.content, { exact: true }),
    ).toBeVisible();

    // The same invite is sent twice. Only the real INSERT changes the
    // membership and creates one persistently unread notice for C.
    for (let i = 0; i < 2; i += 1) {
      const invite = await aContext.request.post(
        api("/circles/" + circleId + "/invitations"),
        { data: { account_id: c }, headers: { origin: WEB_ORIGIN } },
      );
      expect(invite.status()).toBe(204);
    }
    const cBefore = await personalEvents(cContext);
    expect(cBefore.unread_count).toBe(1);
    expect(
      cBefore.items.filter((e) => e.event_kind === "circle_invited"),
    ).toHaveLength(1);

    const cPage = await cContext.newPage();
    await cPage.goto("/nachrichten");
    const cInbox = cPage.getByTestId("personal-events-inbox");
    await expect(cInbox.getByText(houseName, { exact: true })).toBeVisible();
    await cInbox.getByRole("button", { name: "Ablehnen" }).click();
    await expect(cInbox.getByRole("button", { name: "Ablehnen" })).toHaveCount(
      0,
    );
    const cAfter = await personalEvents(cContext);
    expect(cAfter.unread_count).toBe(0);
    expect(cAfter.items[0].actionable).toBe(false);
    expect(cAfter.items[0].circle_id).toBeNull();
    expect(cAfter.items[0].circle_name).toBeNull();
    const aAfter = await personalEvents(aContext);
    expect(
      aAfter.items.filter((e) => e.event_kind === "circle_declined"),
    ).toHaveLength(1);

    await aInbox.getByRole("button", { name: "Aktualisieren" }).click();
    const declined = aAfter.items.find(
      (e) => e.event_kind === "circle_declined",
    );
    expect(declined).toBeDefined();
    await expect(
      aInbox.getByText(declined!.content, { exact: true }),
    ).toBeVisible();

    const guestPage = await guestContext.newPage();
    await guestPage.goto("/nachrichten");
    const guestInbox = guestPage.getByTestId("personal-events-inbox");
    await expect(
      guestInbox.getByText("Keine persönlichen Mitteilungen."),
    ).toBeVisible();
    await expect(guestInbox.getByText(houseName)).toHaveCount(0);
    for (const context of [aContext, bContext, cContext]) {
      const direct = await context.request.get(api("/direct-conversations"));
      expect(direct.status()).toBe(200);
      expect(JSON.stringify(await direct.json())).not.toContain(houseName);
    }
  } finally {
    await Promise.all(contexts.map((context) => context.close()));
    // Only exact test identities are removed. The runner's disposable DB
    // is discarded after the job, even when an earlier assertion fails.
    if (circleId) {
      psql(
        "DELETE FROM domain_audience_circles WHERE id='" +
          circleId.replace(/[^0-9a-f-]/g, "") +
          "' AND created_by_account_id=" +
          sqlAccount(a) +
          ";",
      );
    }
    psql("DELETE FROM domain_accounts WHERE id IN (" + names.join(",") + ");");
    const remaining = psql(
      "SELECT count(*) FROM domain_accounts WHERE id IN (" +
        names.join(",") +
        ");",
    );
    expect(remaining).toBe("0");
  }
});
