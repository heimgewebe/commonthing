import { expect, test, type Page, type Request } from "@playwright/test";
import { mockApiResponses } from "./fixtures/mockApi";

const ACCOUNT_ID = "00000000-0000-0000-0000-000000000003";
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
};

/**
 * Stateful profile/resource mock on top of the shared API fixture. The real
 * atomicity and privacy proof lives in the PostgreSQL API tests; this only
 * proves the browser sends the right single-tag request and keeps drafts.
 */
async function openSettings(page: Page, state: ServerState) {
  await mockApiResponses(page, {
    auth: { authenticated: true, account_id: ACCOUNT_ID, role: "weber" },
  });
  await page.route("**/api/circles/me*", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([
        {
          id: CIRCLE_ID,
          kind: "household",
          name: "Test-Haus",
          state: "active",
          membership_status: "active",
        },
      ]),
    }),
  );
  const profile = () => ({
    id: ACCOUNT_ID,
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
      state.tags = payload.tags as string[];
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
  await expect(section.getByLabel("Güter")).toHaveValue("Leiter");
  return section;
}

function newState(): ServerState {
  return {
    tags: [...SAVED_TAGS],
    moveStatuses: [],
    patchStatuses: [],
    moves: [],
    patches: [],
  };
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

    const move = section.getByTestId("garnrolle-move-resource");
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
    const move = section.getByTestId("garnrolle-move-resource");
    await move.getByTestId("garnrolle-move-tag").selectOption("good:Leiter");

    const submit = move.getByTestId("garnrolle-move-submit");
    await submit.click();
    await expect(move.getByTestId("garnrolle-move-error")).toContainText(
      "Unklar",
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
});
