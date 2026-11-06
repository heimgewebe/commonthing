import { afterEach, describe, expect, it, vi } from "vitest";
import {
  acceptCircle,
  createHousehold,
  inviteToCircle,
  leaveCircle,
  listCircleMembers,
  listMyCircles,
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
      "/api/circles/me?offset=0",
      expect.objectContaining({
        credentials: "include",
        cache: "no-store",
      }),
    );
  });

  it("paginates beyond 100 invitations so old households remain accessible", async () => {
    const newer = Array.from({ length: 100 }, (_, i) => ({
      id: `new-${i}`,
      kind: "household",
      name: "Einladung",
      state: "forming",
      membership_status: "invited",
    }));
    const oldActive = {
      id: "old-active",
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
      "/api/circles/me?offset=0",
      "/api/circles/me?offset=100",
    ]);
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
    await acceptCircle("house-1");
    await inviteToCircle("house-1", "neighbor");
    await leaveCircle("house-1");
    expect(
      fetchMock.mock.calls.map(([path, init]) => [path, init.method]),
    ).toEqual([
      ["/api/circles/house-1/accept", "POST"],
      ["/api/circles/house-1/invitations", "POST"],
      ["/api/circles/house-1/membership", "DELETE"],
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
});
