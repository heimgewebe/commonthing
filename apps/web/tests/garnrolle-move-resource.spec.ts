import {
  expect,
  test,
  type Locator,
  type Page,
  type Request,
} from "@playwright/test";
import { mockApiResponses } from "./fixtures/mockApi";

const ACCOUNT_ID = "00000000-0000-0000-0000-000000000003";
const OTHER_ACCOUNT_ID = "00000000-0000-0000-0000-000000000004";
const CIRCLE_ID = "00000000-0000-4000-8000-000000000099";
const SAVED_TAGS = [
  "account",
  "garnrolle",
  "good:Leiter",
  "skill:Fahrrad reparieren",
  "interest:Commons",
];

type ServerState = {
  tags: string[];
  moveStatuses: number[];
  patchStatuses: number[];
  moves: Record<string, unknown>[];
  patches: Record<string, unknown>[];
  /** Account of every /api/circles/me request (other panels use it too). */
  circleRequests: string[];
  /** Account the mocked session currently belongs to. */
  accountId: string;
  /** Circle responses per request, in order; default: one active circle. */
  circleStatuses: number[];
  /** Holds back circle responses until released (stale-request tests). */
  circleGate?: Promise<void>;
  /** Statuses for the next profile GETs, in order (default: 200). */
  getStatuses: number[];
  /** The lazy editor chunk cannot be loaded at all. */
  breakChunks: boolean;
  /** The lazy editor chunk arrives only after this gate opens. */
  chunkGate?: Promise<void>;
};

const sameSet = (a: unknown, b: string[]) =>
  Array.isArray(a) &&
  a.length === b.length &&
  b.every((tag) => a.includes(tag));

type OpenOptions = {
  /** Draft in sessionStorage before the page loads. */
  draft?: Record<string, unknown>;
  /** Anzeigename expected once the profile is loaded. */
  title?: string;
  /** Return right after navigation (the load is held back on purpose). */
  skipWait?: boolean;
  /** Güter expected once the profile is loaded (default: "Leiter"). */
  goods?: string | RegExp;
};

const circlesOf = (accountId: string) => [
  {
    id: accountId === ACCOUNT_ID ? CIRCLE_ID : "circle-of-other-account",
    kind: "household",
    name: accountId === ACCOUNT_ID ? "Test-Haus" : "Haus des Anderen",
    state: "active",
    membership_status: "active",
  },
];

/**
 * Stateful profile/resource mock on top of the shared API fixture. The real
 * atomicity and privacy proof lives in the PostgreSQL API tests; this only
 * proves the browser sends the right single-tag request and keeps drafts.
 */
async function openSettings(
  page: Page,
  state: ServerState,
  options: OpenOptions = {},
) {
  if (options.draft) {
    await page.addInitScript(
      ({ accountId, draft }) =>
        sessionStorage.setItem(
          `weltgewebe:garnrolle-draft:${accountId}`,
          JSON.stringify(draft),
        ),
      { accountId: ACCOUNT_ID, draft: options.draft },
    );
  }
  // The lazy module is the only chunk that carries the review test id.
  await page.route("**/_app/immutable/**/*.js", async (route) => {
    if (!state.breakChunks && !state.chunkGate) return route.continue();
    const response = await route.fetch();
    if ((await response.text()).includes("garnrolle-tag-review")) {
      if (state.breakChunks) return route.abort();
      await state.chunkGate;
    }
    return route.fulfill({ response });
  });
  await mockApiResponses(page, {
    auth: { authenticated: true, account_id: ACCOUNT_ID, role: "weber" },
  });
  await page.route("**/api/auth/me", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        authenticated: true,
        account_id: state.accountId,
        role: "weber",
      }),
    }),
  );
  await page.route("**/api/circles/me*", async (route) => {
    // The account is fixed when the request is sent, like the real cookie.
    const requestedFor = state.accountId;
    state.circleRequests.push(requestedFor);
    await state.circleGate;
    const status = state.circleStatuses.shift() ?? 200;
    if (status !== 200) return route.fulfill({ status });
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(circlesOf(requestedFor)),
    });
  });
  const profile = () => ({
    id: state.accountId,
    title: "Meine Garnrolle",
    summary: "Gemeinsame Dinge",
    tags: state.tags,
    map_state: "not_on_map",
    radius_m: 0,
  });
  await page.route("**/api/accounts/me/profile", async (route) => {
    const method = route.request().method();
    if (method === "PATCH") {
      const payload = route.request().postDataJSON() as Record<string, unknown>;
      state.patches.push(payload);
      const status = state.patchStatuses.shift() ?? 200;
      if (status !== 200) return route.fulfill({ status });
      // Like the API: a precondition that is not the stored set conflicts.
      if (!sameSet(payload.expected_tags, state.tags)) {
        return route.fulfill({ status: 409 });
      }
      state.tags = payload.tags as string[];
    } else {
      const status = state.getStatuses.shift() ?? 200;
      if (status !== 200) return route.fulfill({ status });
    }
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(profile()),
    });
  });
  await page.route("**/api/accounts/me/profile/resources", async (route) => {
    const payload = route.request().postDataJSON() as Record<string, unknown>;
    state.moves.push(payload);
    const status = state.moveStatuses.shift() ?? 201;
    if (status !== 201) return route.fulfill({ status });
    state.tags = state.tags.filter((tag) => tag !== payload.tag);
    const title = String(payload.tag).replace(/^(good|skill):/, "");
    return route.fulfill({
      status: 201,
      contentType: "application/json",
      headers: { "Cache-Control": "private, no-store" },
      body: JSON.stringify({
        node: {
          id: "moved-node-1",
          kind: "resource",
          title,
          created_at: "2026-11-08T12:00:00Z",
          updated_at: "2026-11-08T12:00:00Z",
          search_visibility: payload.scope === "public" ? "public" : "private",
          tags: [payload.tag],
          location: null,
        },
        audience: {
          node_id: "moved-node-1",
          scope: payload.scope,
          circle_ids: payload.circle_ids,
        },
        profile: profile(),
      }),
    });
  });
  await page.goto("/settings#meine-garnrolle");
  const section = page.getByTestId("my-garnrolle-section");
  if (options.skipWait) return section;
  await expect(section.getByLabel("Anzeigename")).toHaveValue(
    options.title ?? "Meine Garnrolle",
  );
  if (!options.draft && state.tags.includes("good:Leiter")) {
    await expect(section.getByLabel("Güter")).toHaveValue(
      options.goods ?? "Leiter",
    );
  }
  return section;
}

