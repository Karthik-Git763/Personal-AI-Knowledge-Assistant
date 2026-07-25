"use client";

import {
  AlertTriangle,
  CalendarClock,
  CheckCircle2,
  Cloud,
  CloudOff,
  CloudUpload,
  DatabaseBackup,
  Loader2,
  RefreshCw,
  RotateCcw,
  Trash2,
  Unplug,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  Badge,
  Button,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  Skeleton,
} from "@/components/ui/index";
import {
  connectGoogleDrive,
  createWorkspaceBackup,
  deleteGoogleDriveBackups,
  disconnectGoogleDrive,
  getBackupOperation,
  getGoogleDriveBackupStatus,
  listWorkspaceBackups,
  restoreWorkspaceBackup,
  type BackupStatus,
  type GoogleDriveBackupStatus,
  type WorkspaceBackup,
} from "@/lib/api/backups";
import {
  applyTerminalBackupOperation,
  loadBackupSettingsData,
  pollBackupOperation,
} from "@/lib/api/backupLifecycle";
import { APIRequestError } from "@/lib/api/client";

import { RestoreBackupDialog } from "./RestoreBackupDialog";

const ACTIVE_BACKUP_STATUSES = new Set<BackupStatus>(["pending", "exporting", "uploading"]);
const EMPTY_BACKUP_STATUS: GoogleDriveBackupStatus = {
  configured: true,
  enabled: false,
  connection_status: null,
  google_email: null,
  last_attempt_at: null,
  last_success_at: null,
  next_due_at: null,
  consecutive_failures: 0,
  active_operation: null,
  restore_points: [],
};

export function shouldPollBackupOperation(status: BackupStatus): boolean {
  return ACTIVE_BACKUP_STATUSES.has(status);
}

function formatDateTime(value: string | null): string {
  if (!value) return "Not yet";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Unavailable";
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(date);
}

function formatBytes(bytes: number | null): string {
  if (bytes === null || !Number.isFinite(bytes) || bytes < 0) return "Size unavailable";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function formatItemCounts(counts: Record<string, number>): string[] {
  return Object.entries(counts)
    .filter(([, count]) => count > 0)
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([name, count]) => {
      const normalized = name.replaceAll("_", " ");
      const singular = normalized.endsWith("s") ? normalized.slice(0, -1) : normalized;
      return `${count.toLocaleString()} ${count === 1 ? singular : normalized}`;
    });
}

function operationLabel(operation: WorkspaceBackup): string {
  const noun = operation.operation_kind === "restore" ? "restore" : "backup";
  if (operation.status === "pending") return `Preparing ${noun}`;
  if (operation.status === "exporting") return "Exporting workspace";
  if (operation.status === "uploading") {
    return operation.operation_kind === "restore" ? "Restoring workspace" : "Uploading backup";
  }
  if (operation.status === "failed") return `${noun[0].toUpperCase()}${noun.slice(1)} failed`;
  return `${noun[0].toUpperCase()}${noun.slice(1)} complete`;
}

function safeBackupError(error: unknown): string {
  if (error instanceof APIRequestError) {
    if (error.errorCode === "GOOGLE_DRIVE_REAUTHORIZATION_REQUIRED") {
      return "Google Drive authorization has expired. Reconnect to continue.";
    }
    if (error.errorCode === "GOOGLE_DRIVE_NOT_CONFIGURED") {
      return "Google Drive backup is not configured for this installation.";
    }
    if (error.errorCode === "BACKUP_OPERATION_ACTIVE") {
      return "Another backup or restore is already running.";
    }
    if (error.errorCode === "GOOGLE_DRIVE_RETRYABLE") {
      return "Google Drive is temporarily unavailable. Try again shortly.";
    }
  }
  return "The backup request could not be completed. Refresh and try again.";
}

interface GoogleDriveBackupSettingsViewProps {
  status: GoogleDriveBackupStatus;
  backups: WorkspaceBackup[];
  actionPending?: boolean;
  error?: string | null;
  message?: string | null;
  listUnavailable?: boolean;
  onBackup: () => void;
  onConnect: () => void;
  onDeleteBackups: () => void;
  onDisconnect: () => void;
  onRefresh: () => void;
  onRestore: (backup: WorkspaceBackup) => void;
}

