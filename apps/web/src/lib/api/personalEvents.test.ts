import { afterEach, describe, expect, it, vi } from "vitest";
import { listPersonalEvents, markPersonalEventRead } from "./personalEvents";

describe("personal event message client", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("loads a private paginated account inbox without caching", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          items: [
            {
              id: "event-a",
              event_kind: "circle_invited",
              content: "Einladung",
              created_at: "2026-11-12T12:00:00Z",
              read_at: null,
              circle_id: "circle-1",
              circle_name: "Haus",
              target_account_id: null,
              actionable: true,
            },
          ],
          unread_count: 1,
          has_more: true,
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    const page = await listPersonalEvents(undefined, "event-before");
    expect(page.unread_count).toBe(1);
    expect(page.items[0].actionable).toBe(true);
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/personal-events?before=event-before",
      expect.objectContaining({ credentials: "include", cache: "no-store" }),
    );
  });

  it("marks only one explicitly selected message as read", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetchMock);
    await markPersonalEventRead("event-123");
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/personal-events/event-123/read",
      expect.objectContaining({
        credentials: "include",
        cache: "no-store",
        method: "POST",
      }),
    );
  });

  it("does not mistake a forbidden or missing inbox for an empty one", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response(null, { status: 404 })),
    );
    await expect(listPersonalEvents()).rejects.toThrow("404");
  });
});
