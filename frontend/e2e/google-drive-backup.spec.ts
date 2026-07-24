import { expect, test, type Page, type Route } from "@playwright/test";

import { registerAndVerify } from "./helpers";

type OperationStatus = "pending" | "completed";
type OperationKind = "snapshot" | "restore";

interface MockBackup {
  backup_id: string;
  operation_kind: OperationKind;
  source_backup_id: string | null;
  status: OperationStatus;
  trigger: "manual";
  schema_version: number;
  archive_size_bytes: number;
  item_counts: Record<string, number>;
  started_at: string;
  completed_at: string | null;
  failure_message: null;
  restore_eligible: boolean;
}

const backupOperationId = "10000000-0000-4000-8000-000000000001";
const restoreOperationId = "20000000-0000-4000-8000-000000000001";
const googleEmail = "cognolith.backup@example.com";

const initialSnapshots: MockBackup[] = Array.from({ length: 4 }, (_, index) => {
  const day = String(20 - index).padStart(2, "0");
  return {
    backup_id: `00000000-0000-4000-8000-00000000000${index + 1}`,
    operation_kind: "snapshot",
    source_backup_id: null,
    status: "completed",
    trigger: "manual",
    schema_version: 1,
    archive_size_bytes: 2048 + index * 1024,
    item_counts: {
      notes: 12 + index,
      documents: 3,
      chat_sessions: 2,
    },
    started_at: `2026-07-${day}T08:00:00Z`,
    completed_at: `2026-07-${day}T08:01:00Z`,
    failure_message: null,
    restore_eligible: true,
  };
});

function operation(
  backupId: string,
  kind: OperationKind,
  status: OperationStatus,
  sourceBackupId: string | null = null,
): MockBackup {
  return {
    backup_id: backupId,
    operation_kind: kind,
    source_backup_id: sourceBackupId,
    status,
    trigger: "manual",
    schema_version: 1,
    archive_size_bytes: status === "completed" ? 8192 : 0,
    item_counts: status === "completed" ? { notes: 16, documents: 3, chat_sessions: 2 } : {},
    started_at: "2026-07-24T09:00:00Z",
    completed_at: status === "completed" ? "2026-07-24T09:01:00Z" : null,
    failure_message: null,
    restore_eligible: status === "completed" && kind === "snapshot",
  };
}

async function fulfillJson(route: Route, payload: unknown, status = 200): Promise<void> {
  await route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(payload),
  });
}

async function installBackupApi(
  page: Page,
): Promise<{ backupPolls: () => number; restorePolls: () => number }> {
  let connected = false;
  let snapshots = [...initialSnapshots];
  let activeOperation: MockBackup | null = null;
  let backupPollCount = 0;
  let restorePollCount = 0;

  await page.route("**/api/v1/users/me/google-drive/status", async (route) => {
    await fulfillJson(route, {
      configured: true,
      enabled: connected,
      connection_status: connected ? "connected" : "disconnected",
      google_email: connected ? googleEmail : null,
      last_attempt_at: connected ? "2026-07-24T09:00:00Z" : null,
      last_success_at: connected ? "2026-07-24T09:01:00Z" : null,
      next_due_at: connected ? "2026-07-25T09:01:00Z" : null,
      consecutive_failures: 0,
      active_operation: activeOperation,
      restore_points: snapshots.map((snapshot) => ({
        backup_id: snapshot.backup_id,
        schema_version: snapshot.schema_version,
        archive_size_bytes: snapshot.archive_size_bytes,
        created_at: snapshot.completed_at,
        restore_eligible: snapshot.restore_eligible,
      })),
    });
  });

  await page.route("**/api/v1/users/me/google-drive/connect", async (route) => {
    connected = true;
    await fulfillJson(route, {
      authorization_url: "/dashboard/settings?drive=connected",
    });
  });

  await page.route("**/api/v1/users/me/google-drive", async (route) => {
    expect(route.request().method()).toBe("DELETE");
    connected = false;
    activeOperation = null;
    await fulfillJson(route, { status: "disconnected" });
  });

  await page.route("**/api/v1/users/me/backups", async (route) => {
    if (route.request().method() === "GET") {
      await fulfillJson(route, snapshots);
      return;
    }

    expect(route.request().method()).toBe("POST");
    activeOperation = operation(backupOperationId, "snapshot", "pending");
    await fulfillJson(route, activeOperation, 202);
  });

  await page.route("**/api/v1/users/me/backups/*/preview", async (route) => {
    const backupId = new URL(route.request().url()).pathname.split("/").at(-2);
    const snapshot = snapshots.find((candidate) => candidate.backup_id === backupId);
    expect(snapshot).toBeTruthy();
    await fulfillJson(route, {
      backup_id: snapshot!.backup_id,
      schema_version: snapshot!.schema_version,
      item_counts: snapshot!.item_counts,
      archive_size_bytes: snapshot!.archive_size_bytes,
    });
  });

  await page.route("**/api/v1/users/me/backups/*/restore", async (route) => {
    const backupId = new URL(route.request().url()).pathname.split("/").at(-2);
    expect(route.request().method()).toBe("POST");
    expect(route.request().postDataJSON()).toEqual({ confirmation: "RESTORE" });
    activeOperation = operation(restoreOperationId, "restore", "pending", backupId);
    await fulfillJson(route, activeOperation, 202);
  });

  await page.route("**/api/v1/users/me/backups/operations/*", async (route) => {
    const operationId = new URL(route.request().url()).pathname.split("/").at(-1);

    if (operationId === backupOperationId) {
      backupPollCount += 1;
      const completed = operation(backupOperationId, "snapshot", "completed");
      snapshots = [completed, ...snapshots].slice(0, 5);
      activeOperation = null;
      await fulfillJson(route, completed);
      return;
    }

    expect(operationId).toBe(restoreOperationId);
    restorePollCount += 1;
    const completed = operation(
      restoreOperationId,
      "restore",
      "completed",
      snapshots[0].backup_id,
    );
    activeOperation = null;
    await fulfillJson(route, completed);
  });

  return {
    backupPolls: () => backupPollCount,
    restorePolls: () => restorePollCount,
  };
}

