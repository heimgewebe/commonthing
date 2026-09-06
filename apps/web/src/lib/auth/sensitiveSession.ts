export const SENSITIVE_SESSION_PREFIXES = [
  "weltgewebe:garnrolle-draft:",
  "weltgewebe:garnrolle-return-location:",
] as const;

export type SessionStorageLike = Pick<
  Storage,
  "length" | "key" | "getItem" | "setItem" | "removeItem"
>;

export type SessionStorageProvider = () => SessionStorageLike | null;

export interface SensitiveSessionGuard {
  bindAuthoritativeAccount(accountId: string | null): boolean;
  read(accountId: string, key: string): string | null;
  write(accountId: string, key: string, value: string): boolean;
  remove(accountId: string, key: string): boolean;
  take(accountId: string, key: string): string | null;
}

function browserSessionStorage(): SessionStorageLike | null {
  try {
    if (typeof window === "undefined") return null;
    return window.sessionStorage;
  } catch {
    return null;
  }
}

function isSensitiveKey(key: string): boolean {
  return SENSITIVE_SESSION_PREFIXES.some((prefix) => key.startsWith(prefix));
}

function isAccountKey(key: string, accountId: string): boolean {
  return SENSITIVE_SESSION_PREFIXES.some(
    (prefix) => key === `${prefix}${accountId}`,
  );
}

/**
 * Guards private sessionStorage data with the authoritative authenticated
 * account observed in this JavaScript runtime.
 *
 * Every new runtime reconciles sensitive keys against the first authoritative
 * auth result before any private read is allowed. The current account's draft
 * remains available across a normal reload, while sensitive keys belonging to
 * every other account are deleted first. Logout clears all sensitive keys.
 * Failed cleanup keeps reads closed until storage recovers and reconciliation
 * succeeds.
 */
export function createSensitiveSessionGuard(
  provideStorage: SessionStorageProvider = browserSessionStorage,
): SensitiveSessionGuard {
  let boundAccountId: string | null | undefined;
  let cleanupRequired = true;
  const blockedKeys = new Set<string>();

  const getStorage = (): SessionStorageLike | null => {
    try {
      return provideStorage();
    } catch {
      cleanupRequired = true;
      return null;
    }
  };

  const clearSensitiveEntries = (
    storage: SessionStorageLike,
    keepAccountId?: string,
  ) => {
    for (let index = storage.length - 1; index >= 0; index -= 1) {
      const key = storage.key(index);
      if (!key || !isSensitiveKey(key)) continue;
      if (keepAccountId && isAccountKey(key, keepAccountId)) continue;
      storage.removeItem(key);
    }
  };

  const clearBlockedKeys = (storage: SessionStorageLike) => {
    for (const key of blockedKeys) storage.removeItem(key);
    blockedKeys.clear();
  };

  const reconcileBoundStorage = (): SessionStorageLike | null => {
    if (boundAccountId === undefined) return null;
    const storage = getStorage();
    if (!storage) {
      cleanupRequired = true;
      return null;
    }

    try {
      if (cleanupRequired) {
        clearSensitiveEntries(storage, boundAccountId ?? undefined);
      }
      if (blockedKeys.size > 0) clearBlockedKeys(storage);
      cleanupRequired = false;
      return storage;
    } catch {
      cleanupRequired = true;
      return null;
    }
  };

  const storageForAccount = (
    accountId: string,
    key: string,
  ): SessionStorageLike | null => {
    if (boundAccountId !== accountId || !isAccountKey(key, accountId)) {
      return null;
    }
    return reconcileBoundStorage();
  };

  return {
    bindAuthoritativeAccount(accountId: string | null): boolean {
      const changed =
        boundAccountId === undefined || boundAccountId !== accountId;
      boundAccountId = accountId;
      if (changed || accountId === null) cleanupRequired = true;
      return reconcileBoundStorage() !== null;
    },

    read(accountId: string, key: string): string | null {
      const storage = storageForAccount(accountId, key);
      if (!storage) return null;
      try {
        return storage.getItem(key);
      } catch {
        cleanupRequired = true;
        return null;
      }
    },

    write(accountId: string, key: string, value: string): boolean {
      const storage = storageForAccount(accountId, key);
      if (!storage) return false;
      try {
        storage.setItem(key, value);
        return true;
      } catch {
        cleanupRequired = true;
        return false;
      }
    },

    remove(accountId: string, key: string): boolean {
      const storage = storageForAccount(accountId, key);
      if (!storage) return false;
      try {
        storage.removeItem(key);
        blockedKeys.delete(key);
        return true;
      } catch {
        blockedKeys.add(key);
        cleanupRequired = true;
        return false;
      }
    },

    take(accountId: string, key: string): string | null {
      const storage = storageForAccount(accountId, key);
      if (!storage) return null;
      try {
        const value = storage.getItem(key);
        if (value === null) return null;
        try {
          storage.removeItem(key);
          blockedKeys.delete(key);
          return value;
        } catch {
          blockedKeys.add(key);
          cleanupRequired = true;
          return null;
        }
      } catch {
        cleanupRequired = true;
        return null;
      }
    },
  };
}

export const sensitiveSession = createSensitiveSessionGuard();
