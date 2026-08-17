import { useState } from "react";

interface Props {
  text: string;
  className?: string;
}

export function CopyButton({ text, className = "" }: Props) {
  const [copied, setCopied] = useState(false);

  const onClick = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1400);
    } catch {
      // clipboard API unavailable (permissions, insecure context) — no-op,
      // button just won't flip to "copied"
    }
  };

  return (
    <button
      type="button"
      onClick={onClick}
      title="Copy"
      className={`shrink-0 rounded px-1.5 py-0.5 text-[11px] text-muted transition-colors hover:text-gold ${className}`}
    >
      {copied ? "copied ✓" : "copy"}
    </button>
  );
}
