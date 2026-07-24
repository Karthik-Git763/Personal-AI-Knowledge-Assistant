import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

import type { GoogleDriveBackupStatus, WorkspaceBackup } from "@/lib/api/backups";

import {
  GoogleDriveBackupSettingsView,
  shouldPollBackupOperation,
} from "./GoogleDriveBackupSettings";
import { RestoreConfirmationContent } from "./RestoreBackupDialog";

const idleStatus: GoogleDriveBackupStatus = {
  configured: true,
  enabled: true,
  connection_status: "connected",
  google_email: "owner@example.com",
  last_attempt_at: "2026-07-25T08:00:00Z",
  last_success_at: "2026-07-25T08:00:00Z",
  next_due_at: "2026-07-26T08:00:00Z",
  consecutive_failures: 0,
  active_operation: null,
  restore_points: [],
};

const snapshot: WorkspaceBackup = {
  backup_id: "0bb939b8-2a5d-47a1-83ab-b99598bd1077",
  operation_kind: "snapshot",
  source_backup_id: null,
  status: "completed",
  trigger: "manual",
  schema_version: 1,
  archive_size_bytes: 4096,
  item_counts: { notes: 4, documents: 2, chats: 1 },
  started_at: "2026-07-25T08:00:00Z",
  completed_at: "2026-07-25T08:01:00Z",
  failure_message: null,
  restore_eligible: true,
};

const actions = {
  onBackup: vi.fn(),
  onConnect: vi.fn(),
  onDeleteBackups: vi.fn(),
  onDisconnect: vi.fn(),
  onRefresh: vi.fn(),
  onRestore: vi.fn(),
};

describe("GoogleDriveBackupSettingsView", () => {
  it("offers connection when Drive is disconnected", () => {
    const markup = renderToStaticMarkup(
      <GoogleDriveBackupSettingsView
        status={{ ...idleStatus, enabled: false, connection_status: null, google_email: null }}
        backups={[]}
        {...actions}
      />
    );

    expect(markup).toContain("Connect Google Drive");
    expect(markup).toContain("Not connected");
  });

  it("renders connected metadata and complete snapshot details", () => {
    const markup = renderToStaticMarkup(
      <GoogleDriveBackupSettingsView status={idleStatus} backups={[snapshot]} {...actions} />
    );

    expect(markup).toContain("owner@example.com");
    expect(markup).toContain("Daily backup");
    expect(markup).toContain("5 snapshots");
    expect(markup).toContain("4 notes");
    expect(markup).toContain("2 documents");
    expect(markup).toContain("Schema 1");
    expect(markup).toContain("Restore");
  });

  it("offers reconnection when authorization needs attention", () => {
    const markup = renderToStaticMarkup(
      <GoogleDriveBackupSettingsView
        status={{ ...idleStatus, connection_status: "reauthorization_required" }}
        backups={[]}
        {...actions}
      />
    );

    expect(markup).toContain("Reconnect");
    expect(markup).toContain("Authorization required");
  });

  it("locks conflicting controls during active work", () => {
    const markup = renderToStaticMarkup(
      <GoogleDriveBackupSettingsView
        status={{ ...idleStatus, active_operation: { ...snapshot, status: "uploading" } }}
        backups={[snapshot]}
        {...actions}
      />
    );

    expect(markup).toContain("Uploading backup");
    expect(markup).toMatch(/<button[^>]*disabled[^>]*>[\s\S]*?Back up now[\s\S]*?<\/button>/);
  });
});

describe("restore confirmation", () => {
  it("requires the exact RESTORE confirmation", () => {
    const partial = renderToStaticMarkup(
      <RestoreConfirmationContent
        backup={snapshot}
        confirmation="restore"
        isSubmitting={false}
        onCancel={vi.fn()}
        onConfirmationChange={vi.fn()}
        onConfirm={vi.fn()}
      />
    );
    const exact = renderToStaticMarkup(
      <RestoreConfirmationContent
        backup={snapshot}
        confirmation="RESTORE"
        isSubmitting={false}
        onCancel={vi.fn()}
        onConfirmationChange={vi.fn()}
        onConfirm={vi.fn()}
      />
    );

    expect(partial).toContain('type="button" disabled="">Replace workspace</button>');
    expect(exact).toContain('type="button">Replace workspace</button>');
  });

  it("polls only non-terminal backup states", () => {
    expect(shouldPollBackupOperation("pending")).toBe(true);
    expect(shouldPollBackupOperation("exporting")).toBe(true);
    expect(shouldPollBackupOperation("uploading")).toBe(true);
    expect(shouldPollBackupOperation("completed")).toBe(false);
    expect(shouldPollBackupOperation("failed")).toBe(false);
  });
});
