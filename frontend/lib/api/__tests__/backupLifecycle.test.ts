import { describe, expect, it, vi } from "vitest";

import type { GoogleDriveBackupStatus, WorkspaceBackup } from "../backups";
import {
  applyTerminalBackupOperation,
  loadBackupSettingsData,
  pollBackupOperation,
} from "../backupLifecycle";

const activeOperation: WorkspaceBackup = {
  backup_id: "0bb939b8-2a5d-47a1-83ab-b99598bd1077",
  operation_kind: "snapshot",
  source_backup_id: null,
  status: "uploading",
  trigger: "manual",
  schema_version: 1,
  archive_size_bytes: null,
  item_counts: {},
  started_at: "2026-07-25T08:00:00Z",
  completed_at: null,
  failure_message: null,
  restore_eligible: false,
};

const connectedStatus: GoogleDriveBackupStatus = {
  configured: true,
  enabled: true,
  connection_status: "connected",
  google_email: "owner@example.com",
  last_attempt_at: "2026-07-25T08:00:00Z",
  last_success_at: "2026-07-24T08:00:00Z",
  next_due_at: "2026-07-26T08:00:00Z",
  consecutive_failures: 0,
  active_operation: activeOperation,
  restore_points: [],
};

describe("pollBackupOperation", () => {
  it("survives transient failures with capped backoff and refreshes on terminal status", async () => {
    const completed = {
      ...activeOperation,
      status: "completed" as const,
      completed_at: "2026-07-25T08:02:00Z",
    };
    const getOperation = vi
      .fn<() => Promise<WorkspaceBackup>>()
      .mockRejectedValueOnce(new Error("temporary outage"))
      .mockRejectedValueOnce(new Error("temporary outage"))
      .mockResolvedValueOnce(activeOperation)
      .mockResolvedValueOnce(completed);
    const delays: number[] = [];
    const onProgress = vi.fn();
    const onTransientError = vi.fn();
    const refreshAfterTerminal = vi.fn();

    const result = await pollBackupOperation({
      operationId: activeOperation.backup_id,
      getOperation,
      wait: async (delayMs) => {
        delays.push(delayMs);
        return true;
      },
      isTransientError: () => true,
      onProgress,
      onTransientError,
      onTerminal: refreshAfterTerminal,
      initialDelayMs: 100,
      maxDelayMs: 250,
    });

    expect(delays).toEqual([100, 200, 250, 100]);
    expect(onTransientError).toHaveBeenCalledTimes(2);
    expect(onProgress).toHaveBeenCalledWith(activeOperation);
    expect(refreshAfterTerminal).toHaveBeenCalledOnce();
    expect(refreshAfterTerminal).toHaveBeenCalledWith(completed);
    expect(result).toEqual(completed);
  });
});

describe("loadBackupSettingsData", () => {
  it("preserves connected and active status when the backup list fails", async () => {
    const previousBackup = {
      ...activeOperation,
      status: "completed" as const,
      completed_at: "2026-07-24T08:00:00Z",
    };
    const listError = new Error("Drive list unavailable");

    const result = await loadBackupSettingsData({
      getStatus: async () => connectedStatus,
      listBackups: async () => Promise.reject(listError),
      previousStatus: null,
      previousBackups: [previousBackup],
    });

    expect(result.status).toBe(connectedStatus);
    expect(result.status?.active_operation).toBe(activeOperation);
    expect(result.backups).toEqual([previousBackup]);
    expect(result.statusError).toBeNull();
    expect(result.listError).toBe(listError);
  });

  it("preserves a terminal operation when the following status refresh fails", async () => {
    const failedOperation: WorkspaceBackup = {
      ...activeOperation,
      status: "failed",
      failure_message: "Backup failed.",
    };
    const terminalStatus = applyTerminalBackupOperation(connectedStatus, failedOperation);

    const result = await loadBackupSettingsData({
      getStatus: async () => Promise.reject(new Error("status unavailable")),
      listBackups: async () => [],
      previousStatus: terminalStatus,
      previousBackups: [],
    });

    expect(result.status?.active_operation).toEqual(failedOperation);
    expect(result.status?.active_operation?.status).toBe("failed");
    expect(result.statusError).toBeInstanceOf(Error);
  });
});
