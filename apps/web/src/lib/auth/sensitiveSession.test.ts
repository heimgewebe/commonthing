import { describe, expect, it } from "vitest";
import {
  createSensitiveSessionGuard,
  type SessionStorageLike,
} from "./sensitiveSession";

class MemoryStorage implements SessionStorageLike {
  private readonly values = new Map<string, string>();
  throwOnGet = false;
  throwOnSet = false;
  throwOnRemove = false;

  get length() {
    return this.values.size;
  }

  key(index: number): string | null {
    return Array.from(this.values.keys())[index] ?? null;
  }

  getItem(key: string): string | null {
    if (this.throwOnGet) throw new DOMException("blocked", "SecurityError");
    return this.values.get(key) ?? null;
  }

  setItem(key: string, value: string): void {
    if (this.throwOnSet) throw new DOMException("blocked", "SecurityError");
    this.values.set(key, value);
  }

  removeItem(key: string): void {
    if (this.throwOnRemove) throw new DOMException("blocked", "SecurityError");
    this.values.delete(key);
  }

  seed(key: string, value: string) {
    this.values.set(key, value);
  }

  peek(key: string): string | null {
    return this.values.get(key) ?? null;
  }
}

const draftKey = (accountId: string) =>
  `weltgewebe:garnrolle-draft:${accountId}`;
const returnKey = (accountId: string) =>
  `weltgewebe:garnrolle-return-location:${accountId}`;

