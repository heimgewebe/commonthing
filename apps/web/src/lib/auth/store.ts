import { get, writable } from "svelte/store";
import { browser } from "$app/environment";
import {
  sensitiveSession,
  type SensitiveSessionGuard,
} from "./sensitiveSession";

export type AuthState =
  | "checking"
  | "authenticated"
  | "unauthenticated"
  | "degraded";
export type AuthRole = "gast" | "weber" | "admin";
export interface AuthStatus {
  state: AuthState;
  authenticated: boolean;
  account_id?: string;
  role: AuthRole;
}

export interface AuthCheckOptions {
  force?: boolean;
}

export interface AuthStoreOptions {
  isBrowser?: boolean;
  fetcher?: typeof fetch;
  authCheckTimeoutMs?: number;
  sensitiveSessionGuard?: SensitiveSessionGuard;
}

const anonymous = (state: AuthState = "unauthenticated"): AuthStatus => ({
  state,
  authenticated: false,
  role: "gast",
});

export const createAuthStore = (options: AuthStoreOptions = {}) => {
  const isBrowser = options.isBrowser ?? browser;
  const fetcher = options.fetcher ?? fetch;
  const authCheckTimeoutMs = options.authCheckTimeoutMs ?? 5000;
  const sessionGuard = options.sensitiveSessionGuard ?? sensitiveSession;
  const manageSensitiveSession =
    options.sensitiveSessionGuard !== undefined || browser;
  const store = writable<AuthStatus>(
    anonymous(isBrowser ? "checking" : "unauthenticated"),
  );
  const { subscribe, set } = store;
  let revision = 0;
  let controller: AbortController | undefined;
  let pending: Promise<AuthStatus> | undefined;

  const publish = (next: AuthStatus, authoritative = false): AuthStatus => {
    if (authoritative && isBrowser && manageSensitiveSession) {
      try {
        const storageReady = sessionGuard.bindAuthoritativeAccount(
          next.authenticated && next.account_id ? next.account_id : null,
        );
        if (!storageReady) {
          console.warn(
            "Sensitive session storage unavailable; private draft access remains blocked.",
          );
        }
      } catch {
        // Browser-local cleanup is subordinate to the validated server auth
        // response. Never let storage failure rewrite authenticated truth.
        console.warn(
          "Sensitive session cleanup failed; private draft access remains blocked.",
        );
      }
    }
    set(next);
    return next;
  };

  const supersedePending = (): number => {
    revision += 1;
    controller?.abort();
    controller = undefined;
    pending = undefined;
    return revision;
  };

  const checkAuth = (options: AuthCheckOptions = {}): Promise<AuthStatus> => {
    if (!isBrowser) return Promise.resolve(get(store));
    if (pending && !options.force) return pending;
    if (pending) supersedePending();

    const previous = get(store);
    const current = ++revision;
    const requestController = new AbortController();
    let timeout!: ReturnType<typeof setTimeout>;
    controller = requestController;
    publish({ ...previous, state: "checking" });

    pending = (async () => {
      try {
        const operation = import("./check").then((module) =>
          module.fetchAuthStatus(fetcher, requestController.signal),
        );
        const timedOut = new Promise<never>((_resolve, reject) => {
          timeout = setTimeout(() => {
            requestController.abort();
            reject(new Error());
          }, authCheckTimeoutMs);
        });
        const next = await Promise.race([operation, timedOut]);
        if (current !== revision) return get(store);
        return next
          ? publish(next, true)
          : publish({ ...previous, state: "degraded" });
      } catch {
        if (current !== revision) return get(store);
        return publish({ ...previous, state: "degraded" });
      } finally {
        clearTimeout(timeout);
        if (current === revision) {
          controller = undefined;
          pending = undefined;
        }
      }
    })();
    return pending;
  };

  return {
    subscribe,
    checkAuth,
    devLogin: async (accountId: string) => {
      if (!isBrowser) return;
      await (await import("./actions")).devLogin(fetcher, accountId);
      const next = await checkAuth({ force: true });
      if (!next.authenticated) {
        throw new Error("Login invalid");
      }
    },
    requestLogin: async (email: string) => {
      if (!isBrowser) return;
      await (await import("./actions")).requestLogin(fetcher, email);
    },
    logout: async () => {
      if (!isBrowser) return;
      const previous = get(store);
      const ownedRevision = supersedePending();
      await (
        await import("./actions")
      ).logoutAndVerify(
        fetcher,
        previous,
        ownedRevision,
        () => revision,
        (status) => publish({ ...status, state: "degraded" }),
        () => checkAuth({ force: true }),
      );
    },
  };
};

export const authStore = createAuthStore();

if (browser) void authStore.checkAuth();
