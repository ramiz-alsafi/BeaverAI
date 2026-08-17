import type { ReactNode } from "react";

/**
 * Deliberately not pulling in prismjs/highlight.js for this — tool
 * args/output are almost always JSON, occasionally a shell command or a
 * short code snippet, never a full file. A small regex tokenizer covers
 * that well without adding a dependency + language-grammar bundle for a
 * UI that mostly needs "doesn't look like a flat grey blob."
 */

interface Token {
  text: string;
  cls?: string;
}

const CLS = {
  key: "text-gold",
  string: "text-sage",
  number: "text-amber",
  bool: "text-rust",
  punct: "text-muted",
  comment: "text-muted italic",
  keyword: "text-gold",
};

function tokenizeJson(src: string): Token[] {
  const tokens: Token[] = [];
  const re = /("(?:\\.|[^"\\])*")(\s*:)?|(-?\d+\.?\d*(?:[eE][+-]?\d+)?)|(\btrue\b|\bfalse\b|\bnull\b)|([{}[\],:])|(\s+)/g;
  let last = 0;
  let m: RegExpExecArray | null;
  while ((m = re.exec(src)) !== null) {
    if (m.index > last) tokens.push({ text: src.slice(last, m.index) });
    const [, str, colon, num, lit, punct, ws] = m;
    if (str !== undefined) {
      tokens.push({ text: str, cls: colon ? CLS.key : CLS.string });
      if (colon) tokens.push({ text: colon, cls: CLS.punct });
    } else if (num !== undefined) {
      tokens.push({ text: num, cls: CLS.number });
    } else if (lit !== undefined) {
      tokens.push({ text: lit, cls: CLS.bool });
    } else if (punct !== undefined) {
      tokens.push({ text: punct, cls: CLS.punct });
    } else if (ws !== undefined) {
      tokens.push({ text: ws });
    }
    last = re.lastIndex;
  }
  if (last < src.length) tokens.push({ text: src.slice(last) });
  return tokens;
}

const GENERIC_KEYWORDS = new Set([
  "const", "let", "var", "function", "return", "if", "else", "for", "while", "import", "export",
  "from", "async", "await", "class", "def", "try", "except", "catch", "finally", "true", "false",
  "null", "none", "None", "True", "False", "self", "this",
]);

function tokenizeGeneric(src: string): Token[] {
  const tokens: Token[] = [];
  const re = /(#.*$|\/\/.*$)|("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')|(\b\d+\.?\d*\b)|([A-Za-z_][A-Za-z0-9_]*)|(\s+)|(.)/gm;
  let m: RegExpExecArray | null;
  while ((m = re.exec(src)) !== null) {
    const [, comment, str, num, word, ws, other] = m;
    if (comment !== undefined) tokens.push({ text: comment, cls: CLS.comment });
    else if (str !== undefined) tokens.push({ text: str, cls: CLS.string });
    else if (num !== undefined) tokens.push({ text: num, cls: CLS.number });
    else if (word !== undefined) tokens.push({ text: word, cls: GENERIC_KEYWORDS.has(word) ? CLS.keyword : undefined });
    else if (ws !== undefined) tokens.push({ text: ws });
    else if (other !== undefined) tokens.push({ text: other, cls: CLS.punct });
  }
  return tokens;
}

function render(tokens: Token[]): ReactNode {
  return tokens.map((t, i) => (t.cls ? <span key={i} className={t.cls}>{t.text}</span> : <span key={i}>{t.text}</span>));
}

/** Returns pretty-printed + highlighted JSON if `src` parses as JSON,
 *  otherwise null so the caller can fall back to generic highlighting. */
export function highlightJsonIfValid(src: string): ReactNode | null {
  const trimmed = src.trim();
  if (!trimmed || (trimmed[0] !== "{" && trimmed[0] !== "[")) return null;
  try {
    const pretty = JSON.stringify(JSON.parse(trimmed), null, 2);
    return render(tokenizeJson(pretty));
  } catch {
    return null;
  }
}

export function highlightGeneric(src: string): ReactNode {
  return render(tokenizeGeneric(src));
}

/** Best-effort: try JSON first (covers the vast majority of tool
 *  args/output in this app), fall back to generic code highlighting. */
export function highlightAuto(src: string): ReactNode {
  return highlightJsonIfValid(src) ?? highlightGeneric(src);
}
