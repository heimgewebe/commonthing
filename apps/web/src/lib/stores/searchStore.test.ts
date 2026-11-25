import { describe, expect, it, vi } from "vitest";
import { get } from "svelte/store";
import {
  bindDiscoveryHistoryAccount,
  enterDiscoveryHistory,
  rememberDiscoveryHistory,
  onDiscoveryAccountChange,
  searchQuery,
  stageDiscoverySearch,
  takeDiscoverySearch,
} from "./searchStore";

const click = {
  button: 0,
  defaultPrevented: false,
  altKey: false,
  ctrlKey: false,
  metaKey: false,
  shiftKey: false,
};

describe("discovery search handoff", () => {
  it("consumes only the addressed view, exactly once", () => {
    stageDiscoverySearch(click, "commons", "  Werkstatt  ");
    expect(takeDiscoverySearch("map")).toBeNull();
    expect(takeDiscoverySearch("commons")).toBe("Werkstatt");
    expect(takeDiscoverySearch("commons")).toBeNull();
  });

  it("hands back a list search without adding it to the URL", () => {
    stageDiscoverySearch(click, "map", "digitales Wissen");
    expect(takeDiscoverySearch("map")).toBe("digitales Wissen");
    expect(takeDiscoverySearch("map")).toBeNull();
  });

  it("ignores new-tab and cancelled activations", () => {
    for (const modifier of [
      { ctrlKey: true },
      { metaKey: true },
      { shiftKey: true },
      { altKey: true },
      { button: 1 },
      { defaultPrevented: true },
    ]) {
      stageDiscoverySearch(
        { ...click, ...modifier },
        "commons",
        "not this tab",
      );
      expect(takeDiscoverySearch("commons")).toBeNull();
    }
  });

  it("does not retain an old transfer when switching without a query", () => {
    stageDiscoverySearch(click, "commons", "old");
    stageDiscoverySearch(click, "commons", " ");
    expect(takeDiscoverySearch("commons")).toBeNull();
  });
});

describe("transient discovery history", () => {
  it("restores each distinct history entry but never a normal or reloaded entry", () => {
    bindDiscoveryHistoryAccount("history-owner");
    const a = enterDiscoveryHistory("map", undefined, "enter");
    rememberDiscoveryHistory(a.id, "map", "first");
    stageDiscoverySearch(click, "commons", "first");
    const b = enterDiscoveryHistory("commons", undefined, "link");
    expect(b.query).toBe("first");
    rememberDiscoveryHistory(b.id, "commons", "second");
    stageDiscoverySearch(click, "map", "second");
    const c = enterDiscoveryHistory("map", undefined, "link");
    expect(c.query).toBe("second");
    expect(new Set([a.id, b.id, c.id]).size).toBe(3);
    expect(enterDiscoveryHistory("commons", b.id, "popstate")).toEqual({
      id: b.id,
      query: "second",
    });
    expect(enterDiscoveryHistory("map", a.id, "popstate").query).toBe("first");
    expect(enterDiscoveryHistory("map", c.id, "popstate").query).toBe("second");
    expect(enterDiscoveryHistory("commons", b.id, "link").query).toBeNull();
    expect(enterDiscoveryHistory("map", c.id, "enter").query).toBeNull();
    expect(enterDiscoveryHistory("map", b.id, "popstate").query).toBeNull();
  });

  it("drops history and pending handoff on account change without rebinding old entries", () => {
    bindDiscoveryHistoryAccount("owner-a");
    const entry = enterDiscoveryHistory("commons", undefined, "enter");
    rememberDiscoveryHistory(entry.id, "commons", "owner-a private draft");
    stageDiscoverySearch(click, "map", "owner-a private draft");
    bindDiscoveryHistoryAccount("owner-b");
    expect(takeDiscoverySearch("map")).toBeNull();
    rememberDiscoveryHistory(entry.id, "commons", "must not rebind");
    expect(
      enterDiscoveryHistory("commons", entry.id, "popstate").query,
    ).toBeNull();
    bindDiscoveryHistoryAccount("owner-a");
    expect(
      enterDiscoveryHistory("commons", entry.id, "popstate").query,
    ).toBeNull();
  });

  it("notifies mounted views only on a changed authoritative account", () => {
    bindDiscoveryHistoryAccount("notification-owner-a");
    const listener = vi.fn(() => {
      expect(get(searchQuery)).toBe("");
      expect(takeDiscoverySearch("map")).toBeNull();
    });
    const unsubscribe = onDiscoveryAccountChange(listener);
    try {
      searchQuery.set("owner-a draft");
      stageDiscoverySearch(click, "map", "owner-a draft");
      bindDiscoveryHistoryAccount("notification-owner-a");
      expect(listener).not.toHaveBeenCalled();
      expect(get(searchQuery)).toBe("owner-a draft");
      bindDiscoveryHistoryAccount("notification-owner-b");
      expect(listener).toHaveBeenCalledTimes(1);
      bindDiscoveryHistoryAccount("notification-owner-b");
      expect(listener).toHaveBeenCalledTimes(1);
    } finally {
      unsubscribe();
    }
    bindDiscoveryHistoryAccount(null);
    expect(listener).toHaveBeenCalledTimes(1);
  });

  it("does not carry a pending explicit handoff into a history navigation", () => {
    bindDiscoveryHistoryAccount("history-cancel");
    stageDiscoverySearch(click, "commons", "unconsumed");
    expect(
      enterDiscoveryHistory("commons", "unknown", "popstate").query,
    ).toBeNull();
    expect(
      enterDiscoveryHistory("commons", undefined, "link").query,
    ).toBeNull();
  });

  it("bounds history and never recreates evicted entries while leaving", () => {
    bindDiscoveryHistoryAccount("history-capacity");
    const first = enterDiscoveryHistory("map", undefined, "enter");
    rememberDiscoveryHistory(first.id, "map", "old");
    for (let i = 0; i < 64; i++) {
      enterDiscoveryHistory("commons", undefined, "link");
    }
    rememberDiscoveryHistory(first.id, "map", "must not reappear");
    expect(enterDiscoveryHistory("map", first.id, "popstate").query).toBeNull();
  });
});
