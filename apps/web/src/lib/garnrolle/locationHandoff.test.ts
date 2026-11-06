import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Writable } from "svelte/store";
import { authStore, type AuthStatus } from "$lib/auth/store";
import {
  clearGarnrolleHandoff,
  readGarnrolleDraft,
  rememberGarnrolleDraft,
  rememberGarnrolleLocation,
  takeGarnrolleLocation,
} from "./locationHandoff";

vi.mock("$app/environment", () => ({ browser: true }));
vi.mock("$lib/auth/store", async () => {
  const { writable } = await import("svelte/store");
  return {
    authStore: writable({
      authenticated: false,
      account_id: undefined,
      role: "gast",
      state: "unauthenticated",
    }),
  };
});

const mutableAuth = authStore as unknown as Writable<AuthStatus>;

function signIn(accountId: string) {
  mutableAuth.set({
    authenticated: true,
    account_id: accountId,
    role: "gast",
    state: "authenticated",
  });
}

describe("Garnrolle in-memory handoff", () => {
  beforeEach(() => {
    mutableAuth.set({
      authenticated: false,
      role: "gast",
      state: "unauthenticated",
    });
    signIn("account-a");
    clearGarnrolleHandoff("account-a");
  });

  it("keeps a draft and a newly selected coordinate only within the authenticated account", () => {
    expect(rememberGarnrolleDraft("account-a", '{"title":"private"}')).toBe(
      true,
    );
    expect(readGarnrolleDraft("account-a")).toBe('{"title":"private"}');
    expect(readGarnrolleDraft("account-b")).toBeNull();
    expect(
      rememberGarnrolleLocation("account-a", { lat: 53.5, lon: 10.1 }),
    ).toBe(true);
    expect(takeGarnrolleLocation("account-a")).toEqual({
      lat: 53.5,
      lon: 10.1,
    });
    expect(takeGarnrolleLocation("account-a")).toBeNull();
  });

  it("forbids location transfer without a same-account initiated draft", () => {
    expect(
      rememberGarnrolleLocation("account-a", { lat: 53.5, lon: 10.1 }),
    ).toBe(false);
    expect(rememberGarnrolleDraft("account-b", "other")).toBe(false);
  });

  it("never lets the next authenticated account read an old private draft", () => {
    expect(rememberGarnrolleDraft("account-a", "private-a")).toBe(true);
    signIn("account-b");
    expect(readGarnrolleDraft("account-a")).toBeNull();
    expect(readGarnrolleDraft("account-b")).toBeNull();
    signIn("account-a");
    expect(readGarnrolleDraft("account-a")).toBeNull();
  });

  it("forgets pending sensitive state upon logout", () => {
    rememberGarnrolleDraft("account-a", "private");
    mutableAuth.set({
      authenticated: false,
      role: "gast",
      state: "unauthenticated",
    });
    signIn("account-a");
    expect(readGarnrolleDraft("account-a")).toBeNull();
  });

  it("rejects invalid and out-of-range locations", () => {
    rememberGarnrolleDraft("account-a", "private");
    expect(
      rememberGarnrolleLocation("account-a", { lat: Number.NaN, lon: 10 }),
    ).toBe(false);
    expect(rememberGarnrolleLocation("account-a", { lat: 91, lon: 10 })).toBe(
      false,
    );
    expect(rememberGarnrolleLocation("account-a", { lat: 50, lon: -181 })).toBe(
      false,
    );
    expect(takeGarnrolleLocation("account-a")).toBeNull();
  });

  it("clears pending private state on the successful save boundary", () => {
    rememberGarnrolleDraft("account-a", "private");
    rememberGarnrolleLocation("account-a", { lat: 53.5, lon: 10 });
    clearGarnrolleHandoff("account-a");
    expect(readGarnrolleDraft("account-a")).toBeNull();
    expect(takeGarnrolleLocation("account-a")).toBeNull();
  });
});