export function GoogleDriveBackupSettingsView({
  status,
  backups,
  actionPending = false,
  error = null,
  message = null,
  listUnavailable = false,
  onBackup,
  onConnect,
  onDeleteBackups,
  onDisconnect,
  onRefresh,
  onRestore,
}: GoogleDriveBackupSettingsViewProps) {
  const connected = status.connection_status === "connected";
  const attention =
    status.connection_status === "reauthorization_required" ||
    status.connection_status === "failed";
  const active = Boolean(
    status.active_operation && shouldPollBackupOperation(status.active_operation.status)
  );
  const locked = active || actionPending;

  let statusLabel = "Not connected";
  let statusVariant: "outline" | "destructive" | "secondary" = "secondary";
  if (connected) {
    statusLabel = "Connected";
    statusVariant = "outline";
  } else if (attention) {
    statusLabel = "Authorization required";
    statusVariant = "destructive";
  } else if (!status.configured) {
    statusLabel = "Unavailable";
    statusVariant = "destructive";
  }

  return (
    <Card className="border-border bg-card">
      <CardHeader className="border-b border-border">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="space-y-1">
            <CardTitle>Google Drive backup</CardTitle>
            <p className="text-xs text-muted-foreground">
              Keep five private workspace snapshots in Drive app data.
            </p>
          </div>
          <Badge variant={statusVariant}>{statusLabel}</Badge>
        </div>
      </CardHeader>

      <CardContent className="space-y-5 py-5">
        {!status.configured ? (
          <div className="flex gap-3 border border-destructive/30 bg-destructive/10 p-3">
            <CloudOff className="mt-0.5 h-4 w-4 shrink-0 text-destructive" />
            <div>
              <p className="text-sm font-medium text-foreground">Backup is unavailable</p>
              <p className="text-xs text-muted-foreground">
                Google Drive credentials have not been configured for this installation.
              </p>
            </div>
          </div>
        ) : !connected ? (
          <div className="grid gap-4 border border-border bg-muted/30 p-4 md:grid-cols-[1fr_auto] md:items-center">
            <div className="flex gap-3">
              {attention ? (
                <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-destructive" />
              ) : (
                <Cloud className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" />
              )}
              <div>
                <p className="text-sm font-medium text-foreground">
                  {attention ? "Reconnect Google Drive" : "Back up this workspace"}
                </p>
                <p className="mt-1 max-w-2xl text-xs leading-relaxed text-muted-foreground">
                  {attention
                    ? "Authorization must be renewed before automatic or manual backups can continue."
                    : "Connect one Google account to enable daily snapshots and cross-device restore."}
                </p>
              </div>
            </div>
            <Button type="button" onClick={onConnect} disabled={locked}>
              <Cloud className="h-4 w-4" />
              {attention ? "Reconnect" : "Connect Google Drive"}
            </Button>
          </div>
        ) : (
          <>
            <div className="grid gap-px border border-border bg-border sm:grid-cols-2 xl:grid-cols-4">
              <div className="min-w-0 bg-card p-3">
                <p className="text-xs text-muted-foreground">Google account</p>
                <p className="mt-1 truncate text-sm font-medium text-foreground">
                  {status.google_email ?? "Connected account"}
                </p>
              </div>
              <div className="bg-card p-3">
                <p className="text-xs text-muted-foreground">Schedule</p>
                <p className="mt-1 text-sm font-medium text-foreground">Daily backup</p>
              </div>
              <div className="bg-card p-3">
                <p className="text-xs text-muted-foreground">Last successful</p>
                <p className="mt-1 text-sm font-medium text-foreground">
                  {formatDateTime(status.last_success_at)}
                </p>
              </div>
              <div className="bg-card p-3">
                <p className="text-xs text-muted-foreground">Retention</p>
                <p className="mt-1 text-sm font-medium text-foreground">5 snapshots</p>
              </div>
            </div>

            {active && status.active_operation ? (
              <div
                className="flex flex-wrap items-center justify-between gap-3 border border-border bg-muted/40 p-3"
                aria-live="polite"
              >
                <div className="flex items-center gap-2">
                  {active ? (
                    <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" />
                  ) : status.active_operation.status === "failed" ? (
                    <AlertTriangle className="h-4 w-4 text-destructive" />
                  ) : (
                    <CheckCircle2 className="h-4 w-4 text-muted-foreground" />
                  )}
                  <span className="text-sm font-medium text-foreground">
                    {operationLabel(status.active_operation)}
                  </span>
                </div>
                <Button type="button" onClick={onBackup} disabled>
                  <CloudUpload className="h-4 w-4" />
                  Back up now
                </Button>
              </div>
            ) : (
              <div className="flex flex-wrap items-center justify-between gap-3 border border-border bg-muted/30 p-3">
                <div className="flex items-center gap-2 text-sm text-muted-foreground">
                  <CalendarClock className="h-4 w-4" />
                  <span>Next backup {formatDateTime(status.next_due_at)}</span>
                </div>
                <Button type="button" onClick={onBackup} disabled={locked}>
                  <CloudUpload className="h-4 w-4" />
                  Back up now
                </Button>
              </div>
            )}

            <section aria-labelledby="restore-points-heading">
              <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border pb-3">
                <div>
                  <h3 id="restore-points-heading" className="text-sm font-semibold text-foreground">
                    Restore points
                  </h3>
                  <p className="text-xs text-muted-foreground">
                    Restoring replaces the current workspace after a safety backup.
                  </p>
                </div>
                <span className="text-xs text-muted-foreground">
                  {listUnavailable && status.restore_points.length > 0
                    ? `${status.restore_points.length} snapshots known`
                    : `${backups.length} available`}
                </span>
              </div>

              {listUnavailable && backups.length === 0 && status.restore_points.length > 0 ? (
                <div className="flex min-h-28 flex-col items-center justify-center gap-2 text-center">
                  <AlertTriangle className="h-5 w-5 text-muted-foreground" />
                  <p className="text-sm font-medium text-foreground">
                    Restore points temporarily unavailable
                  </p>
                  <p className="text-xs text-muted-foreground">
                    Refresh when Google Drive is available again.
                  </p>
                </div>
              ) : backups.length === 0 ? (
                <div className="flex min-h-28 flex-col items-center justify-center gap-2 text-center">
                  <DatabaseBackup className="h-5 w-5 text-muted-foreground" />
                  <p className="text-sm font-medium text-foreground">No cloud snapshots yet</p>
                  <p className="text-xs text-muted-foreground">
                    Create the first backup to make this workspace restorable.
                  </p>
                </div>
              ) : (
                <div className="divide-y divide-border">
                  {backups.map((backup) => {
                    const counts = formatItemCounts(backup.item_counts);
                    return (
                      <div
                        key={backup.backup_id}
                        className="grid gap-3 py-3 md:grid-cols-[minmax(0,1fr)_auto] md:items-center"
                      >
                        <div className="min-w-0 space-y-1">
                          <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                            <p className="text-sm font-medium text-foreground">
                              {formatDateTime(backup.completed_at ?? backup.started_at)}
                            </p>
                            <span className="text-xs text-muted-foreground">
                              Schema {backup.schema_version}
                            </span>
                            {backup.app_version ? (
                              <span className="text-xs text-muted-foreground">
                                App {backup.app_version}
                              </span>
                            ) : null}
                            <span className="text-xs text-muted-foreground">
                              {formatBytes(backup.archive_size_bytes)}
                            </span>
                          </div>
                          <div className="flex flex-wrap gap-x-3 gap-y-1 text-xs text-muted-foreground">
                            {counts.length > 0 ? (
                              counts.map((count) => <span key={count}>{count}</span>)
                            ) : (
                              <span>Item counts unavailable</span>
                            )}
                          </div>
                        </div>
                        <Button
                          type="button"
                          size="sm"
                          variant="outline"
                          onClick={() => onRestore(backup)}
                          disabled={locked || !backup.restore_eligible}
                          title={
                            backup.restore_eligible
                              ? "Preview and restore this snapshot"
                              : "This snapshot cannot be restored by this version"
                          }
                        >
                          <RotateCcw className="h-4 w-4" />
                          Restore
                        </Button>
                      </div>
                    );
                  })}
                </div>
              )}
            </section>
          </>
        )}

        {error ? (
          <p
            className="border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive"
            role="alert"
          >
            {error}
          </p>
        ) : null}
        {message ? (
          <p className="border border-border bg-muted p-3 text-sm text-foreground" role="status">
            {message}
          </p>
        ) : null}

        <div className="flex flex-wrap items-center justify-between gap-3 border-t border-border pt-4">
          <Button
            type="button"
            variant="outline"
            aria-label="Refresh backup status"
            onClick={onRefresh}
            disabled={actionPending}
          >
            <RefreshCw className="h-4 w-4" />
            Refresh
          </Button>
          {connected ? (
            <div className="flex flex-wrap gap-2">
              <Button
                type="button"
                variant="destructive"
                onClick={onDeleteBackups}
                disabled={locked || backups.length === 0}
              >
                <Trash2 className="h-4 w-4" />
                Delete cloud backups
              </Button>
              <Button type="button" variant="outline" onClick={onDisconnect} disabled={locked}>
                <Unplug className="h-4 w-4" />
                Disconnect
              </Button>
            </div>
          ) : null}
        </div>
      </CardContent>
    </Card>
  );
}