describe("sensitiveSession", () => {
  it("preserves the authoritative account draft and clears other-account data on first bind", () => {
    const storage = new MemoryStorage();
    storage.seed(draftKey("account-a"), "current-a");
    storage.seed(returnKey("account-b"), "stale-b");
    storage.seed("weltgewebe:unrelated", "keep");
    const guard = createSensitiveSessionGuard(() => storage);

    expect(guard.bindAuthoritativeAccount("account-a")).toBe(true);
    expect(guard.read("account-a", draftKey("account-a"))).toBe("current-a");
    expect(storage.peek(returnKey("account-b"))).toBeNull();
    expect(storage.peek("weltgewebe:unrelated")).toBe("keep");
  });

  it("preserves the current account draft across same-runtime navigation", () => {
    const storage = new MemoryStorage();
    const guard = createSensitiveSessionGuard(() => storage);

    expect(guard.bindAuthoritativeAccount("account-a")).toBe(true);
    expect(guard.write("account-a", draftKey("account-a"), "draft-a")).toBe(
      true,
    );
    expect(guard.bindAuthoritativeAccount("account-a")).toBe(true);
    expect(guard.read("account-a", draftKey("account-a"))).toBe("draft-a");
  });

  it("clears sensitive data and rejects old-account access on account switch", () => {
    const storage = new MemoryStorage();
    const guard = createSensitiveSessionGuard(() => storage);

    guard.bindAuthoritativeAccount("account-a");
    guard.write("account-a", draftKey("account-a"), "draft-a");

    expect(guard.bindAuthoritativeAccount("account-b")).toBe(true);
    expect(storage.peek(draftKey("account-a"))).toBeNull();
    expect(guard.read("account-a", draftKey("account-a"))).toBeNull();
    expect(guard.write("account-a", draftKey("account-a"), "late-a")).toBe(
      false,
    );
  });

  it("keeps private reads closed until a throwing storage provider recovers", () => {
    const storage = new MemoryStorage();
    storage.seed(draftKey("previous-account"), "stale-private");
    let unavailable = true;
    const guard = createSensitiveSessionGuard(() => {
      if (unavailable) throw new DOMException("blocked", "SecurityError");
      return storage;
    });

    expect(guard.bindAuthoritativeAccount("account-a")).toBe(false);
    unavailable = false;

    // Recovery must perform the pending cross-account cleanup before storage
    // can be used for the authoritative account.
    expect(guard.read("account-a", draftKey("account-a"))).toBeNull();
    expect(storage.peek(draftKey("previous-account"))).toBeNull();
    expect(guard.write("account-a", draftKey("account-a"), "fresh-a")).toBe(
      true,
    );
    expect(guard.read("account-a", draftKey("account-a"))).toBe("fresh-a");
  });

  it("retries cleanup after a failed account-switch removal before exposing the new account", () => {
    const storage = new MemoryStorage();
    const guard = createSensitiveSessionGuard(() => storage);
    guard.bindAuthoritativeAccount("account-a");
    guard.write("account-a", draftKey("account-a"), "draft-a");

    storage.throwOnRemove = true;
    expect(guard.bindAuthoritativeAccount("account-b")).toBe(false);
    expect(guard.read("account-b", draftKey("account-b"))).toBeNull();

    storage.throwOnRemove = false;
    expect(guard.read("account-b", draftKey("account-b"))).toBeNull();
    expect(storage.peek(draftKey("account-a"))).toBeNull();
    expect(guard.write("account-b", draftKey("account-b"), "fresh-b")).toBe(
      true,
    );
  });

  it("preserves the current account draft across a normal JavaScript reload", () => {
    const storage = new MemoryStorage();
    const beforeReload = createSensitiveSessionGuard(() => storage);
    beforeReload.bindAuthoritativeAccount("account-a");
    beforeReload.write("account-a", draftKey("account-a"), "draft-a");

    const afterReload = createSensitiveSessionGuard(() => storage);
    expect(afterReload.bindAuthoritativeAccount("account-a")).toBe(true);
    expect(afterReload.read("account-a", draftKey("account-a"))).toBe(
      "draft-a",
    );
  });

  it("cleans the previous account after a failed switch even across a JavaScript reload", () => {
    const storage = new MemoryStorage();
    const beforeReload = createSensitiveSessionGuard(() => storage);
    beforeReload.bindAuthoritativeAccount("account-a");
    beforeReload.write("account-a", draftKey("account-a"), "draft-a");

    storage.throwOnRemove = true;
    expect(beforeReload.bindAuthoritativeAccount("account-b")).toBe(false);
    storage.throwOnRemove = false;
    storage.seed(draftKey("account-b"), "draft-b");

    const afterReload = createSensitiveSessionGuard(() => storage);
    expect(afterReload.bindAuthoritativeAccount("account-b")).toBe(true);
    expect(storage.peek(draftKey("account-a"))).toBeNull();
    expect(afterReload.read("account-b", draftKey("account-b"))).toBe(
      "draft-b",
    );
  });

  it("retries a failed get before returning private data", () => {
    const storage = new MemoryStorage();
    const guard = createSensitiveSessionGuard(() => storage);
    guard.bindAuthoritativeAccount("account-a");
    guard.write("account-a", draftKey("account-a"), "draft-a");

    storage.throwOnGet = true;
    expect(guard.read("account-a", draftKey("account-a"))).toBeNull();
    storage.throwOnGet = false;
    expect(guard.read("account-a", draftKey("account-a"))).toBe("draft-a");
  });

  it("returns false on a failed write and succeeds after storage recovers", () => {
    const storage = new MemoryStorage();
    const guard = createSensitiveSessionGuard(() => storage);
    guard.bindAuthoritativeAccount("account-a");

    storage.throwOnSet = true;
    expect(guard.write("account-a", draftKey("account-a"), "draft-a")).toBe(
      false,
    );
    storage.throwOnSet = false;
    expect(guard.write("account-a", draftKey("account-a"), "draft-a")).toBe(
      true,
    );
  });

  it("clears all sensitive data on authoritative logout", () => {
    const storage = new MemoryStorage();
    const guard = createSensitiveSessionGuard(() => storage);
    guard.bindAuthoritativeAccount("account-a");
    guard.write("account-a", draftKey("account-a"), "draft-a");
    guard.write("account-a", returnKey("account-a"), "location-a");
    storage.seed("weltgewebe:unrelated", "keep");

    expect(guard.bindAuthoritativeAccount(null)).toBe(true);
    expect(storage.peek(draftKey("account-a"))).toBeNull();
    expect(storage.peek(returnKey("account-a"))).toBeNull();
    expect(storage.peek("weltgewebe:unrelated")).toBe("keep");
  });

  it("returns a one-shot value only when removal also succeeds", () => {
    const storage = new MemoryStorage();
    const guard = createSensitiveSessionGuard(() => storage);
    guard.bindAuthoritativeAccount("account-a");
    guard.write("account-a", returnKey("account-a"), "location");

    storage.throwOnRemove = true;
    expect(guard.take("account-a", returnKey("account-a"))).toBeNull();

    storage.throwOnRemove = false;
    expect(guard.read("account-a", returnKey("account-a"))).toBeNull();
    expect(storage.peek(returnKey("account-a"))).toBeNull();
  });
});
