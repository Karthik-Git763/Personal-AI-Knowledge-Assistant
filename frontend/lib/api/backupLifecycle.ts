import {
  type GoogleDriveBackupStatus,
  type WorkspaceBackup,
} from "@/lib/api/backups";
import { APIRequestError } from "@/lib/api/client";

const DEFAULT_INITIAL_POLL_DELAY_MS = 1500;
const DEFAULT_MAX_POLL_DELAY_MS = 10_000;

type PollWait = (delayMs: number, signal?: AbortSignal) => Promise<boolean>;

function waitForPollDelay(delayMs: number, signal?: AbortSignal): Promise<boolean> {
  if (signal?.aborted) return Promise.resolve(false);

  return new Promise((resolve) => {
    const timer = window.setTimeout(() => {
      signal?.removeEventListener("abort", cancel);
      resolve(true);
    }, delayMs);
    const cancel = () => {
      window.clearTimeout(timer);
      resolve(false);
    };
    signal?.addEventListener("abort", cancel, { once: true });
  });
}

export function isTransientBackupPollError(error: unknown): boolean {
  if (!(error instanceof APIRequestError)) return true;
  if (error.errorCode === "GOOGLE_DRIVE_RETRYABLE") return true;
  const status = error.statusCode;
  return status === undefined || status === 0 || status === 408 || status === 429 || status >= 500;
}

interface PollBackupOperationOptions {
  operationId: string;
  getOperation: (operationId: string) => Promise<WorkspaceBackup>;
  onProgress: (operation: WorkspaceBackup) => void;
  onTerminal: (operation: WorkspaceBackup) => void;
  onTransientError: (error: unknown, nextDelayMs: number) => void;
  onFatalError?: (error: unknown) => void;
  isTransientError?: (error: unknown) => boolean;
  wait?: PollWait;
  signal?: AbortSignal;
  initialDelayMs?: number;
  maxDelayMs?: number;
}

export async function pollBackupOperation({
  operationId,
  getOperation,
  onProgress,
  onTerminal,
  onTransientError,
  onFatalError,
  isTransientError = isTransientBackupPollError,
  wait = waitForPollDelay,
  signal,
  initialDelayMs = DEFAULT_INITIAL_POLL_DELAY_MS,
  maxDelayMs = DEFAULT_MAX_POLL_DELAY_MS,
}: PollBackupOperationOptions): Promise<WorkspaceBackup | null> {
  let delayMs = initialDelayMs;

  while (!signal?.aborted) {
    const shouldContinue = await wait(delayMs, signal);
    if (!shouldContinue || signal?.aborted) return null;

    try {
      const operation = await getOperation(operationId);
      if (signal?.aborted) return null;
      delayMs = initialDelayMs;
      if (operation.status === "pending" || operation.status === "exporting" || operation.status === "uploading") {
        onProgress(operation);
        continue;
      }
      onTerminal(operation);
      return operation;
    } catch (error) {
      if (signal?.aborted) return null;
      if (!isTransientError(error)) {
        onFatalError?.(error);
        return null;
      }
      delayMs = Math.min(delayMs * 2, maxDelayMs);
      onTransientError(error, delayMs);
    }
  }

  return null;
}

interface LoadBackupSettingsDataOptions {
  getStatus: () => Promise<GoogleDriveBackupStatus>;
  listBackups: () => Promise<WorkspaceBackup[]>;
  previousStatus: GoogleDriveBackupStatus | null;
  previousBackups: WorkspaceBackup[];
}

export interface BackupSettingsLoadResult {
  status: GoogleDriveBackupStatus | null;
  backups: WorkspaceBackup[];
  statusError: unknown | null;
  listError: unknown | null;
}

export async function loadBackupSettingsData({
  getStatus,
  listBackups,
  previousStatus,
  previousBackups,
}: LoadBackupSettingsDataOptions): Promise<BackupSettingsLoadResult> {
  let status: GoogleDriveBackupStatus;
  try {
    status = await getStatus();
  } catch (statusError) {
    return {
      status: previousStatus,
      backups: previousBackups,
      statusError,
      listError: null,
    };
  }

  if (status.connection_status !== "connected") {
    return { status, backups: [], statusError: null, listError: null };
  }

  try {
    const backups = await listBackups();
    return { status, backups, statusError: null, listError: null };
  } catch (listError) {
    return {
      status,
      backups: previousBackups,
      statusError: null,
      listError,
    };
  }
}
