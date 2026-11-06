import { expect, test } from "@playwright/test";
import { FADEN_LIFETIME_MS } from "../src/lib/map/edgeLifecycle";
import { conversationRingScale } from "../src/lib/map/weaveModel";
import {
  CONVERSATION_RING_BASE_DIAMETER_PERCENT,
  CONVERSATION_THREAD_WIDTH_PX,
} from "../src/lib/map/weaveVisualTokens";
import { demoAccounts, demoNodes } from "../src/lib/demo/demoData";
import { mockApiResponses, mockListResponse } from "./fixtures/mockApi";
import { centerDemoNode } from "./fixtures/mapCamera";
import { activateToolFanAction } from "./fixtures/toolFan";

const NODE_ID = demoNodes[0].id;
const ACCOUNT_A = demoAccounts[0].id;
const ACCOUNT_B = demoAccounts[1].id;
const CREATED_AT_MS = Date.now() - 60_000;
const CREATED_AT = new Date(CREATED_AT_MS).toISOString();
const EXPIRES_AT = new Date(CREATED_AT_MS + FADEN_LIFETIME_MS).toISOString();

function faden(
  id: string,
  sourceId: string,
  type: "knotting" | "conversation" | "proposal" | "vote",
  subjectId: string,
) {
  return {
    id,
    source_id: sourceId,
    source_type: "account",
    target_id: NODE_ID,
    target_type: "node",
    edge_kind: "reference",
    faden_type: type,
    faden_subject_id: subjectId,
    created_at: CREATED_AT,
    expires_at: EXPIRES_AT,
  };
}