/** A draft written before drafts recorded the tags they were based on. */
const OLD_DRAFT = {
  displayName: "Entwurf-Name",
  summary: "Entwurf-Beschreibung",
  skills: "Fahrrad reparieren",
  goods: "Leiter, Werkzeug",
  interests: "Commons",
};

function newState(): ServerState {
  return {
    tags: [...SAVED_TAGS],
    moveStatuses: [],
    patchStatuses: [],
    moves: [],
    patches: [],
    circleRequests: [],
    accountId: ACCOUNT_ID,
    circleStatuses: [],
    getStatuses: [],
    breakChunks: false,
  };
}

/** Holds back the lazy editor chunk; returns the release. */
function holdLazyChunk(state: ServerState): () => void {
  let release!: () => void;
  state.chunkGate = new Promise<void>((resolve) => {
    release = resolve;
  });
  return release;
}

/** The editor is lazy: it only exists after this explicit click. */
async function openEditor(section: Locator) {
  await section.getByTestId("garnrolle-move-open").click();
  const move = section.getByTestId("garnrolle-move-resource");
  await expect(move).toBeVisible();
  return move;
}

test.describe("Gespeicherten Profil-Tag als eigene Ressource", () => {
  test("moves exactly one saved tag and keeps unsaved drafts unpublished", async ({
    page,
  }) => {
    const state = newState();
    const section = await openSettings(page, state);
    // Unsaved input is never offered as a movable resource.
    await section.getByLabel("Güter").fill("Leiter, Werkzeug");
    await section.getByLabel("Kurzbeschreibung").fill("Ungespeichert neu");

    const move = await openEditor(section);
    const tagSelect = move.getByTestId("garnrolle-move-tag");
    await expect(tagSelect.locator("option")).toHaveText([
      "– auswählen –",
      "Gut: Leiter",
      "Fähigkeit: Fahrrad reparieren",
    ]);
    await expect(move.getByTestId("garnrolle-move-cache-notice")).toContainText(
      "nicht zurückholen",
    );
    await expect(move.getByTestId("garnrolle-move-scope")).toHaveValue(
      "private",
    );

    await tagSelect.selectOption("good:Leiter");
    await move.getByTestId("garnrolle-move-scope").selectOption("circles");
    const submit = move.getByTestId("garnrolle-move-submit");
    await expect(submit).toBeDisabled();
    await move.getByRole("checkbox", { name: "Test-Haus" }).check();

    const profileWrites: Request[] = [];
    page.on("request", (request) => {
      if (
        request.url().endsWith("/api/accounts/me/profile") &&
        request.method() === "PATCH"
      )
        profileWrites.push(request);
    });
    await submit.click();
    await expect(move.getByTestId("garnrolle-move-success")).toContainText(
      "„Leiter“ ist jetzt eine eigene Ressource",
    );
    expect(state.moves).toHaveLength(1);
    expect(state.moves[0]).toEqual({
      tag: "good:Leiter",
      scope: "circles",
      circle_ids: [CIRCLE_ID],
      operation_id: expect.stringMatching(
        /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
      ),
    });
    // No frontend POST + PATCH sequence: the server moved it atomically.
    expect(profileWrites).toHaveLength(0);

    // Only the moved value left the draft; other unsaved edits remain.
    await expect(section.getByLabel("Güter")).toHaveValue("Werkzeug");
    await expect(section.getByLabel("Kurzbeschreibung")).toHaveValue(
      "Ungespeichert neu",
    );
    await expect(section.getByLabel("Fähigkeiten")).toHaveValue(
      "Fahrrad reparieren",
    );
    await expect(tagSelect.locator("option")).toHaveText([
      "– auswählen –",
      "Fähigkeit: Fahrrad reparieren",
    ]);

    await section.getByTestId("save-garnrolle").click();
    await expect.poll(() => state.patches.length).toBe(1);
    expect(state.patches[0].tags).toEqual([
      "skill:Fahrrad reparieren",
      "good:Werkzeug",
      "interest:Commons",
    ]);
    expect(state.patches[0].expected_tags).toEqual([
      "account",
      "garnrolle",
      "skill:Fahrrad reparieren",
      "interest:Commons",
    ]);
  });

  test("retries an uncertain move with the same operation and never republishes on conflict", async ({
    page,
  }) => {
    const state = newState();
    state.moveStatuses = [500];
    const section = await openSettings(page, state);
    const move = await openEditor(section);
    await move.getByTestId("garnrolle-move-tag").selectOption("good:Leiter");

    const submit = move.getByTestId("garnrolle-move-submit");
    await submit.click();
    await expect(move.getByTestId("garnrolle-move-error")).toContainText(
      "Unklar",
    );
    // An unknown outcome is never reported as "still public, unchanged".
    await expect(move.getByTestId("garnrolle-move-error")).not.toContainText(
      /unverändert|bleibt öffentlich|Nichts wurde geändert/,
    );
    await expect(submit).toHaveText("Erneut versuchen");
    await submit.click();
    await expect(move.getByTestId("garnrolle-move-success")).toContainText(
      "nur für dich",
    );
    expect(state.moves).toHaveLength(2);
    expect(state.moves[1].operation_id).toBe(state.moves[0].operation_id);
    expect(state.moves[1]).toMatchObject({
      tag: "good:Leiter",
      scope: "private",
    });

    // Another tab moved a tag meanwhile: this editor's save conflicts.
    state.tags = state.tags.filter((tag) => tag !== "skill:Fahrrad reparieren");
    state.patchStatuses = [409];
    await section.getByTestId("save-garnrolle").click();
    await expect(section.getByRole("alert").first()).toContainText(
      "Nichts wurde gespeichert",
    );
    // The stale value leaves the draft, but nothing is saved automatically.
    await expect(section.getByLabel("Fähigkeiten")).toHaveValue("");
    await page.waitForTimeout(300);
    expect(state.patches).toHaveLength(1);
    expect(state.tags).not.toContain("skill:Fahrrad reparieren");
  });

  test("loads neither editor, circles nor conversion API before the explicit open", async ({
    page,
  }) => {
    const state = newState();
    const scripts: string[] = [];
    page.on("request", (request) => {
      if (request.resourceType() === "script") scripts.push(request.url());
    });
    const section = await openSettings(page, state);
    await page.waitForLoadState("networkidle");

    const open = section.getByTestId("garnrolle-move-open");
    await expect(open).toHaveText(
      "Gespeicherte Güter/Fähigkeiten mit eigener Sichtbarkeit verwalten",
    );
    await expect(open).toHaveAttribute("aria-expanded", "false");
    await expect(section.getByTestId("garnrolle-move-resource")).toHaveCount(0);
    expect(state.moves).toHaveLength(0);

    // Other settings panels may list circles; this editor must add none.
    const circleRequestsBefore = state.circleRequests.length;
    const scriptsBeforeOpen = scripts.length;
    const move = await openEditor(section);
    await expect(open).toHaveAttribute("aria-expanded", "true");
    expect(scripts.length).toBeGreaterThan(scriptsBeforeOpen);
    // Circles are only needed for the circle audience.
    await page.waitForLoadState("networkidle");
    expect(state.circleRequests).toHaveLength(circleRequestsBefore);
    await move.getByTestId("garnrolle-move-scope").selectOption("circles");
    await expect
      .poll(() => state.circleRequests.length)
      .toBe(circleRequestsBefore + 1);

    // Closing and reopening reuses the loaded module and the loaded circles.
    const scriptsAfterOpen = scripts.length;
    await open.click();
    await expect(section.getByTestId("garnrolle-move-resource")).toHaveCount(0);
    await open.click();
    await expect(section.getByTestId("garnrolle-move-resource")).toBeVisible();
    expect(scripts.length).toBe(scriptsAfterOpen);
    expect(state.moves).toHaveLength(0);
  });

  test("offers no editor entry without a movable saved tag", async ({
    page,
  }) => {
    const state = newState();
    state.tags = ["account", "garnrolle", "interest:Commons"];
    const section = await openSettings(page, state);
    await expect(section.getByTestId("garnrolle-move-open")).toHaveCount(0);
    await expect(section.getByTestId("garnrolle-move-resource")).toHaveCount(0);
  });

  test("never shows circles of another account after an account switch", async ({
    page,
  }) => {
    const state = newState();
    const section = await openSettings(page, state);
    await page.waitForLoadState("networkidle");
    let release!: () => void;
    state.circleGate = new Promise<void>((resolve) => {
      release = resolve;
    });
    const circleRequestsBefore = state.circleRequests.length;
    const move = await openEditor(section);
    await move.getByTestId("garnrolle-move-tag").selectOption("good:Leiter");
    await move.getByTestId("garnrolle-move-scope").selectOption("circles");
    await expect
      .poll(() => state.circleRequests.length)
      .toBeGreaterThan(circleRequestsBefore);
    expect(state.circleRequests[circleRequestsBefore]).toBe(ACCOUNT_ID);

    // Another account takes over while that circle request is still open.
    state.accountId = OTHER_ACCOUNT_ID;
    await page.getByTestId("account-section-logout-all").click();
    await expect(section.getByTestId("garnrolle-move-resource")).toHaveCount(0);
    await expect(section.getByLabel("Güter")).toHaveValue("Leiter");
    const otherMove = await openEditor(section);
    // Nothing of the previous account's selection survives.
    await expect(otherMove.getByTestId("garnrolle-move-tag")).toHaveValue("");
    await otherMove.getByTestId("garnrolle-move-scope").selectOption("circles");

    // The stale answer of the first account now arrives, before the new one.
    release();
    await expect(
      otherMove.getByRole("checkbox", { name: "Haus des Anderen" }),
    ).toBeVisible();
    await page.waitForTimeout(300);
    await expect(section.getByText("Test-Haus")).toHaveCount(0);
    await expect(section.getByRole("checkbox")).toHaveCount(1);
  });

  test("lets a failed circle load be repeated", async ({ page }) => {
    const state = newState();
    const section = await openSettings(page, state);
    await page.waitForLoadState("networkidle");
    // Only the editor's own request fails; other panels already loaded.
    state.circleStatuses = [500];
    const move = await openEditor(section);
    await move.getByTestId("garnrolle-move-scope").selectOption("circles");
    await expect(move).toContainText("Gemeinschaften konnten nicht geladen");
    await move.getByTestId("garnrolle-move-circles-retry").click();
    await expect(
      move.getByRole("checkbox", { name: "Test-Haus" }),
    ).toBeVisible();
  });

  test("adopts the server tags after a stale move without republishing", async ({
    page,
  }) => {
    const state = newState();
    state.moveStatuses = [409];
    const section = await openSettings(page, state);
    const move = await openEditor(section);
    await move.getByTestId("garnrolle-move-tag").selectOption("good:Leiter");
    // Another tab already moved it.
    state.tags = state.tags.filter((tag) => tag !== "good:Leiter");

    const submit = move.getByTestId("garnrolle-move-submit");
    await submit.click();
    await expect(move.getByTestId("garnrolle-move-error")).toContainText(
      "nicht mehr gespeichert",
    );
    await expect(move.getByTestId("garnrolle-move-tag")).toHaveValue("");
    await expect(
      move.getByTestId("garnrolle-move-tag").locator("option"),
    ).toHaveText(["– auswählen –", "Fähigkeit: Fahrrad reparieren"]);
    await expect(section.getByLabel("Güter")).toHaveValue("");
    await expect(submit).toHaveText("Als eigene Ressource umziehen");
    expect(state.moves).toHaveLength(1);
    expect(state.patches).toHaveLength(0);
  });

  test("reports a definite rejection without offering a blind retry", async ({
    page,
  }) => {
    const state = newState();
    state.moveStatuses = [403];
    const section = await openSettings(page, state);
    const move = await openEditor(section);
    await move.getByTestId("garnrolle-move-tag").selectOption("good:Leiter");
    const submit = move.getByTestId("garnrolle-move-submit");
    await submit.click();
    await expect(move.getByTestId("garnrolle-move-error")).toContainText(
      "Nichts wurde geändert",
    );
    await expect(submit).toHaveText("Als eigene Ressource umziehen");
    await expect(section.getByLabel("Güter")).toHaveValue("Leiter");
  });
  test("never republishes a tag that moved after the draft was written", async ({
    page,
  }) => {
    const state = newState();
    // Moved to a private node while the old draft was stored.
    state.tags = SAVED_TAGS.filter((tag) => tag !== "good:Leiter");
    const serverTags = [...state.tags];
    const section = await openSettings(page, state, {
      title: "Entwurf-Name",
      draft: { ...OLD_DRAFT, baseTags: SAVED_TAGS },
    });
    // Local additions stay, the moved value is gone, nothing is left to decide.
    await expect(section.getByLabel("Güter")).toHaveValue("Werkzeug");
    await expect(section.getByLabel("Fähigkeiten")).toHaveValue(
      "Fahrrad reparieren",
    );
    await expect(section.getByTestId("garnrolle-tag-review")).toHaveCount(0);

    await section.getByTestId("save-garnrolle").click();
    await expect.poll(() => state.patches.length).toBe(1);
    expect(state.patches[0].tags).toEqual([
      "skill:Fahrrad reparieren",
      "good:Werkzeug",
      "interest:Commons",
    ]);
    expect(state.patches[0].expected_tags).toEqual(serverTags);
    expect(state.tags).not.toContain("good:Leiter");
    expect(state.moves).toHaveLength(0);
  });

  test("merges foreign additions into a restored draft and keeps deliberate removals", async ({
    page,
  }) => {
    const state = newState();
    state.tags = [
      ...SAVED_TAGS.filter((tag) => tag !== "good:Leiter"),
      "skill:Imkern",
      "good:Honig",
    ];
    const section = await openSettings(page, state, {
      title: "Entwurf-Name",
      // The user had deleted "Commons" on purpose.
      draft: { ...OLD_DRAFT, interests: "", baseTags: SAVED_TAGS },
    });
    await expect(section.getByLabel("Fähigkeiten")).toHaveValue(
      "Fahrrad reparieren, Imkern",
    );
    await expect(section.getByLabel("Güter")).toHaveValue("Werkzeug, Honig");
    await expect(section.getByLabel("Interessen")).toHaveValue("");
    await section.getByTestId("save-garnrolle").click();
    await expect.poll(() => state.patches.length).toBe(1);
    expect(state.patches[0].tags).toEqual([
      "skill:Fahrrad reparieren",
      "skill:Imkern",
      "good:Werkzeug",
      "good:Honig",
    ]);
  });

  test("stores the confirmed baseline with a draft and reconciles it after a move", async ({
    page,
  }) => {
    const state = newState();
    const section = await openSettings(page, state);
    await section.getByLabel("Güter").fill("Leiter, Werkzeug");
    await section.getByTestId("garnrolle-private-resource-link").click();
    await page.waitForURL(/\/map\?compose=node/);
    const stored = await page.evaluate(
      (accountId) =>
        sessionStorage.getItem(`weltgewebe:garnrolle-draft:${accountId}`),
      ACCOUNT_ID,
    );
    expect(JSON.parse(stored ?? "{}")).toMatchObject({
      goods: "Leiter, Werkzeug",
      baseTags: SAVED_TAGS,
    });

    // Moved elsewhere while the user was on the map.
    state.tags = state.tags.filter((tag) => tag !== "good:Leiter");
    await page.goto("/settings#meine-garnrolle");
    const back = page.getByTestId("my-garnrolle-section");
    await expect(back.getByLabel("Güter")).toHaveValue("Werkzeug");
    await expect(back.getByTestId("garnrolle-tag-review")).toHaveCount(0);
  });

  test("applies a draft without baseline only after an explicit decision", async ({
    page,
  }) => {
    const state = newState();
    state.tags = SAVED_TAGS.filter((tag) => tag !== "good:Leiter");
    const serverTags = [...state.tags];
    const section = await openSettings(page, state, {
      title: "Entwurf-Name",
      draft: OLD_DRAFT,
    });
    // Other draft fields survive; the resource fields show the server state.
    await expect(section.getByLabel("Kurzbeschreibung")).toHaveValue(
      "Entwurf-Beschreibung",
    );
    await expect(section.getByLabel("Güter")).toHaveValue("");
    await expect(section.getByLabel("Fähigkeiten")).toHaveValue(
      "Fahrrad reparieren",
    );
    const review = section.getByTestId("garnrolle-tag-review");
    await expect(review).toContainText("Güter: Leiter, Werkzeug");
    await expect(review).not.toContainText("Fähigkeiten:");
    await expect(review).toContainText("gesperrt");
    const save = section.getByTestId("save-garnrolle");
    await expect(save).toBeDisabled();

    // The map hand-off would store a draft too: it waits for the decision.
    await section.getByTestId("garnrolle-private-resource-link").click();
    await page.waitForTimeout(300);
    await expect(page).toHaveURL(/\/settings/);
    await expect(section.getByTestId("garnrolle-move-open")).toBeDisabled();
    expect(state.patches).toHaveLength(0);

    await review.getByTestId("garnrolle-tag-review-discard").click();
    await expect(review).toHaveCount(0);
    await expect(section.getByLabel("Güter")).toHaveValue("");
    await expect(save).toBeEnabled();
    await save.click();
    await expect.poll(() => state.patches.length).toBe(1);
    expect(state.patches[0].tags).toEqual([
      "skill:Fahrrad reparieren",
      "interest:Commons",
    ]);
    expect(state.patches[0].expected_tags).toEqual(serverTags);
  });

  test("takes over the entries of a draft without baseline only on request", async ({
    page,
  }) => {
    const state = newState();
    state.tags = SAVED_TAGS.filter((tag) => tag !== "good:Leiter");
    const section = await openSettings(page, state, {
      title: "Entwurf-Name",
      draft: OLD_DRAFT,
    });
    const review = section.getByTestId("garnrolle-tag-review");
    await review.getByTestId("garnrolle-tag-review-adopt").click();
    await expect(review).toHaveCount(0);
    await expect(section.getByLabel("Güter")).toHaveValue("Leiter, Werkzeug");
    await expect(section.getByLabel("Fähigkeiten")).toHaveValue(
      "Fahrrad reparieren",
    );
    await section.getByTestId("save-garnrolle").click();
    await expect.poll(() => state.patches.length).toBe(1);
    expect(state.patches[0].tags).toContain("good:Leiter");
  });

  test("asks nothing when a draft without baseline matches the server state", async ({
    page,
  }) => {
    const state = newState();
    const section = await openSettings(page, state, {
      title: "Entwurf-Name",
      draft: { ...OLD_DRAFT, goods: "Leiter", skills: "Fahrrad reparieren" },
    });
    await expect(section.getByLabel("Güter")).toHaveValue("Leiter");
    await expect(section.getByTestId("garnrolle-tag-review")).toHaveCount(0);
    await expect(section.getByTestId("save-garnrolle")).toBeEnabled();
  });

  test("fails closed when the reconciliation module cannot be loaded", async ({
    page,
  }) => {
    const state = newState();
    state.tags = SAVED_TAGS.filter((tag) => tag !== "good:Leiter");
    state.breakChunks = true;
    const section = await openSettings(page, state, {
      draft: OLD_DRAFT,
      skipWait: true,
    });
    await expect(section.getByTestId("garnrolle-error")).toContainText(
      "Laden fehlgeschlagen",
    );
    await expect(section.getByLabel("Anzeigename")).toBeDisabled();
    await expect(section.getByTestId("save-garnrolle")).toBeDisabled();
    await expect(section.getByLabel("Güter")).not.toHaveValue(/Werkzeug/);
    await page.waitForTimeout(300);
    expect(state.patches).toHaveLength(0);
  });

  test("keeps a tag another session added after the conflict", async ({
    page,
  }) => {
    const state = newState();
    const section = await openSettings(page, state);
    await section.getByLabel("Güter").fill("Leiter, Werkzeug");
    // Another session saved a public skill meanwhile.
    state.tags = [...SAVED_TAGS, "skill:Imkern"];
    const serverTags = [...state.tags];

    const save = section.getByTestId("save-garnrolle");
    await save.click();
    await expect(section.getByTestId("garnrolle-error")).toContainText(
      "Nichts wurde gespeichert",
    );
    // Merged visibly, local input kept, nothing saved yet.
    await expect(section.getByLabel("Fähigkeiten")).toHaveValue(
      "Fahrrad reparieren, Imkern",
    );
    await expect(section.getByLabel("Güter")).toHaveValue("Leiter, Werkzeug");
    expect(state.tags).toEqual(serverTags);

    await save.click();
    await expect.poll(() => state.patches.length).toBe(2);
    expect(state.patches[1].tags).toEqual([
      "skill:Fahrrad reparieren",
      "skill:Imkern",
      "good:Leiter",
      "good:Werkzeug",
      "interest:Commons",
    ]);
    expect(state.patches[1].expected_tags).toEqual(serverTags);
    expect(state.tags).toContain("skill:Imkern");
  });

  test("keeps saving blocked until the conflicting tags were merged", async ({
    page,
  }) => {
    const state = newState();
    const section = await openSettings(page, state);
    state.tags = [...SAVED_TAGS, "skill:Imkern"];
    // The reload that follows the conflict fails once.
    state.getStatuses = [500];

    const save = section.getByTestId("save-garnrolle");
    await save.click();
    await expect(section.getByTestId("garnrolle-error")).toContainText(
      "Nichts wurde gespeichert",
    );
    await expect(section.getByLabel("Fähigkeiten")).toHaveValue(
      "Fahrrad reparieren",
    );
    // Not merged, so the retry is rejected again instead of deleting it.
    await save.click();
    await expect.poll(() => state.patches.length).toBe(2);
    await expect(section.getByLabel("Fähigkeiten")).toHaveValue(
      "Fahrrad reparieren, Imkern",
    );
    expect(state.tags).toContain("skill:Imkern");
    expect(state.patches[0].tags).not.toContain("skill:Imkern");
    expect(state.patches[1].tags).not.toContain("skill:Imkern");

    await save.click();
    await expect.poll(() => state.patches.length).toBe(3);
    expect(state.patches[2].tags).toContain("skill:Imkern");
    expect(state.tags).toContain("skill:Imkern");
  });

  test("merges foreign additions after a stale move without republishing", async ({
    page,
  }) => {
    const state = newState();
    state.moveStatuses = [409];
    const section = await openSettings(page, state);
    const move = await openEditor(section);
    await move.getByTestId("garnrolle-move-tag").selectOption("good:Leiter");
    state.tags = [
      ...SAVED_TAGS.filter((tag) => tag !== "good:Leiter"),
      "skill:Imkern",
    ];
    await move.getByTestId("garnrolle-move-submit").click();
    await expect(move.getByTestId("garnrolle-move-error")).toContainText(
      "nicht mehr gespeichert",
    );
    await expect(section.getByLabel("Güter")).toHaveValue("");
    await expect(section.getByLabel("Fähigkeiten")).toHaveValue(
      "Fahrrad reparieren, Imkern",
    );
    await section.getByTestId("save-garnrolle").click();
    await expect.poll(() => state.patches.length).toBe(1);
    expect(state.patches[0].tags).toEqual([
      "skill:Fahrrad reparieren",
      "skill:Imkern",
      "interest:Commons",
    ]);
  });

  test("never republishes a moved tag with outer spaces in its suffix", async ({
    page,
  }) => {
    const state = newState();
    state.tags = [...SAVED_TAGS, "good: Leiter  "].filter(
      (tag) => tag !== "good:Leiter",
    );
    const section = await openSettings(page, state);
    await expect(section.getByLabel("Güter")).toHaveValue(/Leiter/);
    await section.getByLabel("Güter").fill("Leiter, Werkzeug");
    const move = await openEditor(section);
    await move
      .getByTestId("garnrolle-move-tag")
      .selectOption({ label: "Gut: Leiter" });
    await move.getByTestId("garnrolle-move-submit").click();
    await expect(move.getByTestId("garnrolle-move-success")).toBeVisible();
    expect(state.moves[0].tag).toBe("good: Leiter  ");
    await expect(section.getByLabel("Güter")).toHaveValue("Werkzeug");
    await expect(section.getByTestId("garnrolle-tag-review")).toHaveCount(0);

    await section.getByTestId("save-garnrolle").click();
    await expect.poll(() => state.patches.length).toBe(1);
    expect(state.patches[0].tags).toEqual([
      "skill:Fahrrad reparieren",
      "good:Werkzeug",
      "interest:Commons",
    ]);
    expect(state.tags.join("|")).not.toContain("Leiter");
  });

  test("keeps an entry that a twin spelling still publishes", async ({
    page,
  }) => {
    const state = newState();
    state.tags = [...SAVED_TAGS, "good: Leiter"];
    const section = await openSettings(page, state, { goods: /Leiter/ });
    const move = await openEditor(section);
    await move
      .getByTestId("garnrolle-move-tag")
      .selectOption({ value: "good: Leiter" });
    await move.getByTestId("garnrolle-move-submit").click();
    await expect(move.getByTestId("garnrolle-move-success")).toBeVisible();
    await expect(section.getByLabel("Güter")).toHaveValue(/Leiter/);
    await expect(section.getByTestId("garnrolle-tag-review")).toHaveCount(0);
    await section.getByTestId("save-garnrolle").click();
    await expect.poll(() => state.patches.length).toBe(1);
    expect(state.patches[0].tags).toContain("good:Leiter");
  });

  test.describe("tag with a comma that was accepted historically", () => {
    const COMMA = "good:Hammer, Bohrer";
    const withComma = () => [...SAVED_TAGS, COMMA];

    async function moveComma(page: Page, state: ServerState) {
      const section = await openSettings(page, state, {
        goods: "Leiter, Hammer, Bohrer",
      });
      // An unsaved entry the user typed must not vanish either.
      await section.getByLabel("Güter").fill("Leiter, Hammer, Bohrer, Zange");
      const move = await openEditor(section);
      await move.getByTestId("garnrolle-move-tag").selectOption(COMMA);
      await move.getByTestId("garnrolle-move-submit").click();
      await expect(move.getByTestId("garnrolle-move-success")).toBeVisible();
      return section;
    }

    test("locks saving and rebuilds the field from the server tags", async ({
      page,
    }) => {
      const state = newState();
      state.tags = withComma();
      const section = await moveComma(page, state);
      expect(state.moves[0].tag).toBe(COMMA);
      await expect(section.getByLabel("Güter")).toHaveValue("Leiter");
      const review = section.getByTestId("garnrolle-tag-review");
      await expect(review).toContainText("Güter: Hammer, Bohrer, Zange");
      await expect(section.getByTestId("save-garnrolle")).toBeDisabled();

      await review.getByTestId("garnrolle-tag-review-discard").click();
      await expect(review).toHaveCount(0);
      await section.getByTestId("save-garnrolle").click();
      await expect.poll(() => state.patches.length).toBe(1);
      expect(state.patches[0].tags).toEqual([
        "skill:Fahrrad reparieren",
        "good:Leiter",
        "interest:Commons",
      ]);
      expect(state.tags.join("|")).not.toMatch(/Hammer|Bohrer/);
    });

    test("publishes the reviewed entries only on explicit request", async ({
      page,
    }) => {
      const state = newState();
      state.tags = withComma();
      const section = await moveComma(page, state);
      await expect.poll(() => state.patches.length).toBe(0);
      await section
        .getByTestId("garnrolle-tag-review")
        .getByTestId("garnrolle-tag-review-adopt")
        .click();
      await expect(section.getByLabel("Güter")).toHaveValue(
        "Leiter, Hammer, Bohrer, Zange",
      );
      await section.getByTestId("save-garnrolle").click();
      await expect.poll(() => state.patches.length).toBe(1);
      expect(state.patches[0].tags).toContain("good:Hammer");
    });

    test("asks about the entries of a stored draft after the tag moved", async ({
      page,
    }) => {
      const state = newState();
      state.tags = SAVED_TAGS;
      const section = await openSettings(page, state, {
        title: "Entwurf-Name",
        draft: {
          ...OLD_DRAFT,
          goods: "Leiter, Hammer, Bohrer",
          baseTags: withComma(),
        },
      });
      await expect(section.getByLabel("Güter")).toHaveValue("Leiter");
      await expect(section.getByTestId("garnrolle-tag-review")).toContainText(
        "Güter: Hammer, Bohrer",
      );
      await expect(section.getByTestId("save-garnrolle")).toBeDisabled();
    });

    test("asks after a conflict even though the editor was closed", async ({
      page,
    }) => {
      const state = newState();
      state.tags = withComma();
      const section = await openSettings(page, state, {
        goods: "Leiter, Hammer, Bohrer",
      });
      // Moved by another session.
      state.tags = SAVED_TAGS;
      await section.getByTestId("save-garnrolle").click();
      await expect(section.getByTestId("garnrolle-error")).toContainText(
        "Nichts wurde gespeichert",
      );
      await expect(section.getByLabel("Güter")).toHaveValue("Leiter");
      await expect(section.getByTestId("garnrolle-tag-review")).toContainText(
        "Hammer, Bohrer",
      );
      await expect(section.getByTestId("save-garnrolle")).toBeDisabled();
      expect(state.patches).toHaveLength(1);
      expect(state.tags.join("|")).not.toMatch(/Hammer|Bohrer/);
    });
  });

  test("never republishes a stored draft entry with outer spaces after the move", async ({
    page,
  }) => {
    const state = newState();
    state.tags = SAVED_TAGS.filter((tag) => tag !== "good:Leiter");
    const section = await openSettings(page, state, {
      title: "Entwurf-Name",
      draft: {
        ...OLD_DRAFT,
        baseTags: [...state.tags, "good: Leiter "],
      },
    });
    await expect(section.getByLabel("Güter")).toHaveValue("Werkzeug");
    await expect(section.getByTestId("garnrolle-tag-review")).toHaveCount(0);
  });

  test("ignores a conflict answer that arrives after an account switch", async ({
    page,
  }) => {
    const state = newState();
    const section = await openSettings(page, state);
    const release = holdLazyChunk(state);
    state.tags = [...SAVED_TAGS, "skill:Imkern"];
    await section.getByTestId("save-garnrolle").click();
    await expect.poll(() => state.patches.length).toBe(1);

    // The other account takes over while the lazy module is still loading.
    state.accountId = OTHER_ACCOUNT_ID;
    state.tags = [...SAVED_TAGS];
    await page.getByTestId("account-section-logout-all").click();
    await expect(section.getByLabel("Fähigkeiten")).toHaveValue(
      "Fahrrad reparieren",
    );
    release();
    await page.waitForTimeout(300);
    await expect(section.getByLabel("Fähigkeiten")).toHaveValue(
      "Fahrrad reparieren",
    );
    await expect(section.getByTestId("garnrolle-error")).toHaveCount(0);

    const save = section.getByTestId("save-garnrolle");
    await expect(save).toBeEnabled();
    await save.click();
    await expect.poll(() => state.patches.length).toBe(2);
    // Nothing of the first account's tags leaked into the new baseline.
    expect(state.patches[1].tags).not.toContain("skill:Imkern");
    expect(state.patches[1].expected_tags).toEqual(SAVED_TAGS);
  });

  test("applies no draft of the previous account when the switch happens while its reconciliation loads", async ({
    page,
  }) => {
    const state = newState();
    state.tags = SAVED_TAGS.filter((tag) => tag !== "good:Leiter");
    const release = holdLazyChunk(state);
    const section = await openSettings(page, state, {
      draft: OLD_DRAFT,
      skipWait: true,
    });
    // The profile is loaded; its draft waits for the held module.
    await page.waitForTimeout(500);

    state.accountId = OTHER_ACCOUNT_ID;
    state.tags = ["account", "garnrolle", "interest:Commons"];
    await page.getByTestId("account-section-logout-all").click();
    await expect(section.getByLabel("Interessen")).toHaveValue("Commons");
    release();
    await page.waitForTimeout(300);

    await expect(section.getByTestId("garnrolle-tag-review")).toHaveCount(0);
    await expect(section.getByLabel("Güter")).toHaveValue("");
    await expect(section.getByLabel("Anzeigename")).toHaveValue(
      "Meine Garnrolle",
    );
    await expect(section.getByTestId("save-garnrolle")).toBeEnabled();
  });
});
