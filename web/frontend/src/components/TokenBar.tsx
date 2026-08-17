import { motion } from "framer-motion";
import { useEffect, useRef, useState } from "react";

interface Props {
  count: number;
  limit: number;
}

/** [ANIM-1] A moving highlight sweeps across the fill briefly whenever
 * count increases — the plain CSS width/color transition alone made an
 * active stream look identical to a static bar that just happened to be
 * partially filled; the sweep gives a clear "this is live and growing"
 * signal without animating on every single render. */
export function TokenBar({ count, limit }: Props) {
  const pct = limit ? Math.min(1, count / limit) : 0;
  const color = pct < 0.6 ? "bg-sage" : pct < 0.85 ? "bg-amber" : "bg-rust";

  const prevCount = useRef(count);
  const [shimmerKey, setShimmerKey] = useState(0);
  useEffect(() => {
    if (count > prevCount.current) setShimmerKey((k) => k + 1);
    prevCount.current = count;
  }, [count]);

  return (
    <div className="relative h-1 overflow-hidden bg-panel">
      <div
        className={`h-full transition-[width,background-color] duration-300 ease-out ${color}`}
        style={{ width: `${pct * 100}%` }}
      />
      {shimmerKey > 0 && (
        <motion.div
          key={shimmerKey}
          className="pointer-events-none absolute inset-y-0 left-0 w-8 bg-gradient-to-r from-transparent via-white/40 to-transparent"
          initial={{ left: "-2rem" }}
          animate={{ left: `${pct * 100}%` }}
          transition={{ duration: 0.5, ease: "easeOut" }}
        />
      )}
    </div>
  );
}