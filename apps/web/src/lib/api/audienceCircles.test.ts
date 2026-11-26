import { afterEach, describe, expect, it, vi } from "vitest";
import {
  acceptCircle,
  declineCircle,
  createHousehold,
  inviteToCircle,
  leaveCircle,
  listCircleMembers,
  listMyCircles,
  withdrawCircleInvitation,
  AudienceCircleError,
} from "./audienceCircles";

afterEach(() => vi.unstubAllGlobals());

describe("voluntary audience circle API", () => {
  it("lists only the server-authorized actor's circles without caching", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(new Response(JSON.stringify([]), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    await expect(listMyCircles()).resolves.toEqual([]);
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/circles/me",
      expect.objectContaining({
        credentials: "include",
        cache: "no-store",
      }),
    );
  });

  it("paginates beyond 100 invitations so old households remain accessible", async () => {
    const newer = Array.from({ length: 100 }, (_, i) => ({
      id: `00000000-0000-4000-8000-${String(i).padStart(12, "0")}`,
      kind: "household",
      name: "Einladung",
      state: "forming",
      membership_status: "invited",
    }));
    const oldActive = {
      id: "00000000-0000-4000-8000-000000000100",
      kind: "household",
      name: "Meine Hausgemeinschaft",
      state: "active",
      membership_status: "active",
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify(newer), { status: 200 }),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify([oldActive]), { status: 200 }),
      );
    vi.stubGlobal("fetch", fetchMock);
    const circles = await listMyCircles();
    expect(circles).toHaveLength(101);
    expect(circles).toContainEqual(oldActive);
    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      "/api/circles/me",
      `/api/circles/me?after=${newer[99].id}`,
    ]);
  });

  it("deduplicates overlapping page results during concurrent invitation changes", async () => {
    const firstPage = Array.from({ length: 100 }, (_, i) => ({
      id: `00000000-0000-4000-8000-${String(i).padStart(12, "0")}`,
      kind: "household",
      name: "Hausgemeinschaft",
      state: "active",
      membership_status: "active",
    }));
    const oldCircle = {
      id: "00000000-0000-4000-8000-000000000101",
      kind: "household",
      name: "Älterer Kreis",
      state: "active",
      membership_status: "active",
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify(firstPage), { status: 200 }),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify([firstPage[99], oldCircle]), {
          status: 200,
        }),
      );
    vi.stubGlobal("fetch", fetchMock);
    const circles = await listMyCircles();
    expect(circles).toHaveLength(101);
    expect(new Set(circles.map((circle) => circle.id)).size).toBe(101);
    expect(circles).toContainEqual(oldCircle);
    expect(fetchMock.mock.calls[1][0]).toBe(
      `/api/circles/me?after=${firstPage[99].id}`,
    );
  });

  it("never infers membership from location; a founder explicitly invites one account", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ id: "circle-a", state: "forming" }), {
        status: 201,
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    await createHousehold("neighbor");
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/circles",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          kind: "household",
          invitee_account_id: "neighbor",
        }),
      }),
    );
  });

  it("accepts and invites explicitly; departure uses authenticated DELETE", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ id: "house-1", state: "active" }), {
          status: 200,
        }),
      )
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
      .mockResolvedValueOnce(new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetchMock);
    await acceptCircle("house-1", "2026-11-12T12:00:00Z");
    await inviteToCircle("house-1", "neighbor");
    await leaveCircle("house-1");
    expect(
      fetchMock.mock.calls.map(([path, init]) => [path, init.method]),
    ).toEqual([
      [
        "/api/circles/house-1/accept?expected_invitation_created_at=2026-11-12T12%3A00%3A00Z",
        "POST",
      ],
      ["/api/circles/house-1/invitations", "POST"],
      ["/api/circles/house-1/membership", "DELETE"],
    ]);
  });

  it("binds decline and withdrawal to the exact invitation round", async () => {
    const at = "2026-11-12T12:00:00Z";
    const fetchMock = vi
      .fn()
      .mockResolvedValue(new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetchMock);
    await declineCircle("house-1", at);
    await withdrawCircleInvitation("house-1", "neighbor", at);
    expect(
      fetchMock.mock.calls.map(([path, init]) => [path, init.method]),
    ).toEqual([
      [
        "/api/circles/house-1/membership?expected_status=invited&expected_invitation_created_at=2026-11-12T12%3A00%3A00Z",
        "DELETE",
      ],
      [
        "/api/circles/house-1/invitations/neighbor?expected_invitation_created_at=2026-11-12T12%3A00%3A00Z",
        "DELETE",
      ],
    ]);
  });

  it("member lists require server validation; 404 never reveals membership names", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(new Response(null, { status: 404 }));
    vi.stubGlobal("fetch", fetchMock);
    await expect(listCircleMembers("not-my-circle")).rejects.toBeInstanceOf(
      AudienceCircleError,
    );
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/circles/not-my-circle/members",
      expect.objectContaining({ credentials: "include" }),
    );
  });

  it("reports an unknown invitation code instead of claiming the service is unavailable", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response(null, { status: 404 })),
    );
    await expect(createHousehold("missing-account")).rejects.toThrow(
      "Garnrollen-Kennung wurde nicht gefunden",
    );
  });

  it("reports an existing household without pretending a new one was created", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response(null, { status: 409 })),
    );
    await expect(createHousehold("neighbor")).rejects.toThrow(
      "bereits eine Hausgemeinschaft oder einen offenen Vorschlag",
    );
  });

  it("reports an already invited member instead of a second sent invitation", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response(null, { status: 409 })),
    );
    await expect(inviteToCircle("house-1", "neighbor")).rejects.toThrow(
      "bereits eingeladen oder Mitglied",
    );
  });

  it("explains the invitation rate limit instead of a generic failure", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response(null, { status: 429 })),
    );
    await expect(inviteToCircle("house-1", "neighbor")).rejects.toThrow(
      "Zu viele Einladungen",
    );
    await expect(createHousehold("neighbor")).rejects.toMatchObject({
      status: 429,
    });
  });
});
