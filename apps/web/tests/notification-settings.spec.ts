import { expect, test, type Page, type Route } from "@playwright/test";

type PermissionState = "default" | "granted" | "denied";

interface MockOptions {
  permission?: PermissionState;
  hasSubscription?: boolean;
  registeredCurrentDevice?: boolean;
  unsubscribeFailuresBeforeSuccess?: number;
  directMessagesPush?: boolean;
  deleteStatus?: number;
  authenticated?: boolean;
  onPreferenceWrite?: (enabled: boolean) => void;
  onPushRead?: (pathname: string) => void;
  onPushDelete?: () => void;
}

async function installBrowserPushMock(
  page: Page,
  options: MockOptions = {},
): Promise<void> {
  const permission = options.permission ?? "default";
  const hasSubscription = options.hasSubscription ?? false;
  const unsubscribeFailuresBeforeSuccess =
    options.unsubscribeFailuresBeforeSuccess ?? 0;

  await page.addInitScript(
    ({ initialPermission, initialSubscription, failuresBeforeSuccess }) => {
      const unsubscribeAttemptsKey =
        "notification-settings-test-unsubscribe-attempts";
      let currentSubscription: {
        endpoint: string;
        toJSON: () => {
          endpoint: string;
          keys: { p256dh: string; auth: string };
        };
        unsubscribe: () => Promise<boolean>;
      } | null = null;

      const makeSubscription = () => ({
        endpoint: "https://push.example.test/current-device",
        toJSON: () => ({
          endpoint: "https://push.example.test/current-device",
          keys: { p256dh: "test-p256dh", auth: "test-auth" },
        }),
        unsubscribe: async () => {
          const attempts = Number(
            sessionStorage.getItem(unsubscribeAttemptsKey) ?? "0",
          );
          sessionStorage.setItem(unsubscribeAttemptsKey, String(attempts + 1));
          if (attempts < failuresBeforeSuccess) return false;
          currentSubscription = null;
          return true;
        },
      });

      if (initialSubscription) currentSubscription = makeSubscription();

      const registration = {
        pushManager: {
          getSubscription: async () => currentSubscription,
          subscribe: async () => {
            currentSubscription ??= makeSubscription();
            return currentSubscription;
          },
        },
      };

      Object.defineProperty(navigator, "serviceWorker", {
        configurable: true,
        value: {
          getRegistration: async () => registration,
          register: async () => registration,
          ready: Promise.resolve(registration),
        },
      });
      Object.defineProperty(window, "PushManager", {
        configurable: true,
        value: class PushManager {},
      });
      Object.defineProperty(window, "Notification", {
        configurable: true,
        value: {
          permission: initialPermission,
          requestPermission: async () => initialPermission,
        },
      });
    },
    {
      initialPermission: permission,
      initialSubscription: hasSubscription,
      failuresBeforeSuccess: unsubscribeFailuresBeforeSuccess,
    },
  );
}

