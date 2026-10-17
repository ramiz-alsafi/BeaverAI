import { useEffect, useRef } from "react";
import type { ApprovalDecision, ApprovalRequestInfo } from "../types";

interface Props {
  request: ApprovalRequestInfo | null;
  onRespond: (id: string, decision: ApprovalDecision) => void;
}

/**
 * [APPROVAL-1] "The agent wants to run X — allow it?"
 *
 * Shown while a turn is paused on a gated tool call (shell command, file
 * write, ...). Safe by default: focus starts on Deny, so a stray Enter or
 * Space declines; Escape declines; clicking the backdrop does nothing (an
 * accidental click must not approve OR silently dismiss a pending request).
 * Matches agent/approval.py's decisions: allow | allow_session | deny.
 */
export function ApprovalModal({ request, onRespond }: Props) {
  const denyRef = useRef<HTMLButtonElement>(null);
  const id = request?.id;

  useEffect(() => {
    if (!id) return;
    denyRef.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onRespond(id, "deny");
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [id, onRespond]);

  if (!request) return null;

  return (
    <div className="fixed inset-0 z-[100] flex items-center justify-center bg-black/60 px-4 backdrop-blur-sm">
      <div
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="approval-title"
        aria-describedby="approval-summary"
        className="w-full max-w-lg rounded-lg border border-gold/50 bg-panel p-5 shadow-2xl"
      >
        <p id="approval-title" className="mb-1 text-[1.05em] font-bold text-gold">
          Approve this action?
        </p>
        <p className="mb-3 text-[0.85em] text-muted">
          Beaver wants to use <code className="rounded bg-ink px-1 py-0.5 text-cream">{request.tool}</code>
          {" — "}
          {request.reason}
          {request.persona ? ` (persona: ${request.persona})` : ""}.
        </p>
        <pre
          id="approval-summary"
          className="mb-4 max-h-48 overflow-auto whitespace-pre-wrap break-all rounded border border-goldDim/30 bg-ink p-3 text-[0.85em] text-cream"
        >
          {request.summary}
        </pre>
        <div className="flex flex-wrap justify-end gap-2">
          <button
            ref={denyRef}
            type="button"
            onClick={() => onRespond(request.id, "deny")}
            className="rounded border border-rust/60 px-4 py-2 text-[0.9em] text-rust transition-colors hover:bg-rust/10"
          >
            Deny
          </button>
          <button
            type="button"
            onClick={() => onRespond(request.id, "allow_session")}
            title={`Stop asking about ${request.tool} until you reload or reconnect`}
            className="rounded border border-goldDim/40 px-4 py-2 text-[0.9em] text-muted transition-colors hover:border-gold hover:text-cream"
          >
            Allow {request.tool} for this session
          </button>
          <button
            type="button"
            onClick={() => onRespond(request.id, "allow")}
            className="rounded bg-gold px-4 py-2 text-[0.9em] font-bold text-ink transition-colors hover:bg-goldDim"
          >
            Allow once
          </button>
        </div>
      </div>
    </div>
  );
}
