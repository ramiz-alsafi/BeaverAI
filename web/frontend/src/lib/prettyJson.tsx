/**
 * Tool args/output arrive over the wire as plain strings — usually a JSON
 * object, sometimes not. Everywhere we used to just dump that string
 * verbatim (the "raw json flashes then vanishes" complaint). This module
 * is the one place that turns those strings into something worth looking
 * at: a safely-parsed value plus a small colored renderer, shared by the
 * pretty inline chips in ToolBlock and the fuller raw view in ActivityPanel.
 */

export type ParsedArgs =
  | { ok: true; value: unknown }
  | { ok: false; raw: string };

export function tryParseJson(raw: string): ParsedArgs {
  const trimmed = raw?.trim();
  if (!trimmed) return { ok: false, raw: "" };
  try {
    return { ok: true, value: JSON.parse(trimmed) };
  } catch {
    return { ok: false, raw };
  }
}

/** Flat key/value pairs for the compact chip row — only meaningful for a
 * top-level JSON object. Nested values are stringified rather than
 * recursed into, since chips are meant to be scannable at a glance. */
export function flattenForChips(value: unknown): Array<[string, string]> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return [];
  }
  return Object.entries(value as Record<string, unknown>).map(([k, v]) => {
    const display = typeof v === "string" ? v : JSON.stringify(v);
    return [k, display];
  });
}

export function truncate(text: string, max = 64): string {
  if (text.length <= max) return text;
  return text.slice(0, max - 1) + "…";
}

// ── Colored recursive JSON renderer, used in the ActivityPanel ─────────────

const INDENT = "  ";

function colorForPrimitive(v: unknown): string {
  if (typeof v === "string") return "text-sage";
  if (typeof v === "number") return "text-gold";
  if (typeof v === "boolean") return "text-rust";
  if (v === null) return "text-muted";
  return "text-cream";
}

function renderPrimitive(v: unknown): string {
  if (typeof v === "string") return JSON.stringify(v);
  return String(v);
}

export function PrettyJsonNode({ value, depth = 0 }: { value: unknown; depth?: number }) {
  const pad = INDENT.repeat(depth);
  const padClose = INDENT.repeat(Math.max(depth - 1, 0));

  if (value === null || typeof value !== "object") {
    return <span className={colorForPrimitive(value)}>{renderPrimitive(value)}</span>;
  }

  if (Array.isArray(value)) {
    if (value.length === 0) return <span className="text-muted">[]</span>;
    return (
      <>
        {"[\n"}
        {value.map((item, i) => (
          <span key={i}>
            {pad}
            <PrettyJsonNode value={item} depth={depth + 1} />
            {i < value.length - 1 ? "," : ""}
            {"\n"}
          </span>
        ))}
        {padClose}
        {"]"}
      </>
    );
  }

  const entries = Object.entries(value as Record<string, unknown>);
  if (entries.length === 0) return <span className="text-muted">{"{}"}</span>;
  return (
    <>
      {"{\n"}
      {entries.map(([k, v], i) => (
        <span key={k}>
          {pad}
          <span className="text-goldDim">{JSON.stringify(k)}</span>
          {": "}
          <PrettyJsonNode value={v} depth={depth + 1} />
          {i < entries.length - 1 ? "," : ""}
          {"\n"}
        </span>
      ))}
      {padClose}
      {"}"}
    </>
  );
}
