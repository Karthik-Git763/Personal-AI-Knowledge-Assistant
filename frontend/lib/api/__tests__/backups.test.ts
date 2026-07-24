import { beforeEach, describe, expect, it, vi } from "vitest";

import { apiClient } from "@/lib/api/client";

import {
  connectGoogleDrive,
  createWorkspaceBackup,
  deleteGoogleDriveBackups,
  disconnectGoogleDrive,
  getBackupOperation,
  getGoogleDriveBackupStatus,
  listWorkspaceBackups,
  previewWorkspaceBackup,
  restoreWorkspaceBackup,
} from "../backups";

vi.mock("@/lib/api/client", () => ({
  apiClient: {
    delete: vi.fn(),
    get: vi.fn(),
    post: vi.fn(),
  },
}));

const operation = {
  backup_id: "0bb939b8-2a5d-47a1-83ab-b99598bd1077",
  operation_kind: "snapshot" as const,
  source_backup_id: null,
  status: "completed" as const,
  trigger: "manual" as const,
  schema_version: 1,
  archive_size_bytes: 4096,
  item_counts: { notes: 4, documents: 2 },
  started_at: "2026-07-25T08:00:00Z",
  completed_at: "2026-07-25T08:01:00Z",
  failure_message: null,
  restore_eligible: true,
};

describe("backup API", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("loads status and the complete backup list", async () => {
    const status = {
      configured: true,
      enabled: true,
      connection_status: "connected" as const,
      google_email: "owner@example.com",
      last_attempt_at: operation.completed_at,
      last_success_at: operation.completed_at,
      next_due_at: "2026-07-26T08:00:00Z",
      consecutive_failures: 0,
      active_operation: null,
      restore_points: [
        {
          backup_id: operation.backup_id,
          schema_version: 1,
          archive_size_bytes: 4096,
          created_at: operation.completed_at,
          restore_eligible: true,
        },
      ],
    };
    vi.mocked(apiClient.get)
      .mockResolvedValueOnce({ data: status })
      .mockResolvedValueOnce({ data: [operation] });

    await expect(getGoogleDriveBackupStatus()).resolves.toEqual(status);
    await expect(listWorkspaceBackups()).resolves.toEqual([operation]);
    expect(apiClient.get).toHaveBeenNthCalledWith(1, "/users/me/google-drive/status");
    expect(apiClient.get).toHaveBeenNthCalledWith(2, "/users/me/backups");
  });

  it("maps connect, backup, preview, restore, and operation endpoints", async () => {
    const preview = {
      backup_id: operation.backup_id,
      schema_version: 1,
      item_counts: operation.item_counts,
      archive_size_bytes: operation.archive_size_bytes,
    };
    vi.mocked(apiClient.post)
      .mockResolvedValueOnce({ data: { authorization_url: "https://accounts.google.test/oauth" } })
      .mockResolvedValueOnce({ data: operation })
      .mockResolvedValueOnce({ data: { ...operation, operation_kind: "restore" } });
    vi.mocked(apiClient.get)
      .mockResolvedValueOnce({ data: preview })
      .mockResolvedValueOnce({ data: operation });

    await expect(connectGoogleDrive()).resolves.toEqual({
      authorization_url: "https://accounts.google.test/oauth",
    });
    await expect(createWorkspaceBackup()).resolves.toEqual(operation);
    await expect(previewWorkspaceBackup(operation.backup_id)).resolves.toEqual(preview);
    await expect(restoreWorkspaceBackup(operation.backup_id, "RESTORE")).resolves.toMatchObject({
      operation_kind: "restore",
    });
    await expect(getBackupOperation(operation.backup_id)).resolves.toEqual(operation);

    expect(apiClient.post).toHaveBeenNthCalledWith(1, "/users/me/google-drive/connect");
    expect(apiClient.post).toHaveBeenNthCalledWith(2, "/users/me/backups");
    expect(apiClient.get).toHaveBeenNthCalledWith(
      1,
      `/users/me/backups/${operation.backup_id}/preview`
    );
    expect(apiClient.post).toHaveBeenNthCalledWith(
      3,
      `/users/me/backups/${operation.backup_id}/restore`,
      { confirmation: "RESTORE" }
    );
    expect(apiClient.get).toHaveBeenNthCalledWith(
      2,
      `/users/me/backups/operations/${operation.backup_id}`
    );
  });

  it("uses exact destructive confirmations for disconnect and cloud deletion", async () => {
    vi.mocked(apiClient.delete).mockResolvedValueOnce({ data: { status: "disconnected" } });
    vi.mocked(apiClient.post).mockResolvedValueOnce({ data: { deleted: 3 } });

    await expect(disconnectGoogleDrive()).resolves.toEqual({ status: "disconnected" });
    await expect(deleteGoogleDriveBackups()).resolves.toEqual({ deleted: 3 });

    expect(apiClient.delete).toHaveBeenCalledWith("/users/me/google-drive");
    expect(apiClient.post).toHaveBeenCalledWith("/users/me/google-drive/delete-backups", {
      confirmation: "DELETE BACKUPS",
    });
  });
});
