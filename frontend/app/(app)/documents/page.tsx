"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";

import { PageHeader } from "@/components/shell";
import {
  Badge,
  Button,
  Card,
  Empty,
  ErrorNote,
  Loading,
  Table,
  Td,
} from "@/components/ui";
import { type DocumentRow, type UploadResult, get, uploadDocument } from "@/lib/api";
import { useAuth } from "@/lib/auth";

const ACCEPT = ".pdf,.docx,.xlsx,.csv,.txt,.md,.png,.jpg,.jpeg,.webp,.gif";

function humanSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

export default function DocumentsPage() {
  const token = useAuth((s) => s.session?.token ?? null);
  const queryClient = useQueryClient();
  const inputRef = useRef<HTMLInputElement>(null);
  const [notice, setNotice] = useState<UploadResult | null>(null);

  const docs = useQuery({
    queryKey: ["documents"],
    queryFn: () =>
      get<{ total: number; documents: DocumentRow[] }>("/documents", token),
    enabled: Boolean(token),
  });

  const upload = useMutation({
    mutationFn: (file: File) => uploadDocument(file, token),
    onSuccess: (result) => {
      setNotice(result);
      queryClient.invalidateQueries({ queryKey: ["documents"] });
      if (inputRef.current) inputRef.current.value = "";
    },
  });

  return (
    <>
      <PageHeader
        title="Documents"
        description="Contracts, scale tickets and spreadsheets — extracted, chunked, embedded and searchable. Photographed tickets are read with OCR."
      />

      <Card title="Upload" className="mb-4">
        <div className="flex flex-wrap items-end gap-3">
          <div className="min-w-0 flex-1">
            <label htmlFor="file" className="mb-1.5 block text-sm font-medium">
              Choose a file
            </label>
            <p id="file-hint" className="mb-1.5 text-xs text-[var(--text-muted)]">
              PDF, DOCX, XLSX, CSV, TXT, MD up to 25 MB. Images (PNG, JPEG, WEBP,
              GIF) up to 6 MB are read with OCR.
            </p>
            <input
              ref={inputRef}
              id="file"
              name="file"
              type="file"
              accept={ACCEPT}
              aria-describedby="file-hint"
              onChange={(event) => {
                const file = event.target.files?.[0];
                if (file) upload.mutate(file);
              }}
              className="block w-full cursor-pointer rounded-lg border border-[var(--border-strong)] bg-[var(--surface)] p-2 text-sm file:mr-3 file:cursor-pointer file:rounded-md file:border-0 file:bg-[var(--accent)] file:px-3 file:py-1.5 file:text-sm file:font-medium file:text-[var(--bg)]"
            />
          </div>
        </div>

        <div aria-live="polite" className="mt-3">
          {upload.isPending && <Loading label="Uploading and processing" />}
          {upload.error && <ErrorNote message={(upload.error as Error).message} />}
          {notice && !upload.isPending && (
            <div
              className="rounded-lg border px-3 py-2.5 text-sm"
              style={{
                borderColor: notice.duplicate ? "var(--warn)" : "var(--accent)",
                background: notice.duplicate
                  ? "var(--warn-soft)"
                  : "var(--accent-soft)",
                color: notice.duplicate ? "var(--warn)" : "var(--accent-text)",
              }}
            >
              <strong className="font-semibold">
                {notice.duplicate ? "Already uploaded:" : "Uploaded:"}
              </strong>{" "}
              {notice.document.filename} — {notice.document.chunk_count} chunk
              {notice.document.chunk_count === 1 ? "" : "s"}, version{" "}
              {notice.document.version}
              {notice.index_error && (
                <span className="mt-1 block" style={{ color: "var(--danger)" }}>
                  Stored, but not indexed: {notice.index_error}. Run the embedding
                  agent to retry.
                </span>
              )}
              {notice.document.note && (
                <span className="mt-1 block text-xs opacity-90">
                  {notice.document.note}
                </span>
              )}
            </div>
          )}
        </div>
      </Card>

      <Card title={`Library (${docs.data?.total ?? 0})`}>
        {docs.isLoading && <Loading label="Loading documents" />}
        {docs.error && <ErrorNote message={(docs.error as Error).message} />}
        {docs.data && docs.data.documents.length === 0 && (
          <Empty>Nothing uploaded yet. Add a contract or a scale ticket above.</Empty>
        )}
        {docs.data && docs.data.documents.length > 0 && (
          <Table
            caption="Uploaded documents with size, chunk count, indexing status and content hash"
            headers={["File", "Size", "Chunks", "Status", "Version", "Hash", "Added"]}
          >
            {docs.data.documents.map((doc) => (
              <tr key={doc.id}>
                <Td className="font-medium">
                  {doc.filename}
                  {doc.note && (
                    <span className="mt-0.5 block text-xs font-normal text-[var(--text-faint)]">
                      {doc.note}
                    </span>
                  )}
                </Td>
                <Td className="tabular text-[var(--text-muted)]">
                  {humanSize(doc.size_bytes)}
                </Td>
                <Td className="tabular">{doc.chunk_count}</Td>
                <Td>
                  <Badge
                    tone={
                      doc.status === "embedded"
                        ? "accent"
                        : doc.status === "empty"
                          ? "warn"
                          : "neutral"
                    }
                  >
                    {doc.status}
                  </Badge>
                </Td>
                <Td className="tabular">v{doc.version}</Td>
                <Td className="tabular text-xs text-[var(--text-faint)]">
                  {doc.sha256.slice(0, 10)}…
                </Td>
                <Td className="text-xs text-[var(--text-muted)]">
                  <time dateTime={doc.uploaded_at}>
                    {new Date(doc.uploaded_at).toLocaleDateString()}
                  </time>
                </Td>
              </tr>
            ))}
          </Table>
        )}
      </Card>
    </>
  );
}