test("connects Drive, backs up, restores, and disconnects", async ({ page, request }) => {
  const browserErrors: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error") browserErrors.push(`console: ${message.text()}`);
  });
  page.on("pageerror", (error) => browserErrors.push(`page: ${error.message}`));

  await registerAndVerify(page, request, `drive-backup-${Date.now()}@example.com`);
  const polls = await installBackupApi(page);

  await page.goto("/dashboard/settings");
  await expect(page.getByRole("heading", { name: "Google Drive backup" })).toBeVisible();
  await expect(page.getByText("Not connected", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Connect Google Drive" })).toBeVisible();

  await page.getByRole("button", { name: "Dark", exact: true }).click();
  await expect(page.locator("html")).toHaveClass(/dark/);
  await expect(page.getByRole("heading", { name: "Google Drive backup" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Connect Google Drive" })).toBeVisible();

  await page.getByRole("button", { name: "Connect Google Drive" }).click();
  await expect(page).toHaveURL(/\/dashboard\/settings$/);
  await expect(page.getByText("Google Drive is connected. Daily backups are now enabled.")).toBeVisible();
  await expect(page.getByText(googleEmail, { exact: true })).toBeVisible();
  await expect(page.getByText("4 available", { exact: true })).toBeVisible();

  await page.getByRole("button", { name: "Back up now" }).click();
  await expect(page.getByText("Preparing backup", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Back up now" })).toBeDisabled();
  await expect(page.getByText("Workspace backup completed.", { exact: true })).toBeVisible();
  await expect(page.getByText("5 available", { exact: true })).toBeVisible();
  expect(polls.backupPolls()).toBeGreaterThan(0);
  await expect(page.getByRole("button", { name: "Restore" })).toHaveCount(5);

  await page.getByRole("button", { name: "Restore" }).first().click();
  const restoreDialog = page.getByRole("dialog");
  await expect(
    restoreDialog.getByRole("heading", { name: "Replace workspace from backup" }),
  ).toBeVisible();
  await expect(restoreDialog.getByText("16 notes", { exact: true })).toBeVisible();

  const confirmation = restoreDialog.getByLabel(/Type RESTORE to continue/);
  const replaceButton = restoreDialog.getByRole("button", { name: "Replace workspace" });
  await confirmation.fill("restore");
  await expect(replaceButton).toBeDisabled();
  await confirmation.fill("RESTORE");
  await expect(replaceButton).toBeEnabled();
  await replaceButton.click();

  await expect(page.getByText("Preparing restore", { exact: true })).toBeVisible();
  await expect(page.getByText("Workspace restore completed.", { exact: true })).toBeVisible();
  expect(polls.restorePolls()).toBeGreaterThan(0);

  await page.getByRole("button", { name: "Disconnect" }).click();
  const disconnectDialog = page.getByRole("dialog");
  await expect(
    disconnectDialog.getByRole("heading", { name: "Disconnect Google Drive?" }),
  ).toBeVisible();
  await disconnectDialog.getByRole("button", { name: "Disconnect" }).click();

  await expect(page.getByText("Google Drive was disconnected.", { exact: true })).toBeVisible();
  await expect(page.getByText("Not connected", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Connect Google Drive" })).toBeVisible();
  await expect
    .poll(() => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1))
    .toBe(true);
  expect(browserErrors).toEqual([]);
});
