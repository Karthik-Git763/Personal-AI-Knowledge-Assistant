"use client";

import { AlertTriangle, DatabaseBackup, Loader2 } from "lucide-react";
import { useEffect, useState } from "react";

import {
  Button,
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  Input,
  Label,
  Skeleton,
} from "@/components/ui/index";
import {
  previewWorkspaceBackup,
  type RestorePreview,
  type WorkspaceBackup,
} from "@/lib/api/backups";

interface RestoreConfirmationContentProps {
  backup: WorkspaceBackup;
  confirmation: string;
  isSubmitting: boolean;
  onCancel: () => void;
  onConfirmationChange: (value: string) => void;
  onConfirm: () => void;
  preview?: RestorePreview | null;
  isLoadingPreview?: boolean;
  error?: string | null;
}

function formatBytes(bytes: number | null): string {
  if (bytes === null || !Number.isFinite(bytes) || bytes < 0) return "Unknown";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function formatCounts(counts: Record<string, number>): string[] {
  return Object.entries(counts)
    .filter(([, count]) => count > 0)
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([name, count]) => {
      const normalized = name.replaceAll("_", " ");
      const singular = normalized.endsWith("s") ? normalized.slice(0, -1) : normalized;
      return `${count.toLocaleString()} ${count === 1 ? singular : normalized}`;
    });
}

export function RestoreConfirmationContent({
  backup,
  confirmation,
  isSubmitting,
  onCancel,
  onConfirmationChange,
  onConfirm,
  preview = null,
  isLoadingPreview = false,
  error = null,
}: RestoreConfirmationContentProps) {
  const counts = formatCounts(preview?.item_counts ?? backup.item_counts);

  return (
    <>
      <div className="flex flex-col gap-1.5">
        <div className="flex items-center gap-2">
          <DatabaseBackup className="h-4 w-4 text-muted-foreground" />
          <div className="font-heading text-base font-medium text-foreground">
            Replace workspace from backup
          </div>
        </div>
        <p className="text-sm text-muted-foreground">
          This restores the selected snapshot and replaces the current workspace after creating a
          safety backup.
        </p>
      </div>

      {isLoadingPreview ? (
        <div className="space-y-2" aria-label="Loading restore preview">
          <Skeleton className="h-9 w-full rounded-none" />
          <Skeleton className="h-16 w-full rounded-none" />
        </div>
      ) : (
        <div className="space-y-3 border border-border bg-muted/40 p-3">
          <div className="flex flex-wrap items-center justify-between gap-2 text-xs">
            <span className="font-medium text-foreground">
              Schema {preview?.schema_version ?? backup.schema_version}
            </span>
            <span className="text-muted-foreground">
              {formatBytes(preview?.archive_size_bytes ?? backup.archive_size_bytes)}
            </span>
          </div>
          {counts.length > 0 ? (
            <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
              {counts.map((count) => (
                <span key={count}>{count}</span>
              ))}
            </div>
          ) : (
            <p className="text-xs text-muted-foreground">No item counts are available.</p>
          )}
        </div>
      )}

      <div className="flex gap-2 border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive">
        <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
        <p>Current notes, documents, chats, and preferences will be replaced.</p>
      </div>

      <div className="space-y-2">
        <Label htmlFor="restore-confirmation">
          Type <span className="font-mono font-semibold">RESTORE</span> to continue
        </Label>
        <Input
          id="restore-confirmation"
          value={confirmation}
          onChange={(event) => onConfirmationChange(event.target.value)}
          autoComplete="off"
          spellCheck={false}
          disabled={isSubmitting || isLoadingPreview}
        />
      </div>

      {error ? (
        <p
          className="border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive"
          role="alert"
          aria-live="assertive"
        >
          {error}
        </p>
      ) : null}

      <DialogFooter>
        <Button type="button" variant="outline" onClick={onCancel} disabled={isSubmitting}>
          Cancel
        </Button>
        <Button
          type="button"
          variant="destructive"
          onClick={onConfirm}
          disabled={confirmation !== "RESTORE" || isSubmitting || isLoadingPreview || Boolean(error)}
        >
          {isSubmitting ? <Loader2 className="h-4 w-4 animate-spin" /> : null}
          Replace workspace
        </Button>
      </DialogFooter>
    </>
  );
}

interface RestoreBackupDialogProps {
  backup: WorkspaceBackup | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onRestore: (backup: WorkspaceBackup) => Promise<void>;
}

export function RestoreBackupDialog({
  backup,
  open,
  onOpenChange,
  onRestore,
}: RestoreBackupDialogProps) {
  const [confirmation, setConfirmation] = useState("");
  const [preview, setPreview] = useState<RestorePreview | null>(null);
  const [isLoadingPreview, setIsLoadingPreview] = useState(false);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open || !backup) return;

    let cancelled = false;
    const previewLoad = window.setTimeout(() => {
      setConfirmation("");
      setPreview(null);
      setIsLoadingPreview(true);
      setError(null);
      void previewWorkspaceBackup(backup.backup_id)
        .then((response) => {
          if (!cancelled) setPreview(response);
        })
        .catch(() => {
          if (!cancelled) {
            setError("The restore preview could not be loaded. Refresh and try again.");
          }
        })
        .finally(() => {
          if (!cancelled) setIsLoadingPreview(false);
        });
    }, 0);

    return () => {
      cancelled = true;
      window.clearTimeout(previewLoad);
    };
  }, [backup, open]);

  if (!backup) return null;

  const confirmRestore = async () => {
    if (confirmation !== "RESTORE" || isSubmitting || isLoadingPreview || error) return;
    setIsSubmitting(true);
    try {
      await onRestore(backup);
      onOpenChange(false);
    } catch {
      setError("The restore could not be started. Refresh and try again.");
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="rounded-none sm:max-w-lg">
        <DialogHeader className="sr-only">
          <DialogTitle>Replace workspace from backup</DialogTitle>
          <DialogDescription>
            Preview and confirm replacement of the current workspace.
          </DialogDescription>
        </DialogHeader>
        <RestoreConfirmationContent
          backup={backup}
          confirmation={confirmation}
          isSubmitting={isSubmitting}
          onCancel={() => onOpenChange(false)}
          onConfirmationChange={setConfirmation}
          onConfirm={() => void confirmRestore()}
          preview={preview}
          isLoadingPreview={isLoadingPreview}
          error={error}
        />
      </DialogContent>
    </Dialog>
  );
}
