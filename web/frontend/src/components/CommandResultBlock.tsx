import { motion } from "framer-motion";
import type { ReactNode } from "react";
import type { CommandEntry } from "../types";

interface Props {
  entry: CommandEntry;
  /** Sends a raw command (e.g. "/continue thread-id") without adding a user bubble. */
  sendCommand: (cmdText: string) => void;
}

function Title({ children }: { children: ReactNode }) {
  return (
    <div className="mb-1.5 text-[0.85em] font-bold uppercase tracking-wide text-gold">{children}</div>
  );
}

function Footer({ text }: { text?: string }) {
  if (!text) return null;
  return <div className="mt-2 text-[0.85em] text-muted">{text}</div>;
}

export function CommandResultBlock({ entry, sendCommand }: Props) {
  const { result } = entry;

  const body = (() => {
    switch (result.kind) {
      case "error":
        return (
          <div dir="auto" className="rounded-md border border-rust px-3.5 py-2 text-rust">
            {result.message}
          </div>
        );

      case "table":
        return (
          <div dir="auto" className="rounded-md border border-goldDim/40 bg-panel px-3.5 py-2.5 text-[0.92em]">
            <Title>{result.title ?? ""}</Title>
            <table className="w-full border-collapse">
              <thead>
                <tr>
                  {(result.columns ?? []).map((col) => (
                    <th
                      key={col}
                      className="border-b border-goldDim px-2 py-1 text-left text-[0.85em] font-normal text-goldDim"
                    >
                      {col}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {(result.rows ?? []).map((row, i) => (
                  <tr key={i}>
                    {row.map((cell, j) => (
                      <td key={j} className="border-b border-goldDim/25 px-2 py-1">
                        {cell}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
            <Footer text={result.footer} />
          </div>
        );

      case "sessions":
        return (
          <div dir="auto" className="rounded-md border border-goldDim/40 bg-panel px-3.5 py-2.5 text-[0.92em]">
            <Title>sessions</Title>
            {!result.rows || result.rows.length === 0 ? (
              <div>no saved sessions</div>
            ) : (
              result.rows.map((row) => (
                <div
                  key={row.thread_id}
                  className={`flex items-center justify-between gap-2.5 border-b border-goldDim/25 py-1.5 ${
                    row.current ? "text-sage" : ""
                  }`}
                >
                  <span>
                    {row.thread_id} · {row.type} · {row.checkpoints} checkpoint(s)
                    {row.current ? "  (current)" : ""}
                  </span>
                  {!row.current && (
                    <span className="flex shrink-0 gap-2">
                      <button
                        type="button"
                        onClick={() => sendCommand(`/continue ${row.thread_id}`)}
                        className="rounded border border-goldDim px-2.5 py-0.5 text-[0.8em] hover:border-gold"
                      >
                        resume
                      </button>
                      <button
                        type="button"
                        onClick={() => sendCommand(`/delete ${row.thread_id}`)}
                        className="rounded border border-goldDim px-2.5 py-0.5 text-[0.8em] hover:border-rust hover:text-rust"
                      >
                        delete
                      </button>
                    </span>
                  )}
                </div>
              ))
            )}
            <Footer text={result.footer} />
          </div>
        );

      case "hud": {
        const fields: [string, unknown][] = [
          ["model", result.model],
          ["persona", result.persona],
          ["temperature", result.temperature],
          ["messages", result.msg_count],
          ["tokens", `${result.token_count} / ${result.context_limit}`],
          ["tools", result.tool_count],
          ["plugins", result.plugin_count],
          ["agents", result.agent_count],
          ["thread", result.thread_id],
        ];
        return (
          <div dir="auto" className="rounded-md border border-goldDim/40 bg-panel px-3.5 py-2.5 text-[0.92em]">
            <Title>status</Title>
            {fields.map(([k, v]) => (
              <div key={k}>
                <span className="text-goldDim">{k}:</span> {String(v ?? "")}
              </div>
            ))}
          </div>
        );
      }

      case "info":
      default:
        return (
          <div dir="auto" className="rounded-md border border-goldDim/40 bg-panel px-3.5 py-2.5 text-[0.92em]">
            <Title>{result.title ?? ""}</Title>
            {(result.lines ?? []).map((line, i) => (
              <div key={i}>{line}</div>
            ))}
          </div>
        );
    }
  })();

  return (
    <motion.div
      initial={{ opacity: 0, y: 10, scale: 0.98 }}
      animate={{ opacity: 1, y: 0, scale: 1 }}
      transition={{ type: "spring", stiffness: 380, damping: 32 }}
      className="self-stretch text-cream"
    >
      {body}
    </motion.div>
  );
}
