import { motion } from "framer-motion";
import type { ErrorEntry } from "../types";

interface Props {
  entry: ErrorEntry;
}

export function ErrorBlock({ entry }: Props) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 10, scale: 0.98 }}
      animate={{ opacity: 1, y: 0, scale: 1 }}
      transition={{ type: "spring", stiffness: 380, damping: 32 }}
      dir="auto"
      className="self-start rounded-md border border-rust px-3.5 py-2 text-[13.5px] text-rust whitespace-pre-wrap break-words"
    >
      {entry.message}
    </motion.div>
  );
}
