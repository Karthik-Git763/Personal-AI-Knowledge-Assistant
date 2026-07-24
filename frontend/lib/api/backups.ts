import { apiClient } from "@/lib/api/client";

export type BackupStatus = "pending" | "exporting" | "uploading" | "completed" | "failed";
export type BackupOperationKind = "snapshot" | "restore";
export type BackupTrigger = "manual" | "scheduled";
export type DriveConnectionStatus =
  | "connected"
  | "disconnected"
  | "reauthorization_required"
  | "failed";

export interface WorkspaceBackup {
  backup_id: string;
  operation_kind: BackupOperationKind;
  source_backup_id: string | null;
  status: BackupStatus;
  trigger: BackupTrigger;
  schema_version: number;
  archive_size_bytes: number | null;
  item_counts: Record<string, number>;
  started_at: string | null;
  completed_at: string | null;
  failure_message: string | null;
  restore_eligible: boolean;
}

export interface RestorePointSummary {
  backup_id: string;
  schema_version: number;
  archive_size_bytes: number;
  created_at: string;
  restore_eligible: boolean;
}

export interface GoogleDriveBackupStatus {
  configured: boolean;
  enabled: boolean;
  connection_status: DriveConnectionStatus | null;
  google_email: string | null;
  last_attempt_at: string | null;
  last_success_at: string | null;
  next_due_at: string | null;
  consecutive_failures: number;
  active_operation: WorkspaceBackup | null;
  restore_points: RestorePointSummary[];
}

export interface RestorePreview {
  backup_id: string;
  schema_version: number;
  item_counts: Record<string, number>;
  archive_size_bytes: number | null;
}

export async function connectGoogleDrive(): Promise<{ authorization_url: string }> {
  const response = await apiClient.post<{ authorization_url: string }>(
    "/users/me/google-drive/connect"
  );
  return response.data;
}

export async function getGoogleDriveBackupStatus(): Promise<GoogleDriveBackupStatus> {
  const response = await apiClient.get<GoogleDriveBackupStatus>(
    "/users/me/google-drive/status"
  );
  return response.data;
}

export async function disconnectGoogleDrive(): Promise<{ status: string }> {
  const response = await apiClient.delete<{ status: string }>("/users/me/google-drive");
  return response.data;
}

export async function deleteGoogleDriveBackups(): Promise<{ deleted: number }> {
  const response = await apiClient.post<{ deleted: number }>(
    "/users/me/google-drive/delete-backups",
    { confirmation: "DELETE BACKUPS" }
  );
  return response.data;
}

export async function createWorkspaceBackup(): Promise<WorkspaceBackup> {
  const response = await apiClient.post<WorkspaceBackup>("/users/me/backups");
  return response.data;
}

export async function listWorkspaceBackups(): Promise<WorkspaceBackup[]> {
  const response = await apiClient.get<WorkspaceBackup[]>("/users/me/backups");
  return response.data;
}

export async function previewWorkspaceBackup(backupId: string): Promise<RestorePreview> {
  const response = await apiClient.get<RestorePreview>(
    `/users/me/backups/${backupId}/preview`
  );
  return response.data;
}

export async function restoreWorkspaceBackup(
  backupId: string,
  confirmation: "RESTORE"
): Promise<WorkspaceBackup> {
  const response = await apiClient.post<WorkspaceBackup>(
    `/users/me/backups/${backupId}/restore`,
    { confirmation }
  );
  return response.data;
}

export async function getBackupOperation(operationId: string): Promise<WorkspaceBackup> {
  const response = await apiClient.get<WorkspaceBackup>(
    `/users/me/backups/operations/${operationId}`
  );
  return response.data;
}
