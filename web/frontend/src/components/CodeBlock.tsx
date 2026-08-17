import { highlightAuto } from "../lib/highlight";
import { CopyButton } from "./CopyButton";

interface Props {
  code: string;
  lang?: string;
  /** Caps visible height with an internal scrollbar instead of pushing the
   *  rest of the chat log down — most useful for large tool outputs. */
  maxHeightPx?: number;
}

export function CodeBlock({ code, lang, maxHeightPx }: Props) {
  return (
    <div className="group relative rounded-md border border-goldDim/25 bg-ink/60">
      <div className="flex items-center justify-between border-b border-goldDim/20 px-2.5 py-1">
        <span className="text-[10.5px] uppercase tracking-wide text-muted">{lang || "text"}</span>
        <CopyButton text={code} />
      </div>
      <pre
        className="overflow-auto px-3 py-2 text-[12.5px] leading-relaxed"
        style={maxHeightPx ? { maxHeight: maxHeightPx } : undefined}
      >
        <code>{highlightAuto(code)}</code>
      </pre>
    </div>
  );
}