async function installApiMocks(
  page: Page,
  options: MockOptions = {},
): Promise<void> {
  let directMessagesPush = options.directMessagesPush ?? false;
  const deleteStatus = options.deleteStatus ?? 204;
  const authenticated = options.authenticated ?? true;
  let registeredCurrentDevice =
    options.registeredCurrentDevice ?? options.hasSubscription ?? false;

  await page.route("**/_app/version.json", (route: Route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ version: "notification-settings-test" }),
    }),
  );

  await page.route("**/api/**", async (route: Route) => {
    const request = route.request();
    const { pathname } = new URL(request.url());
    const method = request.method();

    if (pathname === "/api/accounts" && method === "GET") {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: "[]",
      });
    }

    if (pathname === "/api/auth/me" && method === "GET") {
      if (!authenticated) {
        return route.fulfill({
          status: 401,
          contentType: "application/json",
          body: JSON.stringify({
            authenticated: false,
            account_id: null,
            role: "gast",
          }),
        });
      }
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          authenticated: true,
          account_id: "notification-test-account",
          role: "weber",
        }),
      });
    }

    if (pathname === "/api/auth/devices" && method === "GET") {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: "[]",
      });
    }

    if (pathname === "/api/push/config" && method === "GET") {
      options.onPushRead?.(pathname);
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          enabled: true,
          application_server_key: "AQIDBA",
        }),
      });
    }

    if (pathname === "/api/notifications/preferences" && method === "GET") {
      options.onPushRead?.(pathname);
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ direct_messages_push: directMessagesPush }),
      });
    }

    if (pathname === "/api/notifications/preferences" && method === "PUT") {
      const payload = request.postDataJSON() as {
        direct_messages_push?: boolean;
      };
      directMessagesPush = payload.direct_messages_push === true;
      options.onPreferenceWrite?.(directMessagesPush);
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ direct_messages_push: directMessagesPush }),
      });
    }

    if (pathname === "/api/push/subscriptions" && method === "GET") {
      options.onPushRead?.(pathname);
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          items: registeredCurrentDevice
            ? [
                {
                  id: "push-subscription-current",
                  created_at: "2026-08-27T06:00:00Z",
                  updated_at: "2026-08-27T06:00:00Z",
                  current: true,
                },
              ]
            : [],
          limit: 20,
        }),
      });
    }

    if (pathname === "/api/push/subscriptions" && method === "DELETE") {
      options.onPushDelete?.();
      if (deleteStatus >= 400) {
        return route.fulfill({
          status: deleteStatus,
          contentType: "application/json",
          body: JSON.stringify({
            code: "request_failed",
            message: "raw backend detail must stay hidden",
          }),
        });
      }
      registeredCurrentDevice = false;
      return route.fulfill({ status: 204 });
    }

    if (pathname === "/api/push/subscriptions" && method === "POST") {
      registeredCurrentDevice = true;
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ id: "push-subscription-test" }),
      });
    }

    if (pathname === "/api/direct-conversations" && method === "GET") {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ items: [] }),
      });
    }

    return route.fulfill({
      status: 404,
      contentType: "application/json",
      body: JSON.stringify({ code: "not_mocked" }),
    });
  });
}

async function setup(page: Page, options: MockOptions = {}): Promise<void> {
  await installBrowserPushMock(page, options);
  await installApiMocks(page, options);
}

