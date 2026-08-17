import { motion } from "framer-motion";

interface Props {
  label: string;
}

/** [SPIN-2] Real motion instead of cycling braille text — a small rotating
 * ring with a soft pulsing glow behind it, in the gold accent. The braille
 * frame cycling read as static/flat at a glance despite technically
 * updating every 83ms; an actually-animating element communicates "still
 * working" far more clearly, especially during longer tool calls. */
export function Spinner({ label }: Props) {
  return (
    <span className="inline-flex items-center gap-2">
      <span className="relative inline-flex h-3.5 w-3.5 items-center justify-center">
        <motion.span
          className="absolute inset-0 rounded-full bg-gold/30"
          animate={{ scale: [0.6, 1.15, 0.6], opacity: [0.5, 0.15, 0.5] }}
          transition={{ duration: 1.4, repeat: Infinity, ease: "easeInOut" }}
        />
        <motion.span
          className="h-3 w-3 rounded-full border-[1.5px] border-gold border-t-transparent"
          animate={{ rotate: 360 }}
          transition={{ duration: 0.8, repeat: Infinity, ease: "linear" }}
        />
      </span>
      <span>{label}</span>
    </span>
  );
}