test.describe("Gewachsene Knoten und antragsgebundene Stimmkränze", () => {
  test("renders the diagonal X, zones, proposal-bound votes and topic-coloured threads", async ({
    page,
  }) => {
    await mockApiResponses(page);
    // kind "Knoten" is ignored as a theme, so the two tags alone colour the arms.
    const multiThemeNode = {
      ...demoNodes[0],
      tags: ["Natur", "Bildung"],
      kind: "Knoten",
    };
    await page.route("**/api/nodes*", async (route) => {
      const requestUrl = route.request().url();
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(mockListResponse(requestUrl, [multiThemeNode])),
      });
    });
    const edges = [
      faden("knotting-a", ACCOUNT_A, "knotting", NODE_ID),
      faden(
        "conversation-general",
        ACCOUNT_B,
        "conversation",
        "conversation-node",
      ),
      faden("proposal-a", ACCOUNT_A, "proposal", "proposal-a"),
      faden("proposal-b", ACCOUNT_B, "proposal", "proposal-b"),
      faden("conversation-a", ACCOUNT_B, "conversation", "proposal-a"),
      faden("vote-a", ACCOUNT_A, "vote", "proposal-a"),
      faden("vote-b", ACCOUNT_B, "vote", "proposal-b"),
    ];

    await page.route("**/api/edges*", async (route) => {
      const requestUrl = route.request().url();
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(mockListResponse(requestUrl, edges)),
      });
    });

    await page.goto("/map");

    const marker = page.getByTestId(`marker-node-${NODE_ID}`);
    await expect(marker).toBeVisible({ timeout: 15_000 });
    const woven = marker.locator(".woven-node");
    await expect(woven).toHaveAttribute(
      "data-zone-order",
      "knotting,conversation,proposal,vote",
    );
    await expect(woven).toHaveAttribute("data-x-geometry", "diagonal");
    await expect(woven).toHaveAttribute("data-knotting-threads", "1");
    await expect(woven).toHaveAttribute("data-conversation-threads", "2");
    await expect(woven).toHaveAttribute("data-proposal-count", "2");
    await expect(woven).toHaveAttribute("data-vote-threads", "2");
    await expect(woven.locator(".woven-node__arm")).toHaveCount(4);
    await expect(woven.locator('[data-zone="proposal"]')).toHaveCount(2);
    await expect(woven.locator('[data-zone="vote"]')).toHaveCount(2);
    await expect(woven.locator(".woven-node__cross")).toHaveCount(0);

    const conversationGeometryProof = await woven.evaluate((root) => {
      const ring = root.querySelector<HTMLElement>(".woven-node__conversation");
      if (!ring) throw new Error("conversation ring missing");
      const rootStyle = getComputedStyle(root);
      const ringStyle = getComputedStyle(ring);
      const requestedScale = Number(
        (root as HTMLElement).style
          .getPropertyValue("--weave-conversation-scale")
          .trim(),
      );
      return {
        requestedScale,
        rootWidth: root.getBoundingClientRect().width,
        ringWidth: ring.getBoundingClientRect().width,
        yarnWidth: Number.parseFloat(
          rootStyle.getPropertyValue("--weave-conversation-width"),
        ),
        transform: ringStyle.transform,
      };
    });
    expect(conversationGeometryProof.requestedScale).toBeCloseTo(
      conversationRingScale(2),
    );
    expect(conversationGeometryProof.ringWidth).toBeCloseTo(
      conversationGeometryProof.rootWidth *
        (CONVERSATION_RING_BASE_DIAMETER_PERCENT / 100) *
        conversationGeometryProof.requestedScale,
      1,
    );
    expect(conversationGeometryProof.yarnWidth).toBe(
      CONVERSATION_THREAD_WIDTH_PX,
    );
    expect(conversationGeometryProof.transform).toBe("none");

    const armColorProof = await woven.evaluate((root) => {
      const threadColor = root.style
        .getPropertyValue("--weave-thread-color")
        .trim();
      const arms = Array.from(
        root.querySelectorAll<HTMLElement>(".woven-node__arm"),
      ).map((arm) => ({
        arm: arm.dataset.arm,
        inlineArmColor: arm.style.getPropertyValue("--arm-color").trim(),
        inheritedThreadColor: getComputedStyle(arm)
          .getPropertyValue("--weave-thread-color")
          .trim(),
      }));
      return { threadColor, arms };
    });
    // One physical knotting yarn keeps the shared terminal colour as fallback,
    // while the four stitched arms retain the target's multi-topic palette.
    expect(armColorProof.threadColor).toMatch(/^#[0-9a-f]{6}$/i);
    expect(armColorProof.arms).toHaveLength(4);
    const armColors = armColorProof.arms.map((arm) => arm.inlineArmColor);
    expect(armColors.every((color) => /^#[0-9a-f]{6}$/i.test(color))).toBe(
      true,
    );
    expect(new Set(armColors).size).toBeGreaterThan(1);
    for (const arm of armColorProof.arms) {
      expect(arm.inheritedThreadColor).toBe(armColorProof.threadColor);
    }

    const rendered = await page.evaluate((nodeId) => {
      const map = (window as any).__TEST_MAP__;
      const source = map?.getSource("edges-source");
      const serialized = source?.serialize?.();
      const root = document.querySelector(
        `[data-testid="marker-node-${nodeId}"] .woven-node`,
      ) as HTMLElement | null;
      return {
        markerTheme: root?.style.getPropertyValue("--weave-primary"),
        threadColor: root?.style
          .getPropertyValue("--weave-thread-color")
          .trim(),
        typedFeatures:
          serialized?.data?.features
            ?.filter((feature: any) => feature.properties.fadenType !== "out")
            .map((feature: any) => ({
              type: feature.properties.fadenType,
              themeColor: feature.properties.themeColor,
              themeColors: feature.properties.themeColors,
              id: feature.properties.id,
            })) ?? [],
      };
    }, NODE_ID);

    expect(rendered.markerTheme).toMatch(/^#[0-9a-f]{6}$/i);
    expect(rendered.typedFeatures.length).toBeGreaterThanOrEqual(edges.length);
    const edgeIds = new Set(
      rendered.typedFeatures.map((feature: any) => feature.id),
    );
    expect(edgeIds.size).toBe(edges.length);
    expect(rendered.threadColor).toMatch(/^#[0-9a-f]{6}$/i);
    const threadColor = rendered.threadColor;
    for (const feature of rendered.typedFeatures) {
      expect(feature.themeColors?.length).toBeGreaterThan(1);
      // The X's shared root colour is exactly the terminal (target-side)
      // colour of the incoming thread's theme palette — see
      // terminalThreadColor in weaveModel.ts.
      expect(feature.themeColors.at(-1)).toBe(threadColor);
      expect(feature.themeColors).toContain(feature.themeColor);
    }
    expect(
      new Set(rendered.typedFeatures.map((feature: any) => feature.type)),
    ).toEqual(new Set(["knotting", "conversation", "proposal", "vote"]));

    await expect(marker).toHaveAttribute(
      "aria-label",
      /Knüpfkern 1.*Gesprächsring 2.*Anträge 2.*Stimmen 2/,
    );

    // Zoom may only toggle compact/detail classes — never replace the marker body.
    await woven.evaluate((element) =>
      element.setAttribute("data-zoom-sentinel", "kept"),
    );
    await page.evaluate(() => (window as any).__TEST_MAP__.setZoom(13));
    await expect(woven).toHaveAttribute("data-weave-detail", "compact");
    await expect(woven.locator('[data-zone="vote"]').first()).toBeHidden();
    await expect(woven).toHaveAttribute("data-zoom-sentinel", "kept");

    await page.evaluate(() => (window as any).__TEST_MAP__.setZoom(14));
    await expect(woven).toHaveAttribute("data-weave-detail", "detail");
    await expect(woven.locator('[data-zone="vote"]').first()).toBeVisible();
    await expect(woven).toHaveAttribute("data-zoom-sentinel", "kept");

    const layerOrder = await woven.evaluate((root) => {
      const z = (selector: string) =>
        Number.parseInt(
          getComputedStyle(root.querySelector(selector) as Element).zIndex,
          10,
        );
      return {
        conversation: z(".woven-node__conversation"),
        x: z(".woven-node__x"),
        proposal: z('[data-zone="proposal"]'),
        vote: z('[data-zone="vote"]'),
      };
    });
    // No crossing/knot layer: conversation ring sits under the stitched X;
    // proposal and vote overlays remain above both.
    expect(layerOrder.conversation).toBeLessThan(layerOrder.x);
    expect(layerOrder.x).toBeLessThan(layerOrder.proposal);
    expect(layerOrder.proposal).toBeLessThan(layerOrder.vote);

    const voteContracts = await woven.evaluate((root) => {
      const proposals = Array.from(
        root.querySelectorAll<HTMLElement>('[data-zone="proposal"]'),
      );
      const votes = Array.from(
        root.querySelectorAll<HTMLElement>('[data-zone="vote"]'),
      );
      const rootStyle = getComputedStyle(root);
      return votes.map((vote) => {
        const slot = vote.dataset.proposalSlot;
        const proposal = proposals.find(
          (candidate) => candidate.dataset.proposalSlot === slot,
        );
        if (!proposal) throw new Error(`proposal slot ${slot} missing`);
        const voteStyle = getComputedStyle(vote) as CSSStyleDeclaration & {
          webkitMaskImage?: string;
        };
        const voteRect = vote.getBoundingClientRect();
        const proposalRect = proposal.getBoundingClientRect();
        return {
          slot,
          sameParent:
            vote.parentElement === root && proposal.parentElement === root,
          nestedInProposal: proposal.contains(vote),
          rootOverflow: rootStyle.overflow,
          voteWidth: voteRect.width,
          proposalWidth: proposalRect.width,
          opacity: Number(voteStyle.opacity),
          display: voteStyle.display,
          backgroundImage: voteStyle.backgroundImage,
          maskImage: voteStyle.maskImage || voteStyle.webkitMaskImage || "",
          voteTotal: vote.dataset.voteTotal,
        };
      });
    });

    expect(voteContracts).toHaveLength(2);
    for (const contract of voteContracts) {
      expect(contract.sameParent).toBe(true);
      expect(contract.nestedInProposal).toBe(false);
      expect(contract.rootOverflow).toBe("visible");
      expect(contract.voteWidth).toBeGreaterThan(contract.proposalWidth);
      expect(contract.opacity).toBeGreaterThan(0);
      expect(contract.display).not.toBe("none");
      expect(contract.backgroundImage).toContain("conic-gradient");
      expect(contract.maskImage).toContain("radial-gradient");
      expect(Number(contract.voteTotal)).toBeGreaterThan(0);
    }

    await page.evaluate(
      (nodeKind) => (window as any).__TEST_SET_ACTIVE_FILTERS__([nodeKind]),
      multiThemeNode.kind || "Knoten",
    );
    await expect(
      page.locator('[data-testid^="marker-garnrolle-"]'),
    ).toHaveCount(0);
    await expect(marker).toBeVisible();
    await expect(woven).toHaveAttribute("data-knotting-threads", "1");
    await expect(woven).toHaveAttribute("data-conversation-threads", "2");
    await expect(woven).toHaveAttribute("data-proposal-count", "2");
    await expect(woven).toHaveAttribute("data-vote-threads", "2");
  });

  test("expires a target-only weave edge exactly without ever drawing a line", async ({
    page,
  }) => {
    const now = new Date("2026-08-05T08:00:00.000Z");
    // Playwright install() lets time advance during map startup, but this
    // expiry must happen before the independent 60s periodic refresh.
    const expiryWindowMs = 30_000;
    await page.clock.install({ time: now });
    await mockApiResponses(page);

    const targetOnlyEdge = {
      id: "target-only-expiring-knotting",
      source_id: "outside-visible-markers",
      source_type: "account",
      target_id: NODE_ID,
      target_type: "node",
      edge_kind: "reference",
      faden_type: "knotting",
      faden_subject_id: NODE_ID,
      created_at: new Date(
        now.getTime() - FADEN_LIFETIME_MS + expiryWindowMs,
      ).toISOString(),
      expires_at: new Date(now.getTime() + expiryWindowMs).toISOString(),
    };

    await page.route("**/api/edges*", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(
          mockListResponse(route.request().url(), [targetOnlyEdge]),
        ),
      });
    });

    await page.goto("/map");
    const marker = page.getByTestId(`marker-node-${NODE_ID}`);
    const woven = marker.locator(".woven-node");
    await expect(marker).toBeVisible({ timeout: 15_000 });
    expect(await page.evaluate(() => Date.now())).toBeLessThan(
      new Date(targetOnlyEdge.expires_at).getTime(),
    );
    await expect(woven).toHaveAttribute("data-knotting-threads", "1");
    await expect(
      page.getByTestId("marker-garnrolle-outside-visible-markers"),
    ).toHaveCount(0);

    await expect
      .poll(async () =>
        page.evaluate(() => {
          const source = (window as any).__TEST_MAP__?.getSource(
            "edges-source",
          );
          return source?.serialize?.()?.data?.features?.length ?? 0;
        }),
      )
      .toBe(0);

    await page.clock.fastForward(expiryWindowMs + 1);
    // Otherwise a broken exact-expiry scheduler could pass via the minute
    // interval. If the test ran past that interval, reject the observation.
    expect(await page.evaluate(() => Date.now())).toBeLessThan(
      now.getTime() + 60_000,
    );
    await expect(woven).toHaveAttribute("data-knotting-threads", "0");
    await expect(woven).toHaveAttribute("data-proposal-count", "0");
  });

  test("does not bring an expired thread back when the target is filtered away and shown again", async ({
    page,
  }) => {
    const now = new Date("2026-08-05T08:00:00.000Z");
    await page.clock.install({ time: now });
    await mockApiResponses(page);

    // The source is not among the visible markers, so this thread may shape the
    // target body but must never become a map line.
    const targetOnlyEdge = {
      id: "target-only-filtered-knotting",
      source_id: "outside-visible-markers",
      source_type: "account",
      target_id: NODE_ID,
      target_type: "node",
      edge_kind: "reference",
      faden_type: "knotting",
      faden_subject_id: NODE_ID,
      created_at: new Date(
        now.getTime() - FADEN_LIFETIME_MS + 60_000,
      ).toISOString(),
      expires_at: new Date(now.getTime() + 60_000).toISOString(),
    };

    await page.route("**/api/edges*", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(
          mockListResponse(route.request().url(), [targetOnlyEdge]),
        ),
      });
    });

    const mapLineCount = () =>
      page.evaluate(() => {
        const source = (window as any).__TEST_MAP__?.getSource("edges-source");
        return source?.serialize?.()?.data?.features?.length ?? 0;
      });

    await page.goto("/map");
    const marker = page.getByTestId(`marker-node-${NODE_ID}`);
    const woven = marker.locator(".woven-node");
    await expect(marker).toBeVisible({ timeout: 15_000 });
    await expect(woven).toHaveAttribute("data-knotting-threads", "1");
    await expect(
      page.getByTestId("marker-garnrolle-outside-visible-markers"),
    ).toHaveCount(0);
    await expect.poll(mapLineCount).toBe(0);

    // Hide the target: nothing on screen observes the exact expiry any more.
    await page.evaluate(() =>
      (window as any).__TEST_SET_ACTIVE_FILTERS__(["Garnrolle"]),
    );
    await expect(marker).toHaveCount(0);
    expect(await mapLineCount()).toBe(0);

    // Advance wall time without firing the unrelated map-init watchdog, then
    // run the same projection refresh used by the production interval.
    await page.clock.setSystemTime(new Date(now.getTime() + 120_000));
    await page.evaluate(() =>
      (window as any).__TEST_REFRESH_EDGE_PROJECTION__(),
    );
    expect(await mapLineCount()).toBe(0);

    // Showing it again must read the current time, not the one from before.
    await page.evaluate(() => (window as any).__TEST_SET_ACTIVE_FILTERS__([]));
    await expect(marker).toBeVisible();
    await expect(woven).toHaveAttribute("data-knotting-threads", "0");
    await expect(woven).toHaveAttribute("data-proposal-count", "0");
    await expect.poll(mapLineCount).toBe(0);
  });

  test("dims an ageing proposal thread without replacing its DOM element", async ({
    page,
  }) => {
    const now = new Date("2026-08-05T08:00:00.000Z");
    await page.clock.install({ time: now });
    await mockApiResponses(page);

    const ageing = {
      id: "ageing-proposal",
      source_id: ACCOUNT_A,
      source_type: "account",
      target_id: NODE_ID,
      target_type: "node",
      edge_kind: "reference",
      faden_type: "proposal",
      faden_subject_id: "proposal-a",
      created_at: new Date(now.getTime() - 60_000).toISOString(),
      expires_at: new Date(
        now.getTime() - 60_000 + FADEN_LIFETIME_MS,
      ).toISOString(),
    };

    await page.route("**/api/edges*", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(mockListResponse(route.request().url(), [ageing])),
      });
    });

    await page.goto("/map");
    const marker = page.getByTestId(`marker-node-${NODE_ID}`);
    await expect(marker).toBeVisible({ timeout: 15_000 });
    const proposal = marker.locator(
      '.woven-node [data-zone="proposal"][data-proposal-slot="1"]',
    );
    await expect(proposal).toHaveCount(1);

    // A sentinel that only survives if this very element is kept alive.
    await proposal.evaluate((element) =>
      element.setAttribute("data-sentinel", "kept"),
    );
    const opacityBefore = await proposal.evaluate((element) =>
      Number((element as HTMLElement).style.opacity),
    );
    expect(opacityBefore).toBeGreaterThan(0);

    // Move the projection instant without firing the unrelated map-init
    // watchdog, then invoke the same production refresh used by the interval.
    await page.clock.setSystemTime(new Date(now.getTime() + 3_600_000));
    await page.evaluate(() =>
      (window as any).__TEST_REFRESH_EDGE_PROJECTION__(),
    );

    await expect
      .poll(() =>
        proposal.evaluate((element) =>
          Number((element as HTMLElement).style.opacity),
        ),
      )
      .toBeLessThan(opacityBefore);
    // The DOM was never rebuilt, only its opacity was written.
    await expect(proposal).toHaveAttribute("data-sentinel", "kept");
  });

  test("keeps geometry contracts across density, zoom and viewports with visual evidence", async ({
    page,
  }, testInfo) => {
    await mockApiResponses(page);
    const denseNode = {
      ...demoNodes[0],
      tags: ["Natur", "Bildung", "Kunst", "Handwerk"],
      kind: "Knoten",
    };
    await page.route("**/api/nodes*", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(
          mockListResponse(route.request().url(), [denseNode]),
        ),
      });
    });

    const edges = [
      faden("knotting-dense", ACCOUNT_A, "knotting", NODE_ID),
      ...Array.from({ length: 12 }, (_, index) =>
        faden(
          `conversation-dense-${index}`,
          index % 2 === 0 ? ACCOUNT_A : ACCOUNT_B,
          "conversation",
          "conversation-node",
        ),
      ),
      ...Array.from({ length: 10 }, (_, index) =>
        faden(
          `proposal-dense-${index}`,
          index % 2 === 0 ? ACCOUNT_A : ACCOUNT_B,
          "proposal",
          `proposal-dense-${index}`,
        ),
      ),
      ...Array.from({ length: 20 }, (_, index) =>
        faden(
          `vote-dense-${index}`,
          index % 2 === 0 ? ACCOUNT_A : ACCOUNT_B,
          "vote",
          `proposal-dense-${index % 10}`,
        ),
      ),
    ];
    await page.route("**/api/edges*", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(mockListResponse(route.request().url(), edges)),
      });
    });

    const evidenceDir = testInfo.outputPath("pr-1715-visual-evidence");
    const capture = async (name: string) => {
      await page.screenshot({
        path: `${evidenceDir}/${name}.png`,
        fullPage: false,
      });
    };

    await page.setViewportSize({ width: 1280, height: 800 });
    await page.goto("/map");
    await centerDemoNode(page);
    const marker = page.getByTestId(`marker-node-${NODE_ID}`);
    const woven = marker.locator(".woven-node");
    await expect(marker).toBeVisible({ timeout: 15_000 });

    // Full counts stay in model/aria even when the visual proposal slots cap at 8.
    await expect(woven).toHaveAttribute("data-proposal-count", "10");
    await expect(woven).toHaveAttribute("data-conversation-threads", "12");
    await expect(woven).toHaveAttribute("data-vote-threads", "20");
    await expect(woven.locator('[data-zone="proposal"]')).toHaveCount(8);
    await expect(woven.locator(".woven-node__overflow")).toHaveText("+3");
    await expect(woven.locator(".woven-node__arm")).toHaveCount(4);
    await expect(woven).toHaveAttribute("data-x-geometry", "diagonal");

    const denseConversationGeometry = await woven.evaluate((root) => {
      const ring = root.querySelector<HTMLElement>(".woven-node__conversation");
      if (!ring) throw new Error("conversation ring missing");
      const requestedScale = Number(
        (root as HTMLElement).style
          .getPropertyValue("--weave-conversation-scale")
          .trim(),
      );
      return {
        requestedScale,
        rootWidth: root.getBoundingClientRect().width,
        ringWidth: ring.getBoundingClientRect().width,
        yarnWidth: Number.parseFloat(
          getComputedStyle(root).getPropertyValue("--weave-conversation-width"),
        ),
        transform: getComputedStyle(ring).transform,
      };
    });
    expect(denseConversationGeometry.requestedScale).toBeCloseTo(
      conversationRingScale(12),
    );
    expect(denseConversationGeometry.requestedScale).toBeGreaterThan(
      conversationRingScale(2),
    );
    expect(denseConversationGeometry.ringWidth).toBeCloseTo(
      denseConversationGeometry.rootWidth *
        (CONVERSATION_RING_BASE_DIAMETER_PERCENT / 100) *
        denseConversationGeometry.requestedScale,
      1,
    );
    expect(denseConversationGeometry.yarnWidth).toBe(
      CONVERSATION_THREAD_WIDTH_PX,
    );
    expect(denseConversationGeometry.transform).toBe("none");

    const geometry = await woven.evaluate((root) => {
      const style = getComputedStyle(root);
      const host = (root.closest(".map-marker") ??
        root.parentElement?.parentElement) as HTMLElement | null;
      const hostBox = host?.getBoundingClientRect();
      const threadColor = root.style
        .getPropertyValue("--weave-thread-color")
        .trim();
      const armWidthToken = root.style
        .getPropertyValue("--weave-arm-width")
        .trim();
      const arms = Array.from(
        root.querySelectorAll<HTMLElement>(".woven-node__arm"),
      ).map((arm) => {
        const armStyle = getComputedStyle(arm);
        return {
          arm: arm.dataset.arm,
          inlineArmColor: arm.style.getPropertyValue("--arm-color").trim(),
          resolvedArmColor: armStyle.getPropertyValue("--arm-color").trim(),
          inheritedThreadColor: armStyle
            .getPropertyValue("--weave-thread-color")
            .trim(),
          width: armStyle.width,
          transform: armStyle.transform,
          filter: armStyle.filter,
          maskImage: armStyle.maskImage,
          boxShadow: armStyle.boxShadow,
        };
      });
      const box = root.getBoundingClientRect();
      return {
        overflow: style.overflow,
        background: style.backgroundColor,
        borderRadius: style.borderRadius,
        width: box.width,
        height: box.height,
        threadColor,
        armWidthToken,
        arms,
        hostWidth: hostBox?.width ?? 0,
        hostHeight: hostBox?.height ?? 0,
      };
    });
    expect(geometry.overflow).toBe("visible");
    expect(
      geometry.background === "rgba(0, 0, 0, 0)" ||
        geometry.background === "transparent",
    ).toBe(true);
    // One physical thread gauge, but the target's topic palette remains visible
    // in the X. The terminal incoming colour is only a fallback; each arm has
    // an explicit model-derived topic colour.
    expect(geometry.threadColor).toMatch(/^#[0-9a-f]{6}$/i);
    expect(geometry.arms).toHaveLength(4);
    const armColors = geometry.arms.map((arm) => arm.inlineArmColor);
    expect(armColors.every((color) => /^#[0-9a-f]{6}$/i.test(color))).toBe(
      true,
    );
    // Topic colours come from a bounded deterministic palette, so different
    // topic identities may legitimately collide. The regression contract is
    // that a multi-colour target must not collapse back to one terminal colour.
    expect(new Set(armColors).size).toBeGreaterThan(1);
    const armBoxShadow = geometry.arms[0].boxShadow;
    for (const arm of geometry.arms) {
      expect(arm.resolvedArmColor).toBe(arm.inlineArmColor);
      expect(arm.inheritedThreadColor).toBe(geometry.threadColor);
      // No strand-specific mask/filter/shadow may change apparent gauge.
      expect(arm.filter).toBe("none");
      expect(arm.maskImage).toBe("none");
      expect(arm.boxShadow).toBe(armBoxShadow);
    }
    expect(geometry.hostWidth).toBeGreaterThanOrEqual(44);
    expect(geometry.hostHeight).toBeGreaterThanOrEqual(44);
    // Diagonal X: both strands are rotated; no axis-aligned plus arms.
    expect(
      geometry.arms.every(
        (arm) => arm.transform.includes("matrix") || arm.transform !== "none",
      ),
    ).toBe(true);

    // Thread/X width continuity across representative zooms: shared token +
    // computed arm widths stay continuous. Compare numerically so browser
    // subpixel rounding (e.g. 4.15px → 4.14062px) is not flaky string equality.
    // MapLibre knotting body line-width (LAYERS.EDGES_KNOTTING_LAYER) is
    // mandatory evidence at every sample — not optional/null soft-pass.
    const parsePx = (value: string) => Number.parseFloat(value);
    const zoomWidthSamples: Array<{
      zoom: number;
      armWidthToken: string;
      armWidthPx: number[];
      knottingLineWidth: number;
    }> = [];
    for (const zoom of [12, 14, 16] as const) {
      await page.evaluate((z) => (window as any).__TEST_MAP__.setZoom(z), zoom);
      await expect
        .poll(async () =>
          page.evaluate(() => (window as any).__TEST_MAP__.getZoom()),
        )
        .toBeCloseTo(zoom, 5);
      const sample = await woven.evaluate((root) => {
        const armWidthToken = root.style
          .getPropertyValue("--weave-arm-width")
          .trim();
        const armWidthPx = Array.from(
          root.querySelectorAll<HTMLElement>(".woven-node__arm"),
        ).map((arm) => Number.parseFloat(getComputedStyle(arm).width));
        return { armWidthToken, armWidthPx };
      });
      // Production layer id: LAYERS.EDGES_KNOTTING_LAYER = "edges-knotting-layer"
      const knottingLineWidth = await page.evaluate(() => {
        const map = (window as any).__TEST_MAP__;
        if (!map || typeof map.getPaintProperty !== "function") {
          return null;
        }
        try {
          return map.getPaintProperty("edges-knotting-layer", "line-width");
        } catch {
          return null;
        }
      });
      expect(
        typeof knottingLineWidth === "number" &&
          Number.isFinite(knottingLineWidth),
        `MapLibre edges-knotting-layer line-width must be a finite number at zoom ${zoom}; got ${JSON.stringify(knottingLineWidth)}`,
      ).toBe(true);
      zoomWidthSamples.push({
        zoom,
        armWidthToken: sample.armWidthToken,
        armWidthPx: sample.armWidthPx,
        knottingLineWidth: knottingLineWidth as number,
      });
    }
    expect(zoomWidthSamples).toHaveLength(3);
    const baselineToken = zoomWidthSamples[0].armWidthToken;
    expect(baselineToken).toMatch(/^[\d.]+px$/);
    const tokenPx = parsePx(baselineToken);
    expect(Number.isFinite(tokenPx)).toBe(true);
    expect(tokenPx).toBeGreaterThan(0);
    const baselineArmPx = zoomWidthSamples[0].armWidthPx[0];
    for (const sample of zoomWidthSamples) {
      // Token itself must not drift with zoom (style property, not layout).
      expect(sample.armWidthToken).toBe(baselineToken);
      expect(sample.armWidthPx).toHaveLength(4);
      // All four arms share one computed width within a zoom.
      for (const width of sample.armWidthPx) {
        expect(width).toBeCloseTo(sample.armWidthPx[0], 5);
      }
      // Cross-zoom continuity of the laid-out arm width.
      expect(sample.armWidthPx[0]).toBeCloseTo(baselineArmPx, 5);
      // Computed arm width tracks the shared token (allow subpixel rounding).
      expect(sample.armWidthPx[0]).toBeCloseTo(tokenPx, 1);
      // MapLibre knotting line-width must match the shared DOM arm-width token.
      expect(sample.knottingLineWidth).toBeCloseTo(tokenPx, 5);
    }

    await page.evaluate(() => (window as any).__TEST_MAP__.setZoom(13.4));
    await expect(woven).toHaveAttribute("data-weave-detail", "compact");
    await expect(woven.locator('[data-zone="vote"]').first()).toBeHidden();
    await capture("desktop-zoom-13.4-compact");

    await page.evaluate(() => (window as any).__TEST_MAP__.setZoom(13.6));
    await expect(woven).toHaveAttribute("data-weave-detail", "detail");
    await expect(woven.locator('[data-zone="vote"]').first()).toBeVisible();
    await capture("desktop-zoom-13.6-detail");

    await marker.focus();
    await expect(marker).toBeFocused();
    await capture("desktop-focus");

    await marker.click();
    await expect(marker).toHaveClass(/is-selected/);
    await expect(marker).toHaveAttribute("data-selected", "true");
    const selectedArmColors = await woven.evaluate((root) =>
      Array.from(root.querySelectorAll<HTMLElement>(".woven-node__arm")).map(
        (arm) => arm.style.getPropertyValue("--arm-color").trim(),
      ),
    );
    expect(selectedArmColors).toEqual(armColors);
    await capture("desktop-selected");

    // Drive the real T006 search path with a fixture that returns this node.
    await page.route("**/api/search*", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          items: [
            {
              id: denseNode.id,
              kind: denseNode.kind,
              title: denseNode.title,
              created_at: denseNode.created_at,
              updated_at: denseNode.created_at,
              tags: denseNode.tags,
              location: {
                lat: denseNode.location.lat,
                lon: denseNode.location.lon,
              },
            },
          ],
          mode: "mock",
          generation_id: "pr-1685-dense-search",
          offset: 0,
        }),
      });
    });
    await activateToolFanAction(page, "find");
    const searchInput = page.locator(".search-box input");
    await expect(searchInput).toBeVisible();
    await searchInput.fill(denseNode.title);
    await expect(marker).toHaveAttribute("data-search-match", "true", {
      timeout: 10_000,
    });
    await expect(marker.locator(".map-marker__halo")).toHaveCSS("opacity", "1");
    await capture("desktop-search-halo");

    await page.emulateMedia({ colorScheme: "dark" });
    await capture("desktop-dark");
    await page.emulateMedia({ colorScheme: "light" });
    await capture("desktop-light");

    await page.setViewportSize({ width: 820, height: 1024 });
    await expect(woven).toBeVisible();
    await capture("tablet-detail");

    await page.setViewportSize({ width: 390, height: 844 });
    await expect(woven).toBeVisible();
    await expect(marker).toBeVisible();
    const mobileTouch = await marker.evaluate((element) => {
      const box = element.getBoundingClientRect();
      return { width: box.width, height: box.height };
    });
    expect(mobileTouch.width).toBeGreaterThanOrEqual(44);
    expect(mobileTouch.height).toBeGreaterThanOrEqual(44);
    await capture("mobile-narrow");

    // Geographic center anchor: marker, visual and stitched X share one midpoint.
    // There is no separate crossing element — the X body is the knot.
    const anchor = await marker.evaluate((element) => {
      const visual = element.querySelector(
        ".map-marker__visual",
      ) as HTMLElement | null;
      const xCore = element.querySelector(
        ".woven-node__x",
      ) as HTMLElement | null;
      if (!visual || !xCore) {
        throw new Error("marker visual or woven X missing");
      }
      const markerBox = element.getBoundingClientRect();
      const visualBox = visual.getBoundingClientRect();
      const xBox = xCore.getBoundingClientRect();
      const style = getComputedStyle(visual);
      return {
        transformOrigin: style.transformOrigin,
        markerCx: markerBox.left + markerBox.width / 2,
        markerCy: markerBox.top + markerBox.height / 2,
        visualCx: visualBox.left + visualBox.width / 2,
        visualCy: visualBox.top + visualBox.height / 2,
        xCx: xBox.left + xBox.width / 2,
        xCy: xBox.top + xBox.height / 2,
      };
    });
    expect(Math.abs(anchor.visualCx - anchor.markerCx)).toBeLessThanOrEqual(2);
    expect(Math.abs(anchor.visualCy - anchor.markerCy)).toBeLessThanOrEqual(2);
    expect(Math.abs(anchor.xCx - anchor.markerCx)).toBeLessThanOrEqual(3);
    expect(Math.abs(anchor.xCy - anchor.markerCy)).toBeLessThanOrEqual(3);
    // transform-origin is center: keyword, percent, or resolved px near mid-box.
    const originParts = anchor.transformOrigin
      .split(/\s+/)
      .map((part) => Number.parseFloat(part))
      .filter((value) => Number.isFinite(value));
    const originLooksCentered =
      anchor.transformOrigin.includes("center") ||
      anchor.transformOrigin.includes("50%") ||
      (originParts.length >= 2 &&
        originParts[0] > 15 &&
        originParts[0] < 35 &&
        originParts[1] > 15 &&
        originParts[1] < 35);
    expect(originLooksCentered).toBe(true);
  });
});