test.describe("Settings — notifications information architecture", () => {
  test("renders one canonical notification section inside settings and keeps menu navigation local", async ({
    page,
  }) => {
    await setup(page);
    await page.goto("/settings");

    const menuLink = page.locator(
      '[data-testid="settings-menu"] a[href="#benachrichtigungen"]',
    );
    await expect(menuLink).toBeVisible();
    await expect(page.locator("#benachrichtigungen")).toHaveCount(1);

    await menuLink.click();

    await expect(page).toHaveURL(/\/settings#benachrichtigungen$/);
    await expect(page.locator("#benachrichtigungen")).toBeFocused();
    await expect(
      page.getByRole("heading", { name: "Benachrichtigungen", level: 2 }),
    ).toBeVisible();
    await expect(
      page.getByRole("heading", {
        name: "Registrierte Push-Geräte",
        level: 3,
      }),
    ).toBeVisible();
    await expect(
      page.getByText(
        "Für dieses Konto sind keine aktiven Push-Geräte registriert.",
      ),
    ).toBeVisible();
  });

  test("keeps account device recovery available without local Push support", async ({
    page,
  }) => {
    await page.addInitScript(() => {
      Object.defineProperty(window, "PushManager", {
        configurable: true,
        value: undefined,
      });
    });
    await installApiMocks(page);
    await page.goto("/settings#benachrichtigungen");

    const section = page.locator("#benachrichtigungen");
    await expect(section).toContainText(
      "Web Push ist in diesem Browser oder in dieser Browser-Ansicht nicht verfügbar.",
    );
    await expect(
      section.getByRole("heading", {
        name: "Registrierte Push-Geräte",
        level: 3,
      }),
    ).toBeVisible();
    await expect(
      section.getByText(
        "Für dieses Konto sind keine aktiven Push-Geräte registriert.",
      ),
    ).toBeVisible();
    await expect(
      section.getByRole("button", { name: "Auf diesem Gerät aktivieren" }),
    ).toHaveCount(0);
  });

  test("does not call push APIs for anonymous settings visitors and offers login", async ({
    page,
  }) => {
    let pushReads = 0;
    await setup(page, {
      authenticated: false,
      onPushRead: () => {
        pushReads += 1;
      },
    });
    await page.goto("/settings#benachrichtigungen");

    const section = page.locator("#benachrichtigungen");
    await expect(
      section.getByRole("link", { name: "Anmelden" }),
    ).toHaveAttribute("href", "/login");
    await expect(section.getByText(/Sitzung .*abgelaufen/i)).toHaveCount(0);
    await page.waitForLoadState("networkidle");
    expect(pushReads).toBe(0);
  });

  test("direct settings deep link focuses the notification section", async ({
    page,
  }) => {
    await setup(page);
    await page.goto("/settings#benachrichtigungen");

    const section = page.locator("#benachrichtigungen");
    await expect(section).toBeVisible();
    await expect(section).toBeFocused();
    await expect(section).toHaveAttribute(
      "aria-labelledby",
      "notification-settings-heading",
    );
  });

  test("legacy messages deep link redirects canonically without a back-navigation loop", async ({
    page,
  }) => {
    await setup(page);
    await page.goto("/settings");
    await page.goto("/nachrichten#benachrichtigungen");

    await expect(page).toHaveURL(/\/settings#benachrichtigungen$/);
    await expect(page.locator("#benachrichtigungen")).toBeFocused();

    await page.goBack();
    await expect(page).toHaveURL(/\/settings$/);
  });

  test("messages stays an inbox and only links to notification settings", async ({
    page,
  }) => {
    await setup(page);
    await page.goto("/nachrichten");

    await expect(page.getByRole("heading", { name: "Postfach" })).toBeVisible();
    await expect(page.locator("#benachrichtigungen")).toHaveCount(0);
    await expect(
      page.getByRole("link", { name: "Benachrichtigungen einstellen" }),
    ).toHaveAttribute("href", "/settings#benachrichtigungen");
  });

  test("keeps account preference and current-device state visibly separate", async ({
    page,
  }) => {
    let writtenPreference: boolean | null = null;
    await setup(page, {
      directMessagesPush: false,
      onPreferenceWrite: (enabled) => {
        writtenPreference = enabled;
      },
    });
    await page.goto("/settings#benachrichtigungen");

    const section = page.locator("#benachrichtigungen");
    await expect(section.getByText("Kontoeinstellung")).toBeVisible();
    await expect(section.getByText("Geräteeinstellung")).toBeVisible();
    await expect(
      section.getByText("Push auf diesem Gerät: nicht aktiviert."),
    ).toBeVisible();

    const preference = section.getByRole("checkbox", {
      name: "Push-Hinweise für private Nachrichten",
    });
    await expect(preference).not.toBeChecked();
    await preference.click();
    await expect(preference).toBeChecked();
    expect(writtenPreference).toBe(true);
  });

  test("shows a blocked browser permission as device state without an unusable enable action", async ({
    page,
  }) => {
    await setup(page, { permission: "denied" });
    await page.goto("/settings#benachrichtigungen");

    const section = page.locator("#benachrichtigungen");
    await expect(
      section.getByText(/Push auf diesem Gerät: blockiert\./),
    ).toBeVisible();
    await expect(
      section.getByText("In Browser- oder Systemeinstellungen freigeben"),
    ).toBeVisible();
    await expect(
      section.getByRole("button", { name: "Auf diesem Gerät aktivieren" }),
    ).toHaveCount(0);
  });

  test("allows local cleanup but not reactivation when browser permission is denied", async ({
    page,
  }) => {
    let deleteWrites = 0;
    await setup(page, {
      permission: "denied",
      hasSubscription: true,
      registeredCurrentDevice: false,
      onPushDelete: () => {
        deleteWrites += 1;
      },
    });
    await page.goto("/settings#benachrichtigungen");

    const section = page.locator("#benachrichtigungen");
    const cleanup = section.getByRole("button", {
      name: "Lokales Browser-Abo entfernen",
    });
    await expect(cleanup).toBeVisible();
    await expect(
      section.getByRole("button", { name: "Auf diesem Gerät aktivieren" }),
    ).toHaveCount(0);

    await cleanup.click();

    await expect(
      section.getByText("Das lokale Browser-Abo wurde entfernt."),
    ).toBeVisible();
    expect(deleteWrites).toBe(0);
  });

  test("keeps the remote cleanup retry path visible after a server deletion failure", async ({
    page,
  }) => {
    await setup(page, {
      permission: "granted",
      hasSubscription: true,
      deleteStatus: 503,
    });
    await page.goto("/settings#benachrichtigungen");

    const section = page.locator("#benachrichtigungen");
    await expect(
      section.getByText("Push auf diesem Gerät: aktiviert."),
    ).toBeVisible();

    const deactivate = section.getByRole("button", {
      name: "Auf diesem Gerät deaktivieren",
    });
    await deactivate.click();

    await expect(section.getByRole("alert")).toContainText(
      "Push konnte auf diesem Gerät nicht deaktiviert werden",
    );
    await expect(section.getByRole("alert")).not.toContainText(
      "raw backend detail",
    );
    await expect(deactivate).toBeVisible();
    await expect(
      section.getByText("Push auf diesem Gerät: aktiviert."),
    ).toBeVisible();
  });

  test("reconciles a stale local subscription that was removed on another device", async ({
    page,
  }) => {
    await setup(page, {
      permission: "granted",
      hasSubscription: true,
      registeredCurrentDevice: false,
    });
    await page.goto("/settings#benachrichtigungen");

    const section = page.locator("#benachrichtigungen");
    await expect(
      section.getByText(/Push auf diesem Gerät: serverseitig deaktiviert\./),
    ).toBeVisible();
    await expect(
      section.getByRole("button", { name: "Auf diesem Gerät deaktivieren" }),
    ).toHaveCount(0);

    await section
      .getByRole("button", { name: "Auf diesem Gerät aktivieren" })
      .click();
    await expect(
      section.getByText("Push auf diesem Gerät: aktiviert."),
    ).toBeVisible();
    await expect(
      section.getByLabel("1 von 20 Push-Geräten belegt"),
    ).toBeVisible();
  });

  test("retries only local cleanup after reload when server deletion already succeeded", async ({
    page,
  }) => {
    let deleteWrites = 0;
    await setup(page, {
      permission: "granted",
      hasSubscription: true,
      registeredCurrentDevice: true,
      unsubscribeFailuresBeforeSuccess: 1,
      onPushDelete: () => {
        deleteWrites += 1;
      },
    });
    await page.goto("/settings#benachrichtigungen");

    const section = page.locator("#benachrichtigungen");
    await section
      .getByRole("button", { name: "Auf diesem Gerät deaktivieren" })
      .click();

    await expect(
      section.getByText(/Push auf diesem Gerät: serverseitig deaktiviert\./),
    ).toBeVisible();
    const retry = section.getByRole("button", {
      name: "Lokales Browser-Abo entfernen",
    });
    await expect(retry).toBeVisible();
    expect(deleteWrites).toBe(1);

    await page.reload();

    await expect(retry).toBeVisible();
    await expect(
      section.getByRole("button", { name: "Auf diesem Gerät aktivieren" }),
    ).toBeVisible();
    expect(deleteWrites).toBe(1);

    await retry.click();

    await expect(retry).toHaveCount(0);
    await expect(
      section.getByText("Das lokale Browser-Abo wurde entfernt."),
    ).toBeVisible();
    expect(deleteWrites).toBe(1);
  });

  test("mobile layout keeps notification actions touch-sized", async ({
    page,
  }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await setup(page);
    await page.goto("/settings#benachrichtigungen");

    const section = page.locator("#benachrichtigungen");
    const activate = section.getByRole("button", {
      name: "Auf diesem Gerät aktivieren",
    });
    const inbox = section.getByRole("link", { name: "Zum Postfach" });

    const activateBox = await activate.boundingBox();
    const inboxBox = await inbox.boundingBox();
    expect(activateBox?.height ?? 0).toBeGreaterThanOrEqual(44);
    expect(inboxBox?.height ?? 0).toBeGreaterThanOrEqual(44);
    await expect(section).toBeFocused();
  });
});
