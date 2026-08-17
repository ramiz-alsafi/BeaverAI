interface Props {
  path?: string;
  oldStr: string;
  newStr: string;
}

interface DiffLine {
  type: "same" | "del" | "add";
  text: string;
}

/**
 * Not a real Myers diff — old_str/new_str are already the exact, unique
 * substring being swapped (that's replace_in_file's contract), so a full
 * LCS algorithm is overkill. Aligns matching leading/trailing lines and
 * shows the differing middle as a straight removed/added block, which is
 * what this tool's edits look like in practice (a few changed lines
 * surrounded by unchanged context the caller included for uniqueness).
 */
function buildDiff(oldStr: string, newStr: string): DiffLine[] {
  const oldLines = oldStr.split("\n");
  const newLines = newStr.split("\n");

  let head = 0;
  while (head < oldLines.length && head < newLines.length && oldLines[head] === newLines[head]) head++;

  let tailOld = oldLines.length;
  let tailNew = newLines.length;
  while (
    tailOld > head &&
    tailNew > head &&
    oldLines[tailOld - 1] === newLines[tailNew - 1]
  ) {
    tailOld--;
    tailNew--;
  }

  const lines: DiffLine[] = [];
  for (let i = 0; i < head; i++) lines.push({ type: "same", text: oldLines[i] });
  for (let i = head; i < tailOld; i++) lines.push({ type: "del", text: oldLines[i] });
  for (let i = head; i < tailNew; i++) lines.push({ type: "add", text: newLines[i] });
  for (let i = tailOld; i < oldLines.length; i++) lines.push({ type: "same", text: oldLines[i] });
  return lines;
}

export function DiffView({ path, oldStr, newStr }: Props) {
  const lines = buildDiff(oldStr, newStr);

  return (
    <div className="rounded-md border border-goldDim/25 bg-ink/60">
      {path && (
        <div className="border-b border-goldDim/20 px-2.5 py-1 text-[11px] text-muted">{path}</div>
      )}
      <pre className="overflow-auto px-0 py-1.5 text-[12.5px] leading-relaxed">
        {lines.map((line, i) => (
          <div
            key={i}
            className={
              line.type === "add"
                ? "bg-sage/10 px-3 text-sage before:mr-2 before:content-['+']"
                : line.type === "del"
                  ? "bg-rust/10 px-3 text-rust before:mr-2 before:content-['-']"
                  : "px-3 text-muted before:mr-2 before:content-['_'] before:text-transparent"
            }
          >
            <span className="whitespace-pre-wrap break-words">{line.text || " "}</span>
          </div>
        ))}
      </pre>
    </div>
  );
}
