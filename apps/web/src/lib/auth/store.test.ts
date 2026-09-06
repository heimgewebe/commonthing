import { get } from "svelte/store";
import { describe, expect, it, vi } from "vitest";

vi.mock("$app/environment", () => ({ browser: false }));

import { createAuthStore } from "./store";
import type { SensitiveSessionGuard } from "./sensitiveSession";

function authResponse(accountId = "account-a"): Response {
  return new Response(
    JSON.stringify({
      authenticated: true,
      account_id: accountId,
      role: "weber",
    }),
    { status: 200, headers: { "Content-Type": "application/json" } },
  );
}

function guestResponse(status = 200): Response {
  return new Response(
    JSON.stringify({ authenticated: false, account_id: null, role: "gast" }),
    { status, headers: { "Content-Type": "application/json" } },
  );
}

function storageGuard(
  bindAuthoritativeAccount: SensitiveSessionGuard["bindAuthoritativeAccount"],
): SensitiveSessionGuard {
  return {
    bindAuthoritativeAccount,
    read: () => null,
    write: () => false,
    remove: () => false,
    take: () => null,
  };
}

describe("authStore", () => {
  it("deduplicates parallel auth checks", async () => {
    let resolveFetch!: (response: Response) => void;
    const fetcher = vi.fn(
      () =>
        new Promise<Response>((resolve) => {
          resolveFetch = resolve;
        }),
    );
    const store = createAuthStore({ isBrowser: true, fetcher });
    const first = store.checkAuth();
    const second = store.checkAuth();
    await vi.waitFor(() => expect(fetcher).toHaveBeenCalledTimes(1));
    resolveFetch(authResponse());
    await expect(first).resolves.toMatchObject({
      state: "authenticated",
      authenticated: true,
    });
    await expect(second).resolves.toMatchObject({
      state: "authenticated",
      authenticated: true,
    });
  });

  it("preserves the authenticated identity when the server is unavailable", async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(authResponse("account-preserved"))
      .mockResolvedValueOnce(new Response("unavailable", { status: 503 }));
    const store = createAuthStore({ isBrowser: true, fetcher });
    await store.checkAuth();
    await store.checkAuth();
    expect(get(store)).toEqual({
      state: "degraded",
      authenticated: true,
      account_id: "account-preserved",
      role: "weber",
    });
  });

  it("clears identity after a validated guest payload", async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(authResponse())
      .mockResolvedValueOnce(guestResponse());
    const store = createAuthStore({ isBrowser: true, fetcher });
    await store.checkAuth();
    await store.checkAuth();
    expect(get(store)).toEqual({
      state: "unauthenticated",
      authenticated: false,
      account_id: undefined,
      role: "gast",
    });
  });

  it("accepts a validated rolling-deployment 401 guest response", async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(authResponse())
      .mockResolvedValueOnce(guestResponse(401));
    const store = createAuthStore({ isBrowser: true, fetcher });
    await store.checkAuth();
    await store.checkAuth();
    expect(get(store)).toMatchObject({
      state: "unauthenticated",
      authenticated: false,
    });
  });

  it("does not treat a naked 403 as proof of logout", async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(authResponse("account-preserved"))
      .mockResolvedValueOnce(new Response(null, { status: 403 }));
    const store = createAuthStore({ isBrowser: true, fetcher });
    await store.checkAuth();
    await store.checkAuth();
    expect(get(store)).toEqual({
      state: "degraded",
      authenticated: true,
      account_id: "account-preserved",
      role: "weber",
    });
  });

  it("times out a stalled check into degraded state", async () => {
    const fetcher = vi.fn<typeof fetch>().mockImplementation((_input, init) => {
      return new Promise<Response>((_resolve, reject) => {
        init?.signal?.addEventListener("abort", () => {
          reject(new DOMException("aborted", "AbortError"));
        });
      });
    });
    const store = createAuthStore({
      isBrowser: true,
      fetcher,
      authCheckTimeoutMs: 5,
    });
    await expect(store.checkAuth()).resolves.toMatchObject({
      state: "degraded",
      authenticated: false,
    });
  });

  it("accepts authenticated guest accounts as authoritative identities", async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          authenticated: true,
          account_id: "guest-account",
          role: "gast",
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    const store = createAuthStore({ isBrowser: true, fetcher });
    await store.checkAuth();
    expect(get(store)).toEqual({
      state: "authenticated",
      authenticated: true,
      account_id: "guest-account",
      role: "gast",
    });
  });

  it("rejects malformed authenticated roles as non-authoritative", async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(authResponse("account-preserved"))
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            authenticated: true,
            account_id: "attacker-controlled",
            role: "bogus",
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      );
    const store = createAuthStore({ isBrowser: true, fetcher });
    await store.checkAuth();
    await store.checkAuth();
    expect(get(store)).toEqual({
      state: "degraded",
      authenticated: true,
      account_id: "account-preserved",
      role: "weber",
    });
  });

  it("can cancel and replace an in-flight auth check without stale writes", async () => {
    let firstSignal: AbortSignal | undefined;
    const fetcher = vi
      .fn<typeof fetch>()
      .mockImplementationOnce((_input, init) => {
        firstSignal = init?.signal ?? undefined;
        return new Promise<Response>((_resolve, reject) => {
          firstSignal?.addEventListener("abort", () => {
            reject(new DOMException("aborted", "AbortError"));
          });
        });
      })
      .mockResolvedValueOnce(authResponse("latest-account"));
    const store = createAuthStore({ isBrowser: true, fetcher });
    const stale = store.checkAuth();
    await vi.waitFor(() => expect(fetcher).toHaveBeenCalledTimes(1));
    const latest = store.checkAuth({ force: true });
    await expect(latest).resolves.toMatchObject({
      state: "authenticated",
      account_id: "latest-account",
    });
    await stale;
    expect(firstSignal?.aborted).toBe(true);
    expect(get(store)).toMatchObject({
      state: "authenticated",
      account_id: "latest-account",
    });
  });

  it("propagates logout failure and preserves identity as degraded", async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(authResponse("account-preserved"))
      .mockResolvedValueOnce(new Response("unavailable", { status: 503 }));
    const store = createAuthStore({ isBrowser: true, fetcher });
    await store.checkAuth();
    await expect(store.logout()).rejects.toThrow("Logout failed: 503");
    expect(get(store)).toEqual({
      state: "degraded",
      authenticated: true,
      account_id: "account-preserved",
      role: "weber",
    });
  });

  it("verifies logout after a concurrent auth check observed the old session", async () => {
    let resolveLogout!: (response: Response) => void;
    let authChecks = 0;
    const fetcher = vi.fn<typeof fetch>().mockImplementation((input) => {
      const url = String(input);
      if (url.endsWith("/api/auth/me")) {
        authChecks += 1;
        return Promise.resolve(
          authChecks < 3 ? authResponse("account-a") : guestResponse(),
        );
      }
      if (url.endsWith("/api/auth/logout")) {
        return new Promise<Response>((resolve) => {
          resolveLogout = resolve;
        });
      }
      throw new Error(`unexpected request: ${url}`);
    });
    const store = createAuthStore({ isBrowser: true, fetcher });
    await store.checkAuth();

    const logout = store.logout();
    await vi.waitFor(() => expect(resolveLogout).toBeTypeOf("function"));
    await expect(store.checkAuth({ force: true })).resolves.toMatchObject({
      state: "authenticated",
      account_id: "account-a",
    });
    resolveLogout(new Response(null, { status: 200 }));
    await expect(logout).resolves.toBeUndefined();
    expect(authChecks).toBe(3);
    expect(get(store)).toMatchObject({
      state: "unauthenticated",
      authenticated: false,
      role: "gast",
    });
  });

  it("requires server verification before completing logout", async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(authResponse("account-preserved"))
      .mockResolvedValueOnce(new Response(null, { status: 200 }))
      .mockResolvedValueOnce(new Response("unavailable", { status: 503 }));
    const store = createAuthStore({ isBrowser: true, fetcher });
    await store.checkAuth();
    await expect(store.logout()).rejects.toThrow("Logout unverified");
    expect(get(store)).toMatchObject({
      state: "degraded",
      authenticated: true,
      account_id: "account-preserved",
    });
  });

  it("completes logout only after a validated guest response", async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(authResponse())
      .mockResolvedValueOnce(new Response(null, { status: 200 }))
      .mockResolvedValueOnce(guestResponse());
    const store = createAuthStore({ isBrowser: true, fetcher });
    await store.checkAuth();
    await expect(store.logout()).resolves.toBeUndefined();
    expect(get(store)).toMatchObject({
      state: "unauthenticated",
      authenticated: false,
    });
  });

  it("keeps validated authenticated truth when sensitive storage cleanup throws", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(authResponse("account-storage-blocked"));
    const sessionGuard = storageGuard(() => {
      throw new DOMException("blocked", "SecurityError");
    });
    const store = createAuthStore({
      isBrowser: true,
      fetcher,
      sensitiveSessionGuard: sessionGuard,
    });

    await expect(store.checkAuth()).resolves.toEqual({
      state: "authenticated",
      authenticated: true,
      account_id: "account-storage-blocked",
      role: "weber",
    });
    expect(get(store)).toMatchObject({
      state: "authenticated",
      authenticated: true,
      account_id: "account-storage-blocked",
    });
    expect(warn).toHaveBeenCalledOnce();
    warn.mockRestore();
  });

  it("accepts validated logout truth even while sensitive cleanup stays blocked", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(authResponse("account-before-storage-failure"))
      .mockResolvedValueOnce(guestResponse());
    const sessionGuard = storageGuard(() => false);
    const store = createAuthStore({
      isBrowser: true,
      fetcher,
      sensitiveSessionGuard: sessionGuard,
    });

    await store.checkAuth();
    await store.checkAuth();
    expect(get(store)).toEqual({
      state: "unauthenticated",
      authenticated: false,
      account_id: undefined,
      role: "gast",
    });
    expect(warn).toHaveBeenCalledTimes(2);
    warn.mockRestore();
  });
});