interface DestructiveConfirmationDialogProps {
  kind: "delete" | "disconnect" | null;
  open: boolean;
  isPending: boolean;
  onOpenChange: (open: boolean) => void;
  onConfirm: () => void;
}

function DestructiveConfirmationDialog({
  kind,
  open,
  isPending,
  onOpenChange,
  onConfirm,
}: DestructiveConfirmationDialogProps) {
  if (!kind) return null;
  const deleting = kind === "delete";
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="rounded-none sm:max-w-md">
        <DialogHeader>
          <DialogTitle>
            {deleting ? "Delete all cloud backups?" : "Disconnect Google Drive?"}
          </DialogTitle>
          <DialogDescription>
            {deleting
              ? "This permanently removes every Cognolith snapshot stored in this Google account."
              : "Automatic backups will stop. Existing cloud snapshots remain available until deleted."}
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button type="button" variant="destructive" onClick={onConfirm} disabled={isPending}>
            {isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : null}
            {deleting ? "Delete backups" : "Disconnect"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export function GoogleDriveBackupSettings() {
  const [status, setStatus] = useState<GoogleDriveBackupStatus | null>(null);
  const [backups, setBackups] = useState<WorkspaceBackup[]>([]);
  const [selectedBackup, setSelectedBackup] = useState<WorkspaceBackup | null>(null);
  const [confirmationKind, setConfirmationKind] = useState<"delete" | "disconnect" | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [isActionPending, setIsActionPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const statusRef = useRef<GoogleDriveBackupStatus | null>(null);
  const backupsRef = useRef<WorkspaceBackup[]>([]);

  useEffect(() => {
    statusRef.current = status;
  }, [status]);

  useEffect(() => {
    backupsRef.current = backups;
  }, [backups]);

  const loadBackupData = useCallback(async () => {
    setError(null);
    const result = await loadBackupSettingsData({
      getStatus: getGoogleDriveBackupStatus,
      listBackups: listWorkspaceBackups,
      previousStatus: statusRef.current,
      previousBackups: backupsRef.current,
    });

    if (result.statusError) {
      const fallbackStatus = {
        ...EMPTY_BACKUP_STATUS,
        configured:
          !(result.statusError instanceof APIRequestError) ||
          result.statusError.errorCode !== "GOOGLE_DRIVE_NOT_CONFIGURED",
      };
      setStatus(result.status ?? fallbackStatus);
      setBackups(result.backups);
      setError(safeBackupError(result.statusError));
      setIsLoading(false);
      return;
    }

    setStatus(result.status);
    setBackups(result.backups);
    if (result.listError) {
      setError("Backup status loaded, but restore points could not be refreshed.");
    }
    setIsLoading(false);
  }, []);

  useEffect(() => {
    const driveResult = new URLSearchParams(window.location.search).get("drive");
    if (!driveResult) return;
    const callbackMessages: Record<string, string> = {
      connected: "Google Drive is connected. Daily backups are now enabled.",
      invalid_state: "The Google Drive connection expired. Start the connection again.",
      authorization_failed: "Google Drive could not be connected. Try again.",
      not_configured: "Google Drive backup is not configured for this installation.",
    };
    const messageUpdate = window.setTimeout(
      () => setMessage(callbackMessages[driveResult] ?? "Google Drive connection returned."),
      0
    );
    const url = new URL(window.location.href);
    url.searchParams.delete("drive");
    window.history.replaceState({}, "", `${url.pathname}${url.search}${url.hash}`);
    return () => window.clearTimeout(messageUpdate);
  }, []);

  useEffect(() => {
    const initialLoad = window.setTimeout(() => void loadBackupData(), 0);
    return () => window.clearTimeout(initialLoad);
  }, [loadBackupData]);

  const activeOperation = status?.active_operation ?? null;
  const pollingOperationId = useMemo(
    () =>
      activeOperation && shouldPollBackupOperation(activeOperation.status)
        ? activeOperation.backup_id
        : null,
    [activeOperation]
  );

  useEffect(() => {
    if (!pollingOperationId) return;
    const controller = new AbortController();
    void pollBackupOperation({
      operationId: pollingOperationId,
      getOperation: getBackupOperation,
      signal: controller.signal,
      onProgress: (operation) => {
        setError(null);
        setStatus((current) =>
          current ? { ...current, active_operation: operation } : current
        );
      },
      onTerminal: (operation) => {
        setStatus((current) => applyTerminalBackupOperation(current, operation));
        setMessage(
          operation.status === "completed"
            ? operation.operation_kind === "restore"
              ? "Workspace restore completed."
              : "Workspace backup completed."
            : operation.failure_message ?? "The backup operation failed."
        );
        void loadBackupData();
      },
      onTransientError: () => {
        setError("Operation status is temporarily unavailable. Retrying automatically.");
      },
      onFatalError: (pollError) => {
        setError(safeBackupError(pollError));
      },
    });

    return () => {
      controller.abort();
    };
  }, [loadBackupData, pollingOperationId]);

  const beginConnect = async () => {
    setIsActionPending(true);
    setError(null);
    setMessage(null);
    try {
      const response = await connectGoogleDrive();
      window.location.assign(response.authorization_url);
    } catch (connectError) {
      setError(safeBackupError(connectError));
      setIsActionPending(false);
    }
  };

  const beginBackup = async () => {
    setIsActionPending(true);
    setError(null);
    setMessage(null);
    try {
      const operation = await createWorkspaceBackup();
      setStatus((current) => (current ? { ...current, active_operation: operation } : current));
    } catch (backupError) {
      setError(safeBackupError(backupError));
    } finally {
      setIsActionPending(false);
    }
  };

  const beginRestore = async (backup: WorkspaceBackup) => {
    setIsActionPending(true);
    setError(null);
    setMessage(null);
    try {
      const operation = await restoreWorkspaceBackup(backup.backup_id, "RESTORE");
      setStatus((current) => (current ? { ...current, active_operation: operation } : current));
    } catch (restoreError) {
      setError(safeBackupError(restoreError));
      throw restoreError;
    } finally {
      setIsActionPending(false);
    }
  };

  const confirmDestructiveAction = async () => {
    if (!confirmationKind) return;
    setIsActionPending(true);
    setError(null);
    setMessage(null);
    try {
      if (confirmationKind === "delete") {
        const response = await deleteGoogleDriveBackups();
        setMessage(
          response.deleted === 1
            ? "One cloud backup was deleted."
            : `${response.deleted} cloud backups were deleted.`
        );
      } else {
        await disconnectGoogleDrive();
        setMessage("Google Drive was disconnected.");
      }
      setConfirmationKind(null);
      await loadBackupData();
    } catch (actionError) {
      setError(safeBackupError(actionError));
    } finally {
      setIsActionPending(false);
    }
  };

  return (
    <>
      {isLoading || !status ? (
        <Card className="border-border bg-card" aria-label="Loading Google Drive backup settings">
          <CardHeader className="border-b border-border">
            <Skeleton className="h-5 w-48 rounded-none" />
          </CardHeader>
          <CardContent className="space-y-3 py-5">
            <Skeleton className="h-20 w-full rounded-none" />
            <Skeleton className="h-24 w-full rounded-none" />
          </CardContent>
        </Card>
      ) : (
        <GoogleDriveBackupSettingsView
          status={status}
          backups={backups}
          actionPending={isActionPending}
          error={error}
          message={message}
          listUnavailable={Boolean(error) && status.connection_status === "connected"}
          onBackup={() => void beginBackup()}
          onConnect={() => void beginConnect()}
          onDeleteBackups={() => setConfirmationKind("delete")}
          onDisconnect={() => setConfirmationKind("disconnect")}
          onRefresh={() => {
            setIsLoading(true);
            setMessage(null);
            void loadBackupData();
          }}
          onRestore={setSelectedBackup}
        />
      )}

      <RestoreBackupDialog
        backup={selectedBackup}
        open={Boolean(selectedBackup)}
        onOpenChange={(open) => {
          if (!open) setSelectedBackup(null);
        }}
        onRestore={beginRestore}
      />

      <DestructiveConfirmationDialog
        kind={confirmationKind}
        open={Boolean(confirmationKind)}
        isPending={isActionPending}
        onOpenChange={(open) => {
          if (!open) setConfirmationKind(null);
        }}
        onConfirm={() => void confirmDestructiveAction()}
      />
    </>
  );
}